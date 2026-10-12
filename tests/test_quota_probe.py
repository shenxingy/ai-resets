"""Tests for the ground-truth quota detector.

The cases here are drawn from shapes actually observed in this account's
history rather than invented ones — see the module docstring of
scripts/quota_probe.py for the measurements behind each.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import quota_probe as qp

WEEK = 10080
WEEK_SECONDS = WEEK * 60

# One live RPC result, shared by the normalisation tests and by the run-loop
# tests that need a poll to succeed.
RPC_RESULT = {
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 14, "windowDurationMins": WEEK, "resetsAt": 1788649350},
            "secondary": None,
            "planType": "pro",
        },
        "codex_secondary": {
            "limitId": "codex_secondary",
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1788099870},
            "secondary": {"usedPercent": 0, "windowDurationMins": WEEK, "resetsAt": 1788686670},
            "planType": "pro",
        },
    },
    "rateLimitResetCredits": {"availableCount": 1, "credits": [{"status": "available"}]},
}

# Every file the probe writes, so a test never touches /var/lib/ai-resets.
STATE_FILES = {
    "SAMPLES_FILE": "quota_samples.jsonl",
    "EVENTS_FILE": "quota_events.jsonl",
    "UPSTREAM_FILE": "upstream_snapshots.jsonl",
    "CURSOR_FILE": "quota_cursor.json",
    "HEALTH_FILE": "probe_health.json",
}


def sample(t, used, resets_at=None, credits=1, limit_id="codex", window=WEEK, **extra):
    """One normalised row. `extra` adds or overrides fields (identity, ids)."""
    return {
        "t": t,
        "limit_id": limit_id,
        "slot": "primary",
        "window_minutes": window,
        "used_percent": float(used),
        "resets_at": resets_at,
        "plan_type": "pro",
        "credits_available": credits,
        **extra,
    }


def fp(namespace, raw):
    """The fingerprint the contract defines, computed independently of qp."""
    return hashlib.sha256(f"ai-resets/{namespace}:{raw}".encode()).hexdigest()[:12]


@contextlib.contextmanager
def temp_state():
    """Point every STATE_DIR path at a throwaway directory.

    Also unsets the second Codex account: a shell that exports
    AI_RESETS_CODEX_HOME_B must not make run() spawn a real codex for it.
    """
    originals = {name: getattr(qp, name) for name in (*STATE_FILES, "CODEX_HOME_B")}
    with tempfile.TemporaryDirectory() as tmp:
        for name, filename in STATE_FILES.items():
            setattr(qp, name, Path(tmp) / filename)
        qp.CODEX_HOME_B = ""
        try:
            yield Path(tmp)
        finally:
            for name, value in originals.items():
                setattr(qp, name, value)


@contextlib.contextmanager
def captured_log():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


def feed(detector, rows):
    """Observe every row, returning the events they produced."""
    events = []
    for row in rows:
        events.extend(detector.observe(row))
    return events


# ─── Snapshot normalisation ──────────────────────────────────────────────────


class NormaliseTest(unittest.TestCase):
    RESULT = RPC_RESULT

    def test_flattens_every_slot(self):
        rows = qp.normalise(self.RESULT, now=1000)
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            sorted(qp.slot_key(r) for r in rows),
            ["codex/10080", "codex_secondary/10080", "codex_secondary/300"],
        )

    def test_carries_credit_bank_onto_every_row(self):
        for row in qp.normalise(self.RESULT, now=1000):
            self.assertEqual(row["credits_available"], 1)

    def test_skips_null_secondary_without_crashing(self):
        rows = [r for r in qp.normalise(self.RESULT, now=1000) if r["limit_id"] == "codex"]
        self.assertEqual(len(rows), 1)

    def test_absent_credit_field_is_none_not_zero(self):
        # None and 0 must stay distinct or a Codex build that omits the field
        # would read as "the credit was just spent".
        result = {"rateLimitsByLimitId": self.RESULT["rateLimitsByLimitId"]}
        self.assertIsNone(qp.credits_available(result))
        self.assertEqual(qp.credits_available({"rateLimitResetCredits": {"availableCount": 0}}), 0)

    def test_falls_back_to_single_rate_limits_object(self):
        rows = qp.normalise(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 5, "windowDurationMins": WEEK, "resetsAt": 7},
                    "planType": "pro",
                }
            },
            now=1000,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["used_percent"], 5.0)

    def test_empty_result_yields_no_rows(self):
        self.assertEqual(qp.normalise({}, now=1000), [])


# The same read with the two fields the probe used to drop: the top-level
# accountId and the itemised bank. Ids here are placeholders; the shape is the
# app-server schema's (codex-cli 0.162.1, GetAccountRateLimitsResponse).
IDENTIFIED_RESULT = {
    **RPC_RESULT,
    "accountId": "account-raw-id-0001",
    "rateLimitResetCredits": {
        "availableCount": 2,
        "credits": [
            {"id": "credit-raw-id-b", "status": "available", "grantedAt": 1,
             "expiresAt": 2_000_000_000, "resetType": "codexRateLimits"},
            {"id": "credit-raw-id-a", "status": "available", "grantedAt": 1,
             "expiresAt": None, "resetType": "codexRateLimits"},
            {"id": "credit-raw-id-c", "status": "redeemed", "grantedAt": 1,
             "expiresAt": None, "resetType": "codexRateLimits"},
        ],
    },
}


class IdentitySignalTest(unittest.TestCase):
    """docs/reset-attribution.md, "Signals each probe records"."""

    def test_every_row_names_its_account_by_fingerprint_only(self):
        rows = qp.normalise(IDENTIFIED_RESULT, now=1000)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["account"], "a")
            self.assertEqual(row["account_fp"], fp("account", "account-raw-id-0001"))
        # Not anywhere, in any field: the raw ids never enter a row.
        written = json.dumps(rows)
        for raw in ("account-raw-id-0001", "credit-raw-id-a", "credit-raw-id-b"):
            self.assertNotIn(raw, written)

    def test_an_absent_account_id_is_unread_rather_than_an_identity(self):
        for value in ("absent", None, "", 7):
            with self.subTest(account_id=value):
                result = dict(IDENTIFIED_RESULT)
                if value == "absent":
                    del result["accountId"]
                else:
                    result["accountId"] = value
                for row in qp.normalise(result, now=1000):
                    self.assertIsNone(row["account_fp"])

    def test_only_available_credits_are_itemised_by_fingerprint(self):
        row = qp.normalise(IDENTIFIED_RESULT, now=1000)[0]
        expected = sorted(
            [
                {"id": fp("credit", "credit-raw-id-a"), "expires_at": None},
                {"id": fp("credit", "credit-raw-id-b"), "expires_at": 2_000_000_000},
            ],
            key=lambda credit: credit["id"],
        )
        self.assertEqual(row["credit_ids"], expected)

    def test_the_read_asks_for_the_itemised_bank(self):
        # account/rateLimits/read takes excludeResetCreditDetails; passing it
        # true is how the detail rows would silently disappear.
        self.assertNotIn("excludeResetCreditDetails", json.dumps(qp._rpc_messages()))

    def test_the_namespace_keeps_an_account_and_a_credit_apart(self):
        self.assertNotEqual(qp.fingerprint("account", "x"), qp.fingerprint("credit", "x"))

    def test_an_unitemised_bank_is_none_and_an_empty_one_is_empty(self):
        # The schema: null means only availableCount is known, [] means the
        # details were fetched and nothing was available. Merging the two
        # would let "not itemised" read as "every credit left".
        bank = {"availableCount": 0}
        self.assertIsNone(qp.credit_details({"rateLimitResetCredits": bank}))
        self.assertIsNone(
            qp.credit_details({"rateLimitResetCredits": {**bank, "credits": None}})
        )
        self.assertEqual(qp.credit_details({"rateLimitResetCredits": {**bank, "credits": []}}), [])
        self.assertIsNone(qp.credit_details({}))
        self.assertIsNone(qp.credit_details({"rateLimitResetCredits": None}))

    def test_an_available_credit_without_an_id_unitemises_the_bank(self):
        result = {"rateLimitResetCredits": {"availableCount": 1, "credits": [
            {"status": "available", "grantedAt": 1},
        ]}}
        self.assertIsNone(qp.credit_details(result))

    def test_an_expiry_that_is_not_an_integer_is_read_as_none(self):
        for expiry in ("2026-10-11", True, 1.5):
            with self.subTest(expiry=expiry):
                result = {"rateLimitResetCredits": {"availableCount": 1, "credits": [
                    {"id": "c", "status": "available", "expiresAt": expiry},
                ]}}
                self.assertEqual(qp.credit_details(result)[0]["expires_at"], None)

    def test_each_row_carries_its_own_copy_of_the_itemisation(self):
        rows = qp.normalise(IDENTIFIED_RESULT, now=1000)
        rows[0]["credit_ids"].clear()
        self.assertEqual(len(rows[1]["credit_ids"]), 2)


# ─── Clear recognition ───────────────────────────────────────────────────────


class IsClearTest(unittest.TestCase):
    def test_full_reset_is_a_clear(self):
        # 2026-08-03: 91% -> 0%.
        self.assertTrue(qp.is_clear(sample(0, 91), sample(60, 0)))

    def test_limit_increase_is_not_a_clear(self):
        # 2026-08-16: 100% -> 55% is a denominator change, not a reset. The
        # drop alone clears the threshold, so the floor is what rejects it.
        self.assertFalse(qp.is_clear(sample(0, 100), sample(60, 55)))

    def test_small_drop_on_a_barely_used_window_is_not_a_clear(self):
        self.assertFalse(qp.is_clear(sample(0, 8), sample(60, 0)))

    def test_boundary_drop_exactly_at_threshold_counts(self):
        self.assertTrue(qp.is_clear(sample(0, 20), sample(60, 0)))

    def test_usage_growth_is_never_a_clear(self):
        self.assertFalse(qp.is_clear(sample(0, 10), sample(60, 40)))


# ─── Decision table ──────────────────────────────────────────────────────────


class ClassifyTest(unittest.TestCase):
    def test_clear_after_declared_expiry_is_natural(self):
        anchor = 10_000
        verdict = qp.classify(sample(0, 100), sample(anchor + 30, 0), anchor)
        self.assertEqual(verdict["classification"], qp.CLASS_NATURAL)

    def test_clear_just_before_expiry_is_within_grace(self):
        anchor = 10_000
        verdict = qp.classify(sample(0, 100), sample(anchor - 30, 0), anchor)
        self.assertEqual(verdict["classification"], qp.CLASS_NATURAL)

    def test_early_clear_with_spent_credit_is_self_applied(self):
        anchor = 1_000_000
        verdict = qp.classify(
            sample(0, 90, credits=1),
            sample(anchor - WEEK_SECONDS // 2, 0, credits=0),
            anchor,
        )
        self.assertEqual(verdict["classification"], qp.CLASS_SELF)
        self.assertEqual(verdict["credits_before"], 1)
        self.assertEqual(verdict["credits_after"], 0)

    def test_early_clear_with_untouched_credit_is_a_global_candidate(self):
        anchor = 1_000_000
        verdict = qp.classify(
            sample(0, 90, credits=1),
            sample(anchor - WEEK_SECONDS // 2, 0, credits=1),
            anchor,
        )
        self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)
        self.assertEqual(verdict["early_by_seconds"], WEEK_SECONDS // 2)

    def test_missing_credit_field_does_not_masquerade_as_a_spend(self):
        anchor = 1_000_000
        verdict = qp.classify(
            sample(0, 90, credits=1),
            sample(anchor - 50_000, 0, credits=None),
            anchor,
        )
        self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)

    def test_no_anchor_yet_is_reported_as_unresolved_not_natural(self):
        # ...and not as a global candidate either: with no anchor a natural
        # expiry cannot be ruled out, so "global" would be a false alarm.
        verdict = qp.classify(sample(0, 90), sample(60, 0), None)
        self.assertEqual(verdict["classification"], qp.CLASS_UNRESOLVED)
        self.assertIsNone(verdict["early_by_seconds"])


# ─── Revert versus resumed usage ─────────────────────────────────────────────


class IsRevertTest(unittest.TestCase):
    def pending(self, used_before=100.0, original_anchor=1_000_000):
        return {"used_before": used_before, "prior_active_resets_at": original_anchor}

    def test_usage_on_the_original_anchor_is_a_revert(self):
        # 2026-08-03: 92% back on the anchor that was in force before the clear.
        self.assertTrue(qp.is_revert(self.pending(91), sample(60, 92, 1_000_000)))

    def test_usage_on_a_new_anchor_is_resumed_usage(self):
        # 2026-09-02: 6% on the re-anchored window (1788748080 -> 1788926992).
        self.assertFalse(qp.is_revert(self.pending(100), sample(60, 6, 60 + WEEK_SECONDS)))

    def test_anchor_jitter_of_a_second_still_matches(self):
        # The same frozen anchor has been read as ...350 and ...351.
        self.assertTrue(qp.is_revert(self.pending(91), sample(60, 92, 1_000_001)))

    def test_idle_read_is_never_a_revert(self):
        self.assertFalse(qp.is_revert(self.pending(91), sample(60, 0, 1_000_000)))

    def test_without_anchors_a_snap_back_to_the_prior_level_is_a_revert(self):
        self.assertTrue(qp.is_revert(self.pending(91, None), sample(60, 92, None)))

    def test_without_anchors_a_small_climb_is_resumed_usage(self):
        self.assertFalse(qp.is_revert(self.pending(100, None), sample(60, 6, None)))

    def test_snap_back_floor_does_not_degenerate_on_a_lightly_used_window(self):
        # used_before - CLEAR_DROP_MIN would be 5 here, which would call any
        # resumed usage a revert; the floor must not drop below CLEAR_DROP_MIN.
        self.assertFalse(qp.is_revert(self.pending(25, None), sample(60, 6, None)))
        self.assertTrue(qp.is_revert(self.pending(25, None), sample(60, 24, None)))


# ─── Detector state machine ──────────────────────────────────────────────────


# The exact codex/10080 rows the probe wrote around the 2026-09-02 00:10 EDT
# clear: (t, used_percent, resets_at, credits). The anchor moves once, from
# 1788748080 to 1788926992 (= clear time + one week), and never returns.
LIVE_2026_09_02 = [
    (1788321429, 100, 1788748080, 1),
    (1788322209, 0, 1788926992, 0),
    (1788322749, 1, 1788926992, 0),
    (1788325029, 2, 1788926992, 0),
    (1788325689, 3, 1788926992, 0),
    (1788326109, 4, 1788926992, 0),
    (1788326529, 5, 1788926992, 0),
    (1788326949, 6, 1788926992, 0),
    (1788327609, 7, 1788926992, 0),
]


class DetectorTest(unittest.TestCase):
    def test_usage_resuming_after_a_confirmed_clear_is_not_a_revert(self):
        # The live 2026-09-02 defect: the clear was confirmed, then 79 polls
        # later the account simply started using the cleared window (6%) and
        # the detector re-emitted the same clear as "reverted".
        d = qp.Detector()
        events = []
        for t, used, anchor, credits in LIVE_2026_09_02:
            events.extend(d.observe(sample(t, used, anchor, credits=credits)))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR])
        self.assertTrue(events[0]["confirmed"])
        self.assertEqual(events[0]["classification"], qp.CLASS_SELF)
        self.assertEqual(events[0]["early_by_seconds"], 1788748080 - 1788322209)
        self.assertIsNone(d.slots["codex/10080"]["pending"])
        self.assertEqual(d.slots["codex/10080"]["active_anchor"], 1788926992)

    def test_confirmed_clear_reverting_hours_later_on_its_original_anchor_is_retracted(self):
        # 2026-08-03: 91% -> 0%, held (well past CONFIRM_POLLS), then 92% on
        # the ORIGINAL anchor. Confirmation must not close the watch.
        d = qp.Detector()
        anchor = 1_000_000
        d.observe(sample(0, 91, anchor))
        events = []
        for i in range(1, 20):  # idle reads drift to t + window
            events.extend(d.observe(sample(60 * i, 0, 60 * i + WEEK_SECONDS)))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR])
        # The first revert read is held (REVERT_POLLS): the vendor served this
        # very shape from a stale replica for one poll on 2026-09-09. A revert
        # that holds, as this one did, is withdrawn a poll later.
        self.assertEqual(d.observe(sample(60 * 20, 92, anchor)), [])
        retractions = d.observe(sample(60 * 21, 92, anchor))
        self.assertEqual(len(retractions), 1)
        self.assertEqual(retractions[0]["kind"], qp.EVENT_RETRACTION)
        self.assertFalse(retractions[0]["confirmed"])
        self.assertEqual(retractions[0]["event_id"], events[0]["event_id"])
        # When usage came back, not when the second read agreed.
        self.assertEqual(retractions[0]["reverted_at"], 60 * 20)
        self.assertEqual(retractions[0]["revert_confirmed_at"], 60 * 21)
        self.assertEqual(retractions[0]["revert_reads"], 2)
        self.assertEqual(retractions[0]["used_at_revert"], 92.0)
        self.assertNotIn("revert_read", retractions[0])
        self.assertIsNone(d.slots["codex/10080"]["pending"])

    def test_usage_resuming_on_a_new_anchor_before_the_second_idle_read_confirms(self):
        # A 5h window can pass CLEAR_FLOOR inside one poll. That is not a
        # revert (new anchor) and must not lose the clear.
        d = qp.Detector()
        d.observe(sample(0, 100, 1_000_000, window=300))
        self.assertEqual(d.observe(sample(60, 0, 60 + 300 * 60, window=300)), [])
        events = d.observe(sample(120, 6, 120 + 300 * 60, window=300))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], qp.EVENT_CLEAR)
        self.assertTrue(events[0]["confirmed"])
        self.assertEqual(events[0]["confirmed_by"], "usage_resumed_on_new_anchor")
        self.assertIsNone(d.slots["codex/300"]["pending"])
        # Nothing further once the watch is closed.
        self.assertEqual(d.observe(sample(180, 30, 120 + 300 * 60, window=300)), [])

    def test_event_id_is_stable_across_the_clear_and_its_retraction(self):
        d = qp.Detector()
        d.observe(sample(0, 91, 1_000_000))
        d.observe(sample(60, 0, 1_000_000))
        clear = d.observe(sample(120, 0, 120 + WEEK_SECONDS))[0]
        self.assertEqual(d.observe(sample(180, 92, 1_000_000)), [])  # held
        retraction = d.observe(sample(240, 92, 1_000_000))[0]
        self.assertEqual(clear["event_id"], "codex/10080:60")
        self.assertEqual(retraction["event_id"], clear["event_id"])

    def test_confirmed_global_reset_emits_once(self):
        d = qp.Detector()
        anchor = 1_000_000
        self.assertEqual(d.observe(sample(0, 90, anchor)), [])
        self.assertEqual(d.observe(sample(60, 0, anchor)), [])  # pending, 1 confirm
        events = d.observe(sample(120, 0, 120 + WEEK_SECONDS))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["confirmed"])
        self.assertEqual(events[0]["classification"], qp.CLASS_GLOBAL)
        # A third cleared read must not re-emit.
        self.assertEqual(d.observe(sample(180, 0, 180 + WEEK_SECONDS)), [])

    def test_transient_clear_that_reverts_is_reported_as_unconfirmed(self):
        # 2026-08-03: 91% -> 0%, held, then back to 92% on the original anchor.
        d = qp.Detector()
        anchor = 1_000_000
        d.observe(sample(0, 91, anchor))
        d.observe(sample(60, 0, anchor))
        # An unconfirmed clear waits for the second revert read too: the one
        # rule is simpler to reason about than a rule per confirmation state.
        self.assertEqual(d.observe(sample(120, 92, anchor)), [])
        events = d.observe(sample(180, 92, anchor))
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["confirmed"])
        self.assertEqual(events[0]["reverted_at"], 120)

    def test_anchor_comes_from_the_active_window_not_the_idle_drift(self):
        # Once usage reads 0 the anchor tracks our own polling, so an idle
        # window's drifting resets_at must never become the comparison basis.
        d = qp.Detector()
        active_anchor = 500_000
        d.observe(sample(0, 40, active_anchor))
        d.observe(sample(60, 0, 60 + WEEK_SECONDS))
        d.observe(sample(120, 0, 120 + WEEK_SECONDS))
        self.assertEqual(d.slots["codex/10080"]["active_anchor"], active_anchor)

    def test_usage_returning_resets_the_anchor_and_clears_pending(self):
        d = qp.Detector()
        d.observe(sample(0, 40, 500_000))
        d.observe(sample(60, 0, 60 + WEEK_SECONDS))
        d.observe(sample(120, 25, 900_000))
        state = d.slots["codex/10080"]
        self.assertIsNone(state["pending"])
        self.assertEqual(state["active_anchor"], 900_000)

    def test_a_snap_back_resets_the_anchor_and_clears_pending_on_its_second_read(self):
        # The case above used to read 55%, which is a snap-back to the 40%
        # held before the clear (is_revert), so it was a single-read
        # retraction all along. Under REVERT_POLLS the first such read is held
        # with the watch still open; the second withdraws the clear.
        d = qp.Detector()
        d.observe(sample(0, 40, 500_000))
        d.observe(sample(60, 0, 60 + WEEK_SECONDS))
        self.assertEqual(d.observe(sample(120, 55, 900_000)), [])
        state = d.slots["codex/10080"]
        self.assertEqual(state["pending"]["revert_read"]["t"], 120)
        self.assertEqual(state["active_anchor"], 900_000)
        events = d.observe(sample(180, 55, 900_000))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_RETRACTION])
        self.assertIsNone(state["pending"])

    def test_slots_are_tracked_independently(self):
        d = qp.Detector()
        d.observe(sample(0, 90, 1_000_000))
        d.observe(sample(0, 90, 1_000_000, limit_id="codex_secondary", window=300))
        d.observe(sample(60, 0, 1_000_000))
        events = d.observe(sample(120, 0, 999))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["limit_id"], "codex")

    def test_cursor_round_trips_through_json(self):
        d = qp.Detector()
        d.observe(sample(0, 90, 1_000_000))
        d.observe(sample(60, 0, 1_000_000))
        restored = qp.Detector(json.loads(json.dumps(d.cursor())))
        events = restored.observe(sample(120, 0, 999))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["confirmed"])

    def test_first_ever_sample_produces_no_event(self):
        self.assertEqual(qp.Detector().observe(sample(0, 0, 1_000)), [])


# ─── Stale replica reads ─────────────────────────────────────────────────────


# The codex/10080 rows the probe wrote for account a from the last reading
# before the 2026-09-08 01:22:36 UTC clear to 2026-09-09 18:14:40, verbatim:
# (t, used_percent, resets_at, credits). The clear's own event names
# 1789191176 as the anchor in force before it. On 09-09 that anchor came back
# twice at the pre-clear 92%, each time followed by the post-clear anchor
# again: 16:56:40 -> 17:40:40 (no row between, so one read and a blind gap or
# 44 identical reads; the log cannot say), and 18:13:40 -> 18:14:40, provably
# ONE read, since the polls are a minute apart. A stale replica, not the clear
# un-happening: the account kept using the cleared window (19%) on both sides.
LIVE_2026_09_08_CLEAR = [
    (1788818196, 92, 1789191176, 1),  # 09-07 21:56:36, last row before the clear
    (1788830556, 0, 1789435357, 1),  # 09-08 01:22:36, the clear
    (1788834936, 1, 1789438339, 1),
    (1788836020, 2, 1789438339, 1),
    (1788836800, 3, 1789438339, 1),
    (1788837220, 4, 1789438339, 1),
    (1788837820, 5, 1789438339, 1),  # 03:23:40, the last idle read
    (1788838960, 6, 1789438339, 1),  # 03:42:40, usage resumed: the watch closed
    (1788840640, 7, 1789438339, 1),
    (1788841420, 8, 1789438339, 1),
    (1788841960, 9, 1789438339, 1),
    (1788845800, 10, 1789438339, 1),
    (1788849400, 11, 1789438339, 1),
    (1788853120, 12, 1789438339, 1),
    (1788910840, 13, 1789438339, 1),
    (1788912400, 14, 1789438339, 1),
    (1788912760, 15, 1789438339, 1),
    (1788913540, 16, 1789438339, 1),
    (1788934180, 17, 1789438339, 1),
    (1788938500, 18, 1789438339, 1),
    (1788964900, 19, 1789438339, 1),  # 09-09 14:41:40
]
LIVE_2026_09_09_FLAPS = [
    [(1788973000, 92, 1789191176, 1), (1788975640, 19, 1789438339, 1)],  # 16:56:40
    [(1788977620, 92, 1789191176, 1), (1788977680, 19, 1789438339, 1)],  # 18:13:40
]
PRE_CLEAR_ANCHOR = 1789191176


class StaleReplicaTest(unittest.TestCase):
    """One revert read is not a revert (REVERT_POLLS, 2026-09-09)."""

    def replay(self, rows):
        d = qp.Detector()
        return d, feed(d, [sample(t, used, anchor, credits=c) for t, used, anchor, c in rows])

    def test_the_2026_09_09_reads_cannot_withdraw_a_clear_still_under_watch(self):
        # On the day the watch had closed 39 hours earlier, at 6%. This is the
        # path the 2026-10-11 backtest named: the same reads, verbatim, landing
        # while the cleared window still idled (the rows from 6% to 19% left
        # out). With one read enough, each flap retracted the 09-08 clear.
        idle = LIVE_2026_09_08_CLEAR[:7]
        for flap in LIVE_2026_09_09_FLAPS:
            with self.subTest(at=flap[0][0]):
                d, events = self.replay(idle + flap)
                self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR])
                self.assertEqual(events[0]["prior_active_resets_at"], PRE_CLEAR_ANCHOR)
                # The stale read itself is the revert signature exactly.
                self.assertTrue(
                    qp.is_revert(events[0], sample(flap[0][0], 92, PRE_CLEAR_ANCHOR))
                )
                # 19% on the post-clear anchor is usage resuming: watch closed.
                self.assertIsNone(d.slots["codex/10080"]["pending"])

    def test_one_stale_read_while_idle_leaves_the_clear_and_its_id_alone(self):
        # The backtest's scratch scenario: confirmed clear, idling at 2%, one
        # read of the pre-clear level on the pre-clear anchor at minute 200.
        d = qp.Detector()
        old = 1_000_000
        feed(d, [sample(0, 80, old), sample(60, 0, 60 + WEEK_SECONDS)])
        clear = d.observe(sample(120, 2, 120 + WEEK_SECONDS))[0]
        self.assertTrue(clear["confirmed"])
        self.assertEqual(d.observe(sample(60 * 200, 80, old)), [])
        # The idle read after it is neither a retraction nor a fresh 80 -> 2
        # clear under a new id (which would re-key a live incident).
        self.assertEqual(d.observe(sample(60 * 201, 2, 60 * 201 + WEEK_SECONDS)), [])
        pending = d.slots["codex/10080"]["pending"]
        self.assertEqual(pending["event_id"], clear["event_id"])
        self.assertNotIn("revert_read", pending)

    def test_a_read_between_two_revert_reads_starts_the_count_again(self):
        d = qp.Detector()
        old = 1_000_000
        feed(d, [sample(0, 80, old), sample(60, 0, 60 + WEEK_SECONDS),
                 sample(120, 0, 120 + WEEK_SECONDS)])
        rows = [
            sample(180, 80, old),                     # revert, held
            sample(240, 0, 240 + WEEK_SECONDS),       # idle: count restarts
            sample(300, 80, old),                     # revert, held again
            sample(360, 1, 360 + WEEK_SECONDS),       # idle
            sample(420, 92, old),                     # revert, held
        ]
        self.assertEqual(feed(d, rows), [])
        events = d.observe(sample(480, 92, old))      # ...and held: withdrawn
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_RETRACTION])
        self.assertEqual(events[0]["reverted_at"], 420)

    def test_a_held_read_survives_a_restart(self):
        d = qp.Detector()
        old = 1_000_000
        feed(d, [sample(0, 80, old), sample(60, 0, 60 + WEEK_SECONDS),
                 sample(120, 0, 120 + WEEK_SECONDS), sample(180, 80, old)])
        restored = qp.Detector(json.loads(json.dumps(d.cursor())))
        events = restored.observe(sample(240, 80, old))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_RETRACTION])
        self.assertEqual(events[0]["reverted_at"], 180)

    def test_a_held_read_is_logged_for_the_owner(self):
        d = qp.Detector()
        feed(d, [sample(0, 80, 1_000_000), sample(60, 0, 60 + WEEK_SECONDS)])
        with captured_log() as out:
            d.observe(sample(120, 80, 1_000_000))
        self.assertIn("REVERT READ held codex/10080 80%", out.getvalue())


# ─── Sample de-duplication ───────────────────────────────────────────────────


class ChangedRowsTest(unittest.TestCase):
    def test_first_sample_is_always_kept(self):
        d = qp.Detector()
        self.assertEqual(len(qp.changed_rows([sample(0, 14, 1_000)], d)), 1)

    def test_idle_window_anchor_drift_is_not_logged(self):
        # Every poll advances an idle window's resets_at; logging that would
        # write a row a minute forever while saying nothing.
        d = qp.Detector()
        d.observe(sample(0, 0, 1_000))
        self.assertEqual(qp.changed_rows([sample(60, 0, 1_060)], d), [])

    def test_active_window_anchor_change_is_logged(self):
        d = qp.Detector()
        d.observe(sample(0, 40, 1_000))
        self.assertEqual(len(qp.changed_rows([sample(60, 40, 2_000)], d)), 1)

    def test_usage_change_is_logged(self):
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000))
        self.assertEqual(len(qp.changed_rows([sample(60, 15, 1_000)], d)), 1)

    def test_credit_bank_change_is_logged(self):
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, credits=1))
        self.assertEqual(len(qp.changed_rows([sample(60, 14, 1_000, credits=0)], d)), 1)

    def test_unchanged_active_row_is_dropped(self):
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000))
        self.assertEqual(qp.changed_rows([sample(60, 14, 1_000)], d), [])

    def test_credit_field_blinking_out_is_not_a_change(self):
        # 159 of 368 rows on 2026-09-03/04 flapped between an int and None,
        # and the raw comparison wrote a sample row on every one of them.
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, credits=1))
        self.assertEqual(qp.changed_rows([sample(60, 14, 1_000, credits=None)], d), [])

    def test_credit_field_returning_to_the_same_count_is_not_a_change(self):
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, credits=1))
        d.observe(sample(60, 14, 1_000, credits=None))
        self.assertEqual(qp.changed_rows([sample(120, 14, 1_000, credits=1)], d), [])

    def test_a_real_credit_move_seen_across_a_flap_is_still_logged(self):
        # 1 -> None -> 0 is a spend, not a flap, and must reach the log.
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, credits=1))
        d.observe(sample(60, 14, 1_000, credits=None))
        self.assertEqual(len(qp.changed_rows([sample(120, 14, 1_000, credits=0)], d)), 1)

    def test_an_account_switch_is_logged_even_with_nothing_else_moving(self):
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, account_fp="aaaaaaaaaaaa"))
        moved = qp.changed_rows([sample(60, 14, 1_000, account_fp="bbbbbbbbbbbb")], d)
        self.assertEqual(len(moved), 1)

    def test_an_unreadable_identity_is_not_a_change(self):
        # Same rule as the credit field: "unread" is not a new account, and a
        # flap must not write a row on each side of it.
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, account_fp="aaaaaaaaaaaa"))
        self.assertEqual(qp.changed_rows([sample(60, 14, 1_000, account_fp=None)], d), [])
        d.observe(sample(60, 14, 1_000, account_fp=None))
        self.assertEqual(
            qp.changed_rows([sample(120, 14, 1_000, account_fp="aaaaaaaaaaaa")], d), []
        )
        # ...while a different account seen across the flap still is one.
        self.assertEqual(
            len(qp.changed_rows([sample(120, 14, 1_000, account_fp="bbbbbbbbbbbb")], d)), 1
        )

    def test_the_first_fingerprinted_row_after_an_upgrade_is_logged(self):
        # A cursor written before account_fp existed: the first row that has
        # one is the first identity the log will ever hold.
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000))
        self.assertEqual(
            len(qp.changed_rows([sample(60, 14, 1_000, account_fp="aaaaaaaaaaaa")], d)), 1
        )

    def test_a_credit_swap_the_count_cannot_see_is_logged(self):
        # A grant and a redemption between two polls cancel in the count.
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, credits=1, credit_ids=[{"id": "c1", "expires_at": None}]))
        moved = qp.changed_rows(
            [sample(60, 14, 1_000, credits=1, credit_ids=[{"id": "c2", "expires_at": None}])], d
        )
        self.assertEqual(len(moved), 1)

    def test_itemisation_blinking_out_is_not_a_change(self):
        d = qp.Detector()
        ids = [{"id": "c1", "expires_at": None}]
        d.observe(sample(0, 14, 1_000, credits=1, credit_ids=ids))
        self.assertEqual(qp.changed_rows([sample(60, 14, 1_000, credits=1, credit_ids=None)], d), [])
        d.observe(sample(60, 14, 1_000, credits=1, credit_ids=None))
        self.assertEqual(
            qp.changed_rows([sample(120, 14, 1_000, credits=1, credit_ids=list(ids))], d), []
        )

    def test_the_field_going_absent_for_good_is_logged_once_it_is_stale(self):
        # Past the carry window we stop claiming to know the count; that
        # transition from "1" to "unknown" is itself worth one row.
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000, credits=1))
        stale = qp.CREDITS_MAX_AGE_POLLS * qp.POLL_SECONDS + 60
        self.assertEqual(
            len(qp.changed_rows([sample(stale, 14, 1_000, credits=None)], d)), 1
        )


# ─── Upstream snapshot de-duplication ────────────────────────────────────────


class DedupeUpstreamTest(unittest.TestCase):
    """These feeds are polled far faster than they change.

    Measured over the probe's first 29h: codex-reset.com repeated itself on
    98.3% of writes and codex-resets.com on 40.3%, together projecting to
    331 MB/year of pure redundancy.
    """

    def snap(self, source="codex-reset.com", t=0, **body):
        return {"t": t, "source": source, "status": 200, **body}

    def test_first_snapshot_is_kept(self):
        sigs = {}
        self.assertEqual(len(qp.dedupe_upstream([self.snap(watch="a")], sigs)), 1)

    def test_identical_content_at_a_later_time_is_dropped(self):
        sigs = {}
        qp.dedupe_upstream([self.snap(t=0, watch="a")], sigs)
        self.assertEqual(qp.dedupe_upstream([self.snap(t=300, watch="a")], sigs), [])

    def test_changed_content_is_kept(self):
        sigs = {}
        qp.dedupe_upstream([self.snap(t=0, watch="a")], sigs)
        kept = qp.dedupe_upstream([self.snap(t=300, watch="b")], sigs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["t"], 300)

    def test_content_returning_to_an_earlier_value_is_kept(self):
        # Only the immediately preceding state is compared, so a flap back to
        # a previous value is a real transition and must be recorded.
        sigs = {}
        qp.dedupe_upstream([self.snap(t=0, watch="a")], sigs)
        qp.dedupe_upstream([self.snap(t=300, watch="b")], sigs)
        self.assertEqual(len(qp.dedupe_upstream([self.snap(t=600, watch="a")], sigs)), 1)

    def test_sources_are_tracked_separately(self):
        sigs = {}
        both = [self.snap(source="a", watch="x"), self.snap(source="b", watch="x")]
        self.assertEqual(len(qp.dedupe_upstream(both, sigs)), 2)
        self.assertEqual(len(sigs), 2)

    def test_errors_are_always_kept(self):
        # A failed fetch says nothing about upstream content, and suppressing
        # it would corrupt the "this state held until the next row" reading.
        sigs = {}
        err = {"t": 0, "source": "codex-reset.com", "error": "URLError: boom"}
        self.assertEqual(len(qp.dedupe_upstream([err], sigs)), 1)
        self.assertEqual(len(qp.dedupe_upstream([dict(err, t=300)], sigs)), 1)

    def test_an_error_does_not_poison_the_stored_signature(self):
        sigs = {}
        qp.dedupe_upstream([self.snap(t=0, watch="a")], sigs)
        qp.dedupe_upstream([{"t": 300, "source": "codex-reset.com", "error": "boom"}], sigs)
        # Same content as before the error: still redundant, still dropped.
        self.assertEqual(qp.dedupe_upstream([self.snap(t=600, watch="a")], sigs), [])

    def test_signature_ignores_timestamp_only(self):
        a = qp.upstream_signature({"t": 1, "source": "s", "watch": {"n": 1}})
        b = qp.upstream_signature({"t": 999, "source": "s", "watch": {"n": 1}})
        c = qp.upstream_signature({"t": 1, "source": "s", "watch": {"n": 2}})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_signature_is_stable_across_key_order(self):
        a = qp.upstream_signature({"t": 1, "source": "s", "x": 1, "y": 2})
        b = qp.upstream_signature({"y": 2, "x": 1, "source": "s", "t": 1})
        self.assertEqual(a, b)

    def test_signatures_survive_a_json_round_trip(self):
        sigs = {}
        qp.dedupe_upstream([self.snap(t=0, watch="a")], sigs)
        restored = json.loads(json.dumps(sigs))
        self.assertEqual(qp.dedupe_upstream([self.snap(t=300, watch="a")], restored), [])


# ─── Persistence ─────────────────────────────────────────────────────────────


class PersistenceTest(unittest.TestCase):
    def test_append_jsonl_creates_parents_and_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "out.jsonl"
            qp.append_jsonl(path, [{"a": 1}])
            qp.append_jsonl(path, [{"a": 2}, {"a": 3}])
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual([json.loads(x)["a"] for x in lines], [1, 2, 3])

    def test_append_jsonl_noop_on_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            self.assertEqual(qp.append_jsonl(path, []), 0)
            self.assertFalse(path.exists())

    def test_missing_cursor_reads_as_empty(self):
        original = qp.CURSOR_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                qp.CURSOR_FILE = Path(tmp) / "absent.json"
                self.assertEqual(qp.load_cursor(), {})
        finally:
            qp.CURSOR_FILE = original

    def test_corrupt_cursor_reads_as_empty_rather_than_crashing(self):
        original = qp.CURSOR_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                qp.CURSOR_FILE = Path(tmp) / "cursor.json"
                qp.CURSOR_FILE.write_text("{not json", encoding="utf-8")
                self.assertEqual(qp.load_cursor(), {})
        finally:
            qp.CURSOR_FILE = original

    def test_cursor_save_and_load_round_trip(self):
        original = qp.CURSOR_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                qp.CURSOR_FILE = Path(tmp) / "cursor.json"
                qp.save_cursor({"slots": {"codex/10080": {"active_anchor": 7}}})
                self.assertEqual(qp.load_cursor()["slots"]["codex/10080"]["active_anchor"], 7)
        finally:
            qp.CURSOR_FILE = original


# ─── Blind-gap clears ────────────────────────────────────────────────────────


# The blind-gap shape, with anchors that could really occur: a weekly window
# anchored 4.6 days out, then two hours of silence, then an active read whose
# anchor sits a full week from the recovery poll.
GAP_BEFORE_T = 1_000_000
GAP_AFTER_T = GAP_BEFORE_T + 7200
GAP_ANCHOR = GAP_BEFORE_T + 400_000
GAP_NEW_ANCHOR = GAP_AFTER_T + WEEK_SECONDS


class IsAnchorJumpClearTest(unittest.TestCase):
    """100% -> [35 blind polls] -> 8%: the drop is only visible in the anchor."""

    def test_big_drop_with_a_forward_anchor_is_a_clear(self):
        self.assertTrue(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR),
                sample(GAP_AFTER_T, 8, GAP_NEW_ANCHOR),
            )
        )

    def test_the_same_anchor_is_not_a_clear(self):
        # Ordinary usage cannot fall, but a limit rescale can (100 -> 55 on
        # 2026-08-16) and it keeps its anchor. Rejected twice over.
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR), sample(GAP_AFTER_T, 55, GAP_ANCHOR)
            )
        )
        # ...and jitter of a few seconds on a frozen anchor is still the same
        # anchor (the server has read it as both ...350 and ...351).
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR),
                sample(GAP_AFTER_T, 8, GAP_ANCHOR + 5),
            )
        )

    def test_an_anchor_moving_backward_is_not_a_clear(self):
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR),
                sample(GAP_AFTER_T, 8, GAP_ANCHOR - 100_000),
            )
        )

    def test_an_anchor_move_inside_the_natural_grace_is_not_a_clear(self):
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR),
                sample(GAP_AFTER_T, 8, GAP_ANCHOR + qp.NATURAL_GRACE_SECONDS),
            )
        )

    def test_a_cleared_reading_is_left_to_is_clear(self):
        # Both ends must be ACTIVE; a read at the floor is the ordinary
        # signature and must not be counted twice.
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR),
                sample(GAP_AFTER_T, 0, GAP_NEW_ANCHOR),
            )
        )
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 4, GAP_ANCHOR),
                sample(GAP_AFTER_T, 40, GAP_NEW_ANCHOR),
            )
        )

    def test_a_small_drop_is_not_a_clear(self):
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 20, GAP_ANCHOR),
                sample(GAP_AFTER_T, 15, GAP_NEW_ANCHOR),
            )
        )

    def test_a_missing_anchor_on_either_side_is_not_a_clear(self):
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, None), sample(GAP_AFTER_T, 8, GAP_NEW_ANCHOR)
            )
        )

    def test_an_anchor_the_window_held_before_the_gap_is_not_a_clear(self):
        # 2026-09-09, as the sample log holds it: a stale replica's 92% on the
        # pre-reset anchor, then 44 minutes on, 19% on the anchor the account
        # had held since its 09-08 reset. Forward by 2.9 days, yet short of a
        # window past anything inside the gap: the window went BACK to it.
        stale, back = LIVE_2026_09_09_FLAPS[0]
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(stale[0], stale[1], stale[2]), sample(back[0], back[1], back[2])
            )
        )
        # The boundary: a whole window past the read that opened the gap,
        # less the server's rounding of a frozen anchor.
        lattice = GAP_BEFORE_T + WEEK_SECONDS - qp.ANCHOR_JITTER_SECONDS
        for anchor, expected in ((lattice, True), (lattice - 1, False)):
            with self.subTest(short_by=GAP_BEFORE_T + WEEK_SECONDS - anchor):
                self.assertEqual(
                    qp.is_anchor_jump_clear(
                        sample(GAP_BEFORE_T, 100, GAP_ANCHOR), sample(GAP_AFTER_T, 8, anchor)
                    ),
                    expected,
                )
        # Anthropic's lattice rolls on by exactly one window at expiry, and the
        # 5-hour window is held to its own length, not the week's.
        five_hours = 300 * 60
        self.assertTrue(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_BEFORE_T + 600, window=300),
                sample(GAP_AFTER_T, 8, GAP_BEFORE_T + 600 + five_hours, window=300),
            )
        )
        self.assertFalse(
            qp.is_anchor_jump_clear(
                sample(GAP_BEFORE_T, 100, GAP_ANCHOR), sample(GAP_AFTER_T, 8, None)
            )
        )


class DetectorAnchorJumpTest(unittest.TestCase):
    def test_a_clear_inside_a_blind_gap_is_recovered(self):
        # The 2026-09-03 outage shape: 35 polls missing, and by the time the
        # probe reads again the account is already back to 8% on a window that
        # re-anchored a week out.
        d = qp.Detector()
        d.observe(sample(GAP_BEFORE_T, 100, GAP_ANCHOR))
        events = d.observe(sample(GAP_AFTER_T, 8, GAP_NEW_ANCHOR))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["kind"], qp.EVENT_CLEAR)
        self.assertTrue(event["confirmed"])
        self.assertEqual(event["confirmed_by"], "anchor_jump")
        self.assertTrue(event["reanchored"])
        self.assertEqual(event["classification"], qp.CLASS_GLOBAL)
        # The clear happened somewhere inside the gap, so the bracket spans it.
        self.assertEqual(event["clear_bracket_lo"], GAP_BEFORE_T)
        self.assertEqual(event["clear_bracket_hi"], GAP_AFTER_T)
        self.assertEqual(event["early_by_seconds"], GAP_ANCHOR - GAP_AFTER_T)

    def test_no_watch_is_opened_so_resumed_usage_cannot_retract_it(self):
        # An active window plus a revert watch is the 2026-09-02 false
        # retraction waiting to happen: usage climbing back toward 100% would
        # read as a snap-back.
        d = qp.Detector()
        d.observe(sample(GAP_BEFORE_T, 100, GAP_ANCHOR))
        d.observe(sample(GAP_AFTER_T, 8, GAP_NEW_ANCHOR))
        self.assertIsNone(d.slots["codex/10080"].get("pending"))
        self.assertEqual(d.observe(sample(GAP_AFTER_T + 60, 95, GAP_NEW_ANCHOR)), [])

    def test_a_spent_credit_across_the_gap_is_still_self_applied(self):
        d = qp.Detector()
        d.observe(sample(GAP_BEFORE_T, 100, GAP_ANCHOR, credits=1))
        events = d.observe(sample(GAP_AFTER_T, 8, GAP_NEW_ANCHOR, credits=0))
        self.assertEqual(events[0]["classification"], qp.CLASS_SELF)

    def test_an_anchor_jump_while_a_clear_is_pending_does_not_double_emit(self):
        # Usage resuming on the new anchor confirms the pending clear; it must
        # not also be counted as a second, independent clear.
        d = qp.Detector()
        d.observe(sample(GAP_BEFORE_T, 100, GAP_ANCHOR))
        d.observe(sample(GAP_BEFORE_T + 60, 0, GAP_BEFORE_T + 60 + WEEK_SECONDS))
        events = d.observe(sample(GAP_BEFORE_T + 120, 40, GAP_BEFORE_T + 120 + WEEK_SECONDS))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR])
        self.assertEqual(events[0]["confirmed_by"], "usage_resumed_on_new_anchor")
        self.assertEqual(
            d.observe(sample(GAP_BEFORE_T + 180, 20, GAP_BEFORE_T + 180 + WEEK_SECONDS)),
            [],
        )

    def test_the_2026_09_09_flaps_as_recorded_are_not_a_clear(self):
        # Every codex/10080 row the sample log holds from before the 09-08
        # clear to 09-09 18:14:40, replayed as written: the one clear, kept,
        # and nothing from either flap. Replaying the whole log this way
        # yields every clear in docs/reset-attribution.md's table and, before
        # the window-past-the-gap test, this one phantom besides.
        rows = LIVE_2026_09_08_CLEAR + [r for flap in LIVE_2026_09_09_FLAPS for r in flap]
        d = qp.Detector()
        events = feed(d, [sample(t, used, anchor, credits=c) for t, used, anchor, c in rows])
        self.assertEqual([e["event_id"] for e in events], ["codex/10080:1788830556"])
        self.assertTrue(events[0]["confirmed"])

    def test_the_live_sample_stream_gains_no_extra_clears(self):
        # Regression guard for the replay: the 2026-09-02 rows re-anchor once
        # and then simply accumulate usage, which must stay one event.
        d = qp.Detector()
        events = feed(
            d,
            [
                sample(t, used, anchor, credits=credits)
                for t, used, anchor, credits in LIVE_2026_09_02
            ],
        )
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR])


# ─── Banked-credit carry-forward ─────────────────────────────────────────────


class ResolveCreditsTest(unittest.TestCase):
    def known(self, value=1, t=0):
        return {"value": value, "t": t}

    def test_an_int_reading_is_used_directly(self):
        self.assertEqual(qp.resolve_credits(0, self.known(1), 60), (0, False))

    def test_an_absent_reading_falls_back_to_the_last_int(self):
        self.assertEqual(qp.resolve_credits(None, self.known(1), 60), (1, True))

    def test_a_stale_count_is_not_carried(self):
        stale = qp.CREDITS_MAX_AGE_POLLS * qp.POLL_SECONDS + 1
        self.assertEqual(qp.resolve_credits(None, self.known(1), stale), (None, False))

    def test_the_carry_window_boundary_is_inclusive(self):
        edge = qp.CREDITS_MAX_AGE_POLLS * qp.POLL_SECONDS
        self.assertEqual(qp.resolve_credits(None, self.known(1), edge), (1, True))

    def test_nothing_known_stays_unknown(self):
        self.assertEqual(qp.resolve_credits(None, None, 60), (None, False))
        self.assertEqual(qp.resolve_credits(None, {}, 60), (None, False))

    def test_a_carried_zero_stays_zero_not_absent(self):
        # None and 0 must stay distinct in both directions.
        self.assertEqual(qp.resolve_credits(None, self.known(0), 60), (0, True))


class CreditCarryForwardTest(unittest.TestCase):
    def test_a_spend_is_seen_even_when_the_earlier_reading_blinked_out(self):
        # The dangerous shape: the row before the clear read None, so the raw
        # comparison has no "before" and calls the owner's own spend a
        # vendor-wide reset.
        anchor = 1_000_000
        verdict = qp.classify(
            sample(950_000, 90, credits=None),
            sample(950_060, 0, credits=0),
            anchor,
            {"value": 1, "t": 949_940},
        )
        self.assertEqual(verdict["classification"], qp.CLASS_SELF)
        self.assertEqual((verdict["credits_before"], verdict["credits_after"]), (1, 0))
        self.assertTrue(verdict["credits_carried"])

    def test_a_stale_count_does_not_invent_a_spend(self):
        # Past the carry window the bank could have moved unseen, so the
        # before side is unknown and the verdict may not lean on it.
        anchor = 1_000_000
        stale_t = 950_000 - qp.CREDITS_MAX_AGE_POLLS * qp.POLL_SECONDS - 1
        verdict = qp.classify(
            sample(950_000, 90, credits=None),
            sample(950_060, 0, credits=0),
            anchor,
            {"value": 1, "t": stale_t},
        )
        self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)
        self.assertIsNone(verdict["credits_before"])

    def test_an_unchanged_carried_count_is_reported_as_carried(self):
        anchor = 1_000_000
        verdict = qp.classify(
            sample(950_000, 90, credits=1),
            sample(950_060, 0, credits=None),
            anchor,
            {"value": 1, "t": 950_000},
        )
        self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)
        self.assertTrue(verdict["credits_carried"])
        self.assertIn("last readable count", verdict["reason"])

    def test_a_late_credit_reading_corrects_a_clear_before_it_is_published(self):
        # The clear row read None; the next row reads 0. Publishing the first
        # verdict would put the owner's banked credit out as a vendor reset.
        d = qp.Detector()
        anchor = 1_000_000
        d.observe(sample(0, 90, anchor, credits=1))
        self.assertEqual(d.observe(sample(60, 0, anchor, credits=None)), [])
        events = d.observe(sample(120, 0, 120 + WEEK_SECONDS, credits=0))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["classification"], qp.CLASS_SELF)
        self.assertEqual(events[0]["credits_after"], 0)
        self.assertIn("after the clear", events[0]["reason"])

    def test_a_late_reading_cannot_turn_a_self_applied_clear_global(self):
        d = qp.Detector()
        anchor = 1_000_000
        d.observe(sample(0, 90, anchor, credits=1))
        d.observe(sample(60, 0, anchor, credits=0))
        events = d.observe(sample(120, 0, 120 + WEEK_SECONDS, credits=1))
        self.assertEqual(events[0]["classification"], qp.CLASS_SELF)

    def test_an_already_published_clear_is_not_reclassified(self):
        # Correcting a record subscribers have already been mailed is the
        # notifier's job (a retraction), never a silent rewrite here.
        d = qp.Detector()
        anchor = 1_000_000
        d.observe(sample(0, 90, anchor, credits=1))
        d.observe(sample(60, 0, anchor, credits=1))
        published = d.observe(sample(120, 0, 120 + WEEK_SECONDS, credits=1))[0]
        self.assertEqual(published["classification"], qp.CLASS_GLOBAL)
        self.assertEqual(d.observe(sample(180, 0, 180 + WEEK_SECONDS, credits=0)), [])
        self.assertEqual(
            d.slots["codex/10080"]["pending"]["classification"], qp.CLASS_GLOBAL
        )

    def test_every_int_reading_updates_the_known_count(self):
        d = qp.Detector()
        feed(
            d,
            [
                sample(0, 14, 1_000, credits=1),
                sample(60, 14, 1_000, credits=None),
                sample(120, 14, 1_000, credits=0),
                sample(180, 14, 1_000, credits=None),
            ],
        )
        self.assertEqual(
            d.slots["codex/10080"]["credits_known"], {"value": 0, "t": 120}
        )

    def test_a_long_absence_is_logged_exactly_once(self):
        d = qp.Detector()
        with captured_log() as out:
            feed(d, [sample(60 * i, 14, 1_000, credits=None) for i in range(1, 25)])
        lines = [l for l in out.getvalue().splitlines() if "CREDIT FIELD ABSENT" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"CREDIT FIELD ABSENT n={qp.CREDITS_ABSENT_LOG_AFTER + 1}", lines[0])

    def test_a_readable_row_restarts_the_absence_count(self):
        d = qp.Detector()
        with captured_log() as out:
            feed(d, [sample(60 * i, 14, 1_000, credits=None) for i in range(1, 9)])
            feed(d, [sample(600, 14, 1_000, credits=1)])
            feed(d, [sample(600 + 60 * i, 14, 1_000, credits=None) for i in range(1, 9)])
        self.assertNotIn("CREDIT FIELD ABSENT", out.getvalue())
        self.assertEqual(d.slots["codex/10080"]["credits_absent"], 8)


class CreditGrantedTest(unittest.TestCase):
    def test_a_bank_increase_is_emitted_as_an_observation(self):
        d = qp.Detector()
        d.observe(sample(0, 52, 1_000, credits=0))
        events = d.observe(sample(60, 52, 1_000, credits=1))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["kind"], qp.EVENT_CREDIT_GRANTED)
        self.assertEqual(event["event_id"], "codex/10080:credit:60")
        self.assertEqual(event["slot"], "codex/10080")
        self.assertEqual(event["limit_id"], "codex")
        self.assertEqual(event["t"], 60)
        self.assertEqual((event["credits_before"], event["credits_after"]), (0, 1))
        # An observation, not a verdict, and no watch to reconcile later.
        self.assertNotIn("classification", event)
        self.assertIsNone(d.slots["codex/10080"].get("pending"))

    def test_the_live_2026_09_03_grant(self):
        # 0 -> 1 at 22:39:09 EDT with the weekly window at 52%, about 3h27m
        # after a post saying a banked reset would land.
        d = qp.Detector()
        d.observe(sample(1788504549, 52, 1788926993, credits=0))
        events = d.observe(sample(1788504549 + 60, 52, 1788926993, credits=1))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CREDIT_GRANTED])

    def test_a_grant_read_across_a_blank_row_is_emitted_once(self):
        d = qp.Detector()
        events = feed(
            d,
            [
                sample(0, 52, 1_000, credits=0),
                sample(60, 52, 1_000, credits=None),
                sample(120, 52, 1_000, credits=1),
                sample(180, 52, 1_000, credits=1),
            ],
        )
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CREDIT_GRANTED])
        self.assertEqual(events[0]["t"], 120)

    def test_a_spend_is_never_a_grant(self):
        d = qp.Detector()
        d.observe(sample(0, 52, 1_000, credits=1))
        self.assertEqual(d.observe(sample(60, 52, 1_000, credits=0)), [])

    def test_the_first_reading_of_all_is_not_a_grant(self):
        d = qp.Detector()
        self.assertEqual(d.observe(sample(0, 52, 1_000, credits=1)), [])

    def test_a_grant_on_the_row_that_clears_still_reports_both(self):
        d = qp.Detector()
        anchor = 1_000_000
        d.observe(sample(0, 90, anchor, credits=0))
        # The clear itself is pending here, so only the observation surfaces.
        events = d.observe(sample(60, 0, 60 + WEEK_SECONDS, credits=1))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CREDIT_GRANTED])
        self.assertIsNotNone(d.slots["codex/10080"]["pending"])


# ─── Attribution: whose reset was it ─────────────────────────────────────────

FP_A = "aaaaaaaaaaaa"
FP_B = "bbbbbbbbbbbb"
CREDIT_1 = {"id": "c00000000001", "expires_at": None}
CREDIT_2 = {"id": "c00000000002", "expires_at": None}


def held(t, used, resets_at=None, credits=1, ids=(CREDIT_1,), account_fp=FP_A, plan="pro", **extra):
    """A row from an account whose identity and bank were both read."""
    return sample(
        t, used, resets_at, credits=credits, plan_type=plan, account="a",
        account_fp=account_fp, credit_ids=None if ids is None else list(ids), **extra,
    )


def clear_pair(**after):
    """An early clear one poll wide, with every own-cause check passing."""
    before = held(0, 90, 1_000_000)
    return before, {**held(60, 0, 60 + WEEK_SECONDS), **after}


class AttributionTest(unittest.TestCase):
    """docs/reset-attribution.md, "The rule", steps 1-6."""

    def test_every_check_passing_is_unattributed(self):
        before, after = clear_pair()
        verdict = qp.classify(before, after, 1_000_000)
        self.assertEqual(verdict["classification"], qp.CLASS_UNATTRIBUTED)
        self.assertEqual(
            verdict["checks"],
            {"identity": "same", "plan": "same", "credit": "unspent",
             "bracket": "tight", "local_trace": "n_a"},
        )

    def test_each_check_that_cannot_run_keeps_the_clear_unresolved(self):
        # Contract step 6: early, but one check could not be run, and the
        # reason has to say which one.
        cases = {
            "identity": ({}, {"account_fp": None}, {}, "account fingerprint"),
            "plan": ({}, {"plan_type": None}, {}, "plan was not read"),
            "credit": ({}, {"credits_available": None}, {}, "banked credit count"),
            "bracket": ({"t": 60 - 4 * qp.POLL_SECONDS - 1}, {}, {}, "bracketed by"),
            "anchor_jump": ({}, {}, {"anchor_jump": True}, "blind gap"),
        }
        for name, (before_edit, after_edit, kwargs, said) in cases.items():
            with self.subTest(check=name):
                before, after = clear_pair(**after_edit)
                before = {**before, **before_edit}
                verdict = qp.classify(before, after, 1_000_000, **kwargs)
                self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)
                self.assertIn(said, verdict["reason"])

    def test_a_carried_bank_is_not_a_bank_read_on_both_sides(self):
        before, after = clear_pair(credits_available=None)
        verdict = qp.classify(before, after, 1_000_000, {"value": 1, "t": 0})
        self.assertEqual(verdict["checks"]["credit"], "unread")
        self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)
        self.assertIn("last readable count", verdict["reason"])

    def test_the_bracket_boundary_is_inclusive_at_three_polls(self):
        width = qp.TIGHT_BRACKET_POLLS * qp.POLL_SECONDS
        before, after = clear_pair()
        tight = qp.classify({**before, "t": 60 - width}, after, 1_000_000)
        wide = qp.classify({**before, "t": 60 - width - 1}, after, 1_000_000)
        self.assertEqual(tight["checks"]["bracket"], "tight")
        self.assertEqual(wide["checks"]["bracket"], "wide")

    def test_a_changed_fingerprint_or_plan_is_never_a_clear(self):
        # The Detector catches these first; classify() is the second lock.
        for edit in ({"account_fp": FP_B}, {"plan_type": "prolite"}):
            with self.subTest(edit=edit):
                before, after = clear_pair(**edit)
                verdict = qp.classify(before, after, 1_000_000)
                self.assertEqual(verdict["classification"], qp.CLASS_ACCOUNT_CHANGE)

    def test_a_spend_still_wins_over_an_unread_identity(self):
        # Step 4 before step 5: a positive own-cause needs no other check.
        before, after = clear_pair(account_fp=None, credits_available=0, credit_ids=[])
        self.assertEqual(qp.classify(before, after, 1_000_000)["classification"], qp.CLASS_SELF)

    def test_an_account_without_a_bank_passes_the_credit_check_as_no_bank(self):
        before, after = clear_pair(credits_available=None, credit_ids=None)
        before = {**before, "credits_available": None, "credit_ids": None}
        with mock.patch.object(qp, "CREDIT_BANK", False):
            verdict = qp.classify(before, after, 1_000_000)
        self.assertEqual(verdict["checks"]["credit"], "no_bank")
        self.assertEqual(verdict["classification"], qp.CLASS_UNATTRIBUTED)
        # The same unread field on an account that HAS a bank is unread.
        self.assertEqual(qp.classify(before, after, 1_000_000)["checks"]["credit"], "unread")

    def test_a_required_local_trace_holds_the_clear_until_it_is_read(self):
        before, after = clear_pair()
        with mock.patch.object(qp, "LOCAL_TRACE", True):
            verdict = qp.classify(before, after, 1_000_000)
        self.assertEqual(verdict["checks"]["local_trace"], "unread")
        self.assertEqual(verdict["classification"], qp.CLASS_GLOBAL)
        self.assertIn("reset-menu history", verdict["reason"])

    def test_the_clear_says_whether_the_bank_was_itemised_on_both_sides(self):
        # Contract, "credit, Codex": the list is an extra test, not required,
        # so a count-only 1 -> 1 is still unattributed. What it changes is how
        # much a sentence may claim (observation_sentences, the email).
        for before_ids, after_ids, expected in (
            ([CREDIT_1], [CREDIT_1], True),
            ([], [], True),
            (None, [CREDIT_1], False),
            ([CREDIT_1], None, False),
        ):
            with self.subTest(before=before_ids, after=after_ids):
                before, after = clear_pair(credit_ids=after_ids)
                before = {**before, "credit_ids": before_ids}
                verdict = qp.classify(before, after, 1_000_000)
                self.assertEqual(verdict["classification"], qp.CLASS_UNATTRIBUTED)
                self.assertIs(verdict["credit_itemised"], expected)
                if expected:
                    self.assertIn("no credit leaving it early", verdict["reason"])
                else:
                    self.assertNotIn("leaving", verdict["reason"])
                    self.assertIn("count read on both sides", verdict["reason"])

    def test_a_check_value_nobody_wrote_down_does_not_pass(self):
        good = {"identity": "same", "plan": "same", "credit": "unspent",
                "bracket": "tight", "local_trace": "none"}
        self.assertEqual(qp.attribute_early_clear(good, 600)[0], qp.CLASS_UNATTRIBUTED)
        for name in good:
            with self.subTest(check=name):
                for broken in ({**good, name: "maybe"}, {k: v for k, v in good.items() if k != name}):
                    self.assertEqual(qp.attribute_early_clear(broken, 600)[0], qp.CLASS_GLOBAL)


class CreditLookbackTest(unittest.TestCase):
    """The bank can move a poll before the window does (2026-10-04)."""

    # The real rows, as the sample log holds them: the count fell 2 -> 1 at
    # 02:43:52 UTC, the window cleared at 02:44:52, so the adjacent pair read
    # 1 -> 1. The 2 before it was last WRITTEN at 01:21:52, 82 minutes
    # earlier, because the log only records a count when it moves.
    OCT_04 = [
        (1791076912, 100.0, 1791580921, 2),
        (1791081832, 100.0, 1791580921, 1),
        (1791081892, 0.0, 1791686634, 1),
        (1791082192, 1.0, 1791686634, 1),
    ]

    def replay(self, rows, **edit):
        d = qp.Detector()
        events = []
        for t, used, anchor, credits in rows:
            events += d.observe(held(t, used, anchor, credits=credits, ids=None, **edit))
        return [e for e in events if e.get("kind") == qp.EVENT_CLEAR]

    def test_the_2026_10_04_shape_from_the_sample_log_is_self_applied(self):
        clears = self.replay(self.OCT_04)
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0]["classification"], qp.CLASS_SELF)
        self.assertEqual(clears[0]["checks"]["credit"], "spent")
        self.assertEqual((clears[0]["credits_before"], clears[0]["credits_after"]), (1, 1))
        self.assertIn("read 2", clears[0]["reason"])

    def test_the_same_shape_read_every_poll_is_self_applied(self):
        live = [(1791081892 - 60 * n, 100.0, 1791580921, 2) for n in range(5, 1, -1)]
        live += [(1791081832, 100.0, 1791580921, 1)] + self.OCT_04[2:]
        clears = self.replay(live)
        self.assertEqual(clears[0]["classification"], qp.CLASS_SELF)

    def test_without_the_fall_the_same_rows_are_unattributed(self):
        steady = [(t, used, anchor, 1) for t, used, anchor, _ in self.OCT_04]
        self.assertEqual(self.replay(steady)[0]["classification"], qp.CLASS_UNATTRIBUTED)

    def test_a_fall_older_than_the_lookback_does_not_explain_the_clear(self):
        lookback = qp.CREDIT_LOOKBACK_POLLS * qp.POLL_SECONDS
        rows = [
            (0, 100.0, 1_000_000, 2),
            (100, 100.0, 1_000_000, 1),
            # The bank read 1 again inside the lookback, so the fall was over.
            (500 - lookback, 100.0, 1_000_000, 1),
            (500, 100.0, 1_000_000, 1),
            (560, 0.0, 560 + WEEK_SECONDS, 1),
            (620, 0.0, 620 + WEEK_SECONDS, 1),
        ]
        d = qp.Detector()
        events = []
        for t, used, anchor, credits in rows:
            events += d.observe(held(t, used, anchor, credits=credits, ids=None))
        self.assertEqual(events[0]["classification"], qp.CLASS_UNATTRIBUTED)

    def test_a_credit_leaving_with_the_count_unchanged_is_self_applied(self):
        # A redemption and a grant between two polls cancel in the count.
        d = qp.Detector()
        d.observe(held(0, 90, 1_000_000, ids=[CREDIT_1]))
        d.observe(held(60, 0, 60 + WEEK_SECONDS, ids=[CREDIT_2]))
        event = d.observe(held(120, 0, 120 + WEEK_SECONDS, ids=[CREDIT_2]))[0]
        self.assertEqual(event["classification"], qp.CLASS_SELF)
        self.assertIn("left the bank before its own expiry", event["reason"])

    def test_a_credit_that_reached_its_expiry_is_not_a_spend(self):
        expiring = {"id": "c00000000003", "expires_at": 30}
        d = qp.Detector()
        d.observe(held(0, 90, 1_000_000, ids=[CREDIT_1, expiring], credits=2))
        d.observe(held(60, 0, 60 + WEEK_SECONDS, ids=[CREDIT_1], credits=1))
        event = d.observe(held(120, 0, 120 + WEEK_SECONDS, ids=[CREDIT_1], credits=1))[0]
        # The COUNT still fell across the pair, which is a spend on its own;
        # what this pins is that the id test does not add a reason of its own.
        self.assertNotIn("left the bank", event["reason"])
        unexpired = qp.credit_left(
            [{"t": 0, "count": 2, "ids": [CREDIT_1, expiring]}], 0, 60, 2, [CREDIT_1], 60
        )
        self.assertIsNone(unexpired)

    def test_a_credit_leaving_before_a_future_expiry_is_a_spend(self):
        later = {"id": "c00000000004", "expires_at": 2_000_000_000}
        trail = [{"t": 0, "count": 1, "ids": [later]}]
        self.assertIsNotNone(qp.credit_left(trail, 0, 60, 1, [], 60))
        # ...and at or before the read that showed the clear, it expired.
        at_hi = {**later, "expires_at": 60}
        self.assertIsNone(qp.credit_left([{"t": 0, "count": 1, "ids": [at_hi]}], 0, 60, 1, [], 60))

    def test_classify_reads_the_lookback_itself(self):
        # The Detector's late refresh repeats this test a poll later, which
        # would hide a classify() that skipped it; an anchor-jump clear is
        # emitted at once and has only this one.
        before, after = clear_pair()
        trail = [{"t": -100, "count": 2, "ids": None}, {"t": 0, "count": 1, "ids": None}]
        verdict = qp.classify(before, after, 1_000_000, credit_trail=trail)
        self.assertEqual(verdict["classification"], qp.CLASS_SELF)
        self.assertEqual(verdict["checks"]["credit"], "spent")

    def test_an_anchor_jump_clear_is_never_unattributed(self):
        # A clear recovered across a blind gap: emitted confirmed at once,
        # with no later read to correct it. Two polls is still a gap.
        d = qp.Detector()
        d.observe(held(0, 80, 500_000))
        events = d.observe(held(150, 30, 150 + WEEK_SECONDS))
        self.assertEqual(events[0]["confirmed_by"], "anchor_jump")
        self.assertEqual(events[0]["checks"]["bracket"], "wide")
        self.assertEqual(events[0]["classification"], qp.CLASS_GLOBAL)
        self.assertIn("blind gap", events[0]["reason"])

    def test_an_unitemised_side_skips_only_the_id_test(self):
        trail = [{"t": 0, "count": 1, "ids": [CREDIT_1]}]
        self.assertIsNone(qp.credit_left(trail, 0, 60, 1, None, 60))
        self.assertIsNotNone(qp.credit_left(trail, 0, 60, 1, [], 60))

    def test_the_trail_stays_bounded(self):
        d = qp.Detector()
        for n in range(500):
            d.observe(held(n * 60, 50, 1_000_000))
        trail = d.slots["codex/10080"]["credit_trail"]
        self.assertLessEqual(len(trail), qp.CREDIT_LOOKBACK_POLLS + 2)
        self.assertEqual(trail[-1]["t"], 499 * 60)

    def test_a_late_fall_downgrades_an_unattributed_clear(self):
        d = qp.Detector()
        d.observe(held(0, 90, 1_000_000))
        d.observe(held(60, 0, 60 + WEEK_SECONDS))
        self.assertEqual(
            d.slots["codex/10080"]["pending"]["classification"], qp.CLASS_UNATTRIBUTED
        )
        event = d.observe(held(120, 0, 120 + WEEK_SECONDS, credits=0, ids=[]))[0]
        self.assertEqual(event["classification"], qp.CLASS_SELF)
        self.assertEqual(event["checks"]["credit"], "spent")
        self.assertIn("after the clear", event["reason"])

    def test_a_late_credit_id_leaving_downgrades_it_too(self):
        d = qp.Detector()
        d.observe(held(0, 90, 1_000_000, ids=[CREDIT_1]))
        d.observe(held(60, 0, 60 + WEEK_SECONDS, ids=[CREDIT_1]))
        event = d.observe(held(120, 0, 120 + WEEK_SECONDS, ids=[CREDIT_2]))[0]
        self.assertEqual(event["classification"], qp.CLASS_SELF)

    def test_a_late_reading_cannot_make_a_clear_unattributed(self):
        # Unread at the clear, read later: still unread at the clear.
        d = qp.Detector()
        d.observe(held(0, 90, 1_000_000))
        d.observe(held(60, 0, 60 + WEEK_SECONDS, account_fp=None))
        event = d.observe(held(120, 0, 120 + WEEK_SECONDS))[0]
        self.assertEqual(event["classification"], qp.CLASS_GLOBAL)


class AccountChangeTest(unittest.TestCase):
    """2026-09-19 and 09-20: an account switch is not a window clearing."""

    # The real rows around the 09-19 switch. The pro row before it was last
    # written four days earlier: an account at 100% writes nothing new.
    SEP_19 = [
        sample(1789455761, 100, 1789932817, credits=0, plan_type="pro"),
        sample(1789789445, 0, 1790394246, credits=0, plan_type="prolite"),
        sample(1789789505, 1, 1790394255, credits=0, plan_type="prolite"),
    ]

    def test_the_2026_09_19_plan_switch_is_an_account_change_not_a_clear(self):
        d = qp.Detector()
        events = feed(d, self.SEP_19)
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_ACCOUNT_CHANGE])
        change = events[0]
        self.assertEqual(change["classification"], qp.CLASS_ACCOUNT_CHANGE)
        self.assertEqual((change["plan_before"], change["plan_after"]), ("pro", "prolite"))
        self.assertEqual((change["used_before"], change["used_after"]), (100.0, 0.0))
        self.assertEqual(change["checks"], {"identity": "unread", "plan": "changed"})
        self.assertIsNone(d.slots["codex/10080"].get("pending"))

    def test_a_switch_re_baselines_the_slot(self):
        d = qp.Detector()
        d.observe(held(0, 100, 1_000_000, credits=2))
        d.observe(held(60, 0, 1_000_000, credits=2))  # a pending clear
        events = d.observe(held(120, 0, 120 + WEEK_SECONDS, credits=0, ids=[], account_fp=FP_B))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_ACCOUNT_CHANGE])
        state = d.slots["codex/10080"]
        self.assertIsNone(state.get("pending"))
        self.assertIsNone(state.get("active_anchor"))
        # The new account's bank starts from its own first reading.
        self.assertEqual(state["credit_trail"], [{"t": 120, "count": 0, "ids": []}])
        self.assertEqual(state["identity"], {"account_fp": FP_B, "plan_type": "pro"})
        # So its first clear is judged against its own bank, not the old 2.
        d.observe(held(180, 80, 2_000_000, credits=0, ids=[], account_fp=FP_B))
        d.observe(held(240, 0, 240 + WEEK_SECONDS, credits=0, ids=[], account_fp=FP_B))
        clear = d.observe(held(300, 0, 300 + WEEK_SECONDS, credits=0, ids=[], account_fp=FP_B))[0]
        self.assertEqual(clear["classification"], qp.CLASS_UNATTRIBUTED)

    def test_a_switch_seen_through_an_unread_row_is_still_a_switch(self):
        d = qp.Detector()
        d.observe(held(0, 50, 1_000_000))
        self.assertEqual(d.observe(held(60, 50, 1_000_000, account_fp=None)), [])
        events = d.observe(held(120, 50, 1_000_000, account_fp=FP_B))
        self.assertEqual(events[0]["kind"], qp.EVENT_ACCOUNT_CHANGE)
        self.assertEqual(events[0]["account_fp_before"], FP_A)

    def test_an_unreadable_identity_is_never_a_switch(self):
        d = qp.Detector()
        rows = [held(0, 50, 1_000_000), held(60, 50, 1_000_000, account_fp=None, plan=None),
                held(120, 50, 1_000_000)]
        self.assertEqual(feed(d, rows), [])

    def test_one_switch_is_one_event_not_a_second_one_for_the_late_field(self):
        # /login can land between the two files: plan read new, identity
        # still unread. When the identity arrives it is the same switch.
        d = qp.Detector()
        d.observe(held(0, 50, 1_000_000))
        first = d.observe(held(60, 50, 1_000_000, plan="team", account_fp=None))
        second = d.observe(held(120, 50, 1_000_000, plan="team", account_fp=FP_B))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_a_slot_with_an_identity_but_no_last_row_starts_cleanly(self):
        # A hand-edited or half-migrated cursor must not crash the poll loop.
        d = qp.Detector({"slots": {"codex/10080": {"identity": {"account_fp": FP_A}}}})
        self.assertEqual(d.observe(held(60, 50, 1_000_000, account_fp=FP_B)), [])

    def test_the_remembered_identity_survives_a_restart(self):
        d = qp.Detector()
        d.observe(held(0, 50, 1_000_000))
        restored = qp.Detector(json.loads(json.dumps(d.cursor())))
        events = restored.observe(held(60, 50, 1_000_000, account_fp=FP_B))
        self.assertEqual(events[0]["kind"], qp.EVENT_ACCOUNT_CHANGE)

    def test_a_cursor_from_before_fingerprints_still_sees_a_plan_switch(self):
        cursor = {"slots": {"codex/10080": {"last": sample(0, 100, 1_000_000)}}}
        d = qp.Detector(json.loads(json.dumps(cursor)))
        events = d.observe(sample(60, 0, 60 + WEEK_SECONDS, plan_type="prolite"))
        self.assertEqual(events[0]["kind"], qp.EVENT_ACCOUNT_CHANGE)

    def test_every_clear_carries_both_sides_and_its_checks(self):
        d = qp.Detector()
        d.observe(held(0, 90, 1_000_000))
        d.observe(held(60, 0, 60 + WEEK_SECONDS))
        event = d.observe(held(120, 0, 120 + WEEK_SECONDS))[0]
        for field, value in {
            "account": "a", "account_fp_before": FP_A, "account_fp_after": FP_A,
            "plan_before": "pro", "plan_after": "pro", "window_family": "codex/10080",
        }.items():
            self.assertEqual(event[field], value, field)
        self.assertEqual(
            sorted(event["checks"]), ["bracket", "credit", "identity", "local_trace", "plan"]
        )

    def test_a_legacy_row_gives_a_clear_with_unread_identity(self):
        d = qp.Detector()
        feed(d, [sample(0, 90, 1_000_000), sample(60, 0, 60 + WEEK_SECONDS)])
        event = d.observe(sample(120, 0, 120 + WEEK_SECONDS))[0]
        self.assertIsNone(event["account"])
        self.assertIsNone(event["account_fp_before"])
        self.assertEqual(event["checks"]["identity"], "unread")
        self.assertEqual(event["classification"], qp.CLASS_GLOBAL)

    def test_the_window_family_names_a_second_account_s_window_alike(self):
        self.assertEqual(
            qp.window_family({"limit_id": "b:codex", "account": "b", "window_minutes": WEEK}),
            "codex/10080",
        )
        self.assertEqual(
            qp.window_family({"limit_id": "codex", "account": "a", "window_minutes": WEEK}),
            "codex/10080",
        )

    def test_the_change_is_logged_and_never_published(self):
        with temp_state(), captured_log() as out:
            qp.append_jsonl(qp.EVENTS_FILE, [])
            d = qp.Detector()
            for row in self.SEP_19[:1]:
                d.observe(row)
            with mock.patch.object(qp, "normalise", return_value=self.SEP_19[1:2]), \
                    mock.patch.object(qp, "read_rate_limits", return_value={}):
                qp.poll_once(d, now=self.SEP_19[1]["t"])
        self.assertIn("ACCOUNT CHANGED", out.getvalue())
        row = observe({**feed(qp.Detector(), self.SEP_19)[0]})
        self.assertEqual(row["verdict"], "account_change")
        self.assertFalse(row["public"])


# ─── Probe health file ───────────────────────────────────────────────────────


class HealthFileTest(unittest.TestCase):
    def test_a_missing_file_reads_as_empty(self):
        with temp_state():
            self.assertEqual(qp.load_health(), {})

    def test_a_corrupt_file_reads_as_empty_rather_than_crashing(self):
        with temp_state():
            qp.HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            qp.HEALTH_FILE.write_text("{not json", encoding="utf-8")
            self.assertEqual(qp.load_health(), {})

    def test_a_json_scalar_reads_as_empty(self):
        with temp_state():
            qp.HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            qp.HEALTH_FILE.write_text("42", encoding="utf-8")
            self.assertEqual(qp.load_health(), {})

    def test_writing_leaves_no_temp_file_behind(self):
        with temp_state() as state:
            qp.write_health({"label": "codex", "updated_at": 1})
            self.assertEqual(
                sorted(p.name for p in state.iterdir()), ["probe_health.json"]
            )

    def test_another_probes_block_survives_our_write(self):
        # P2 adds a "claude" key beside ours; a full-file rewrite must not
        # drop it, and the notifier reads both from one file.
        with temp_state():
            qp.write_health({"label": "claude", "updated_at": 1}, label="claude")
            qp.write_health({"label": "codex", "updated_at": 2})
            health = qp.load_health()
            self.assertEqual(sorted(health), ["claude", "codex"])
            self.assertEqual(health["claude"]["updated_at"], 1)

    def test_the_block_carries_every_key_the_reader_expects(self):
        d = qp.Detector()
        d.observe(sample(100, 14, 1_000))
        block = qp.health_block(d, 200, last_ok_at=200)
        self.assertEqual(
            sorted(block),
            [
                "blind_since",
                "consecutive_failures",
                "detectable_now",
                "label",
                "last_error",
                "last_ok_at",
                "last_sample_t",
                "throttled_until",
                "token_stale",
                "updated_at",
            ],
        )
        self.assertEqual(block["label"], "codex")
        self.assertEqual(block["updated_at"], 200)
        self.assertEqual(block["last_sample_t"], 100)
        self.assertIsNone(block["throttled_until"])
        self.assertFalse(block["token_stale"])

    def test_detectable_now_follows_the_detection_floor(self):
        d = qp.Detector()
        d.observe(sample(0, 14, 1_000))
        d.observe(sample(0, 4, 1_000, limit_id="codex_secondary", window=300))
        self.assertEqual(
            d.detectable_now(), {"codex/10080": True, "codex_secondary/300": False}
        )

    def test_a_window_exactly_at_the_threshold_is_detectable(self):
        # A drop from exactly CLEAR_DROP_MIN to zero IS a clear (is_clear uses
        # `drop >= CLEAR_DROP_MIN`), so the health file must not tell the
        # notifier this window was too empty to show one. usedPercent arrives
        # from the RPC as an integer, so the boundary is reachable in practice.
        d = qp.Detector()
        d.observe(sample(0, qp.CLEAR_DROP_MIN, 1_000))
        self.assertTrue(d.detectable_now()["codex/10080"])
        events = d.observe(sample(60, 0, 60 + WEEK_SECONDS))
        events += d.observe(sample(120, 0, 120 + WEEK_SECONDS))
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR])

    def test_a_window_below_the_threshold_is_not_detectable(self):
        d = qp.Detector()
        d.observe(sample(0, qp.CLEAR_DROP_MIN - 1, 1_000))
        self.assertFalse(d.detectable_now()["codex/10080"])

    def test_an_empty_detector_reports_no_slots_and_no_samples(self):
        d = qp.Detector()
        self.assertEqual(d.detectable_now(), {})
        self.assertIsNone(d.last_sample_t())

    def test_last_sample_t_is_the_newest_row_across_slots(self):
        d = qp.Detector()
        d.observe(sample(500, 14, 1_000))
        d.observe(sample(300, 0, 1_000, limit_id="codex_secondary", window=300))
        self.assertEqual(d.last_sample_t(), 500)

    def test_a_restart_carries_a_fresh_outage_forward(self):
        with temp_state():
            import time as real_time

            now = int(real_time.time())
            qp.write_health(
                {
                    "label": "codex",
                    "updated_at": now - 30,
                    "last_ok_at": now - 900,
                    "blind_since": now - 800,
                    "consecutive_failures": 10,
                }
            )
            self.assertEqual(qp.resume_health(), (now - 900, now - 800))

    def test_a_stale_block_does_not_claim_a_month_of_blindness(self):
        with temp_state():
            import time as real_time

            now = int(real_time.time())
            qp.write_health(
                {
                    "label": "codex",
                    "updated_at": now - 30 * 86400,
                    "last_ok_at": now - 30 * 86400,
                    "blind_since": now - 30 * 86400,
                }
            )
            last_ok_at, blind_since = qp.resume_health()
            self.assertEqual(last_ok_at, now - 30 * 86400)
            self.assertIsNone(blind_since)

    def test_resuming_with_no_file_starts_clean(self):
        with temp_state():
            self.assertEqual(qp.resume_health(), (None, None))


class _StopLoop(Exception):
    """Ends run()'s infinite loop from inside a patched RPC."""


class RunLoopTest(unittest.TestCase):
    def run_probe(self, side_effect, once=False):
        """Drive run() with a scripted RPC; return (health, log, raised)."""
        with temp_state():
            raised = None
            with mock.patch.object(qp, "read_rate_limits", side_effect=side_effect), \
                 mock.patch.object(qp.time, "sleep"), captured_log() as out:
                try:
                    result = qp.run(once=once, with_upstream=False)
                except BaseException as exc:  # SystemExit is not an Exception
                    raised, result = exc, None
            return qp.load_health().get("codex"), out.getvalue(), raised, result

    def test_a_successful_poll_writes_the_health_file(self):
        health, _, raised, result = self.run_probe([RPC_RESULT], once=True)
        self.assertIsNone(raised)
        self.assertEqual(result, 0)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(health["last_ok_at"], health["last_sample_t"])
        self.assertIsNone(health["blind_since"])
        self.assertIsNone(health["last_error"])
        self.assertTrue(health["detectable_now"]["codex/10080"])

    def test_a_failing_poll_writes_the_health_file_too(self):
        # The whole point: the file has to move on the iterations that fail,
        # because those are the ones the owner needs to hear about.
        health, log, raised, result = self.run_probe(
            [qp.ProbeError("app-server rejected the read: http 404")], once=True
        )
        self.assertIsNone(raised)
        self.assertEqual(result, 1)
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertIsNone(health["last_ok_at"])
        self.assertIsNotNone(health["blind_since"])
        self.assertIn("http 404", health["last_error"])
        self.assertIn("PROBE FAILED (1 in a row)", log)

    def test_ten_consecutive_failures_exit_two_for_systemd(self):
        health, log, raised, _ = self.run_probe(qp.ProbeError("http 404"))
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 2)
        self.assertEqual(health["consecutive_failures"], qp.BLIND_EXIT_FAILURES)
        self.assertIsNotNone(health["blind_since"])
        self.assertIn("PROBE EXITING", log)

    def test_blindness_is_logged_every_fifth_failure_not_once(self):
        # The 2026-09-03 outage ran 35 polls on a single log line.
        _, log, _, _ = self.run_probe(qp.ProbeError("http 404"))
        blind = [l for l in log.splitlines() if l.startswith("PROBE BLIND")]
        self.assertEqual(len(blind), 2)
        self.assertIn("5 consecutive failures", blind[0])
        self.assertIn("10 consecutive failures", blind[1])

    def test_a_successful_poll_clears_the_streak(self):
        health, log, raised, _ = self.run_probe(
            [
                qp.ProbeError("http 404"),
                qp.ProbeError("http 404"),
                RPC_RESULT,
                _StopLoop(),
            ]
        )
        self.assertIsInstance(raised, _StopLoop)
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertIsNone(health["blind_since"])
        self.assertIsNone(health["last_error"])
        self.assertNotIn("PROBE EXITING", log)

    def test_failures_after_a_recovery_start_a_new_streak(self):
        health, _, raised, _ = self.run_probe(
            [qp.ProbeError("boom")] * 9 + [RPC_RESULT] + [qp.ProbeError("boom"), _StopLoop()]
        )
        self.assertIsInstance(raised, _StopLoop)
        self.assertEqual(health["consecutive_failures"], 1)


# ─── Observation export ──────────────────────────────────────────────────────


def clear_event(event_id="codex/10080:100", classification=qp.CLASS_GLOBAL):
    return {
        "event_id": event_id,
        "kind": qp.EVENT_CLEAR,
        "slot": "codex/10080",
        "limit_id": "codex",
        "detected_at": 100,
        "used_before": 100.0,
        "used_after": 0.0,
        "clear_bracket_lo": 40,
        "clear_bracket_hi": 100,
        "classification": classification,
        "confirmed": True,
        # The evidence a global_candidate verdict rests on: the banked-credit
        # count did not fall, so this was not the owner spending their own.
        "credits_before": 1,
        "credits_after": 1,
        "credits_carried": False,
    }


class ExportTest(unittest.TestCase):
    def export(self, events, health=None, name="observed/openai.json"):
        """Write the fixtures, export, and return the parsed payload."""
        with temp_state() as state:
            if events is not None:
                qp.append_jsonl(qp.EVENTS_FILE, events)
            if health is not None:
                qp.write_health(health)
            target = state / name
            with captured_log():
                self.assertEqual(qp.export_observations(target, now=999), 0)
            return json.loads(target.read_text(encoding="utf-8"))

    def test_the_envelope_names_the_vendor_and_the_probe(self):
        payload = self.export(
            [clear_event()],
            health={"label": "codex", "updated_at": 900, "last_ok_at": 900},
        )
        self.assertEqual(payload["vendor"], "openai")
        self.assertEqual(payload["exported_at"], 999)
        self.assertEqual(payload["probe"]["label"], "codex")
        self.assertEqual(payload["probe"]["status"], "ok")
        self.assertEqual(payload["probe"]["last_verified_at"], "1970-01-01T00:15:00Z")

    def test_only_scheduled_clears_are_publishable(self):
        # Private credit use and ambiguous clears must never become public
        # reset evidence, including records from before this policy change.
        payload = self.export(
            [
                clear_event("a", qp.CLASS_GLOBAL),
                clear_event("b", qp.CLASS_SELF),
                clear_event("c", qp.CLASS_NATURAL),
                clear_event("d", qp.CLASS_UNRESOLVED),
                # Every own-cause excluded is still one account: private.
                clear_event("e", qp.CLASS_UNATTRIBUTED),
                {**clear_event("f", qp.CLASS_ACCOUNT_CHANGE), "kind": qp.EVENT_ACCOUNT_CHANGE},
            ]
        )
        self.assertEqual(
            {o["event_id"]: o["public"] for o in payload["observations"]},
            {"a": False, "b": False, "c": True, "d": False, "e": False, "f": False},
        )

    def test_one_login_seen_on_every_slot_is_one_observation(self):
        change = {
            "kind": qp.EVENT_ACCOUNT_CHANGE, "classification": qp.CLASS_ACCOUNT_CHANGE,
            "account": "a", "t": 500, "seen_before": 440,
        }
        payload = self.export(
            [{**change, "event_id": f"{slot}:account:500", "slot": slot}
             for slot in ("codex/10080", "codex_secondary/300", "codex_secondary/10080")]
        )
        self.assertEqual(len(payload["observations"]), 1)
        row = payload["observations"][0]
        self.assertEqual(row["verdict"], "account_change")
        self.assertEqual(row["observed_before"], "1970-01-01T00:07:20Z")
        self.assertEqual(row["observed_after"], "1970-01-01T00:08:20Z")

    def test_a_clear_whose_credit_evidence_was_never_read_is_withheld(self):
        # global_candidate rests on one fact: the credit count did not fall.
        # The field was unreadable on 43% of rows during the 2026-09-03/04
        # flap, and classify() still writes "the credit bank unchanged" into
        # the reason when it read nothing at all. Publishing that would
        # announce the owner's own spend as a vendor-wide reset.
        payload = self.export(
            [
                {**clear_event("unread"), "credits_before": None, "credits_after": None},
                {**clear_event("carried"), "credits_carried": True},
                {**clear_event("half"), "credits_after": None},
            ]
        )
        self.assertEqual(
            {o["event_id"]: o["public"] for o in payload["observations"]},
            {"unread": False, "carried": False, "half": False},
        )

    def test_a_clear_with_no_identity_is_withheld(self):
        # No event_id means the retraction check cannot apply, and a publish
        # decision must not resolve missing data toward publishing.
        payload = self.export([{**clear_event(), "event_id": None, "slot": None}])
        self.assertFalse(payload["observations"][0]["public"])

    def test_a_credit_grant_is_never_public(self):
        # It says what the OWNER's bank did, not what the vendor did.
        payload = self.export(
            [
                {
                    "event_id": "codex/10080:credit:120",
                    "kind": qp.EVENT_CREDIT_GRANTED,
                    "slot": "codex/10080",
                    "limit_id": "codex",
                    "t": 120,
                    "credits_before": 0,
                    "credits_after": 1,
                }
            ]
        )
        self.assertEqual(len(payload["observations"]), 1)
        self.assertFalse(payload["observations"][0]["public"])

    def test_a_limit_change_is_public(self):
        payload = self.export([{"event_id": "x", "kind": qp.EVENT_LIMIT_CHANGE}])
        self.assertTrue(payload["observations"][0]["public"])

    def test_a_retracted_clear_is_marked_and_withheld(self):
        # The 2026-08-03 shape. Publishing a clear we have already retracted
        # would be asserting something we know to be false.
        payload = self.export(
            [
                clear_event("a"),
                {**clear_event("a"), "kind": qp.EVENT_RETRACTION, "confirmed": False},
            ]
        )
        first, second = payload["observations"]
        self.assertFalse(first["public"])
        self.assertTrue(first["retracted"])
        self.assertFalse(second["public"])

    def test_the_original_event_fields_are_carried_through(self):
        payload = self.export([clear_event()])
        observation = payload["observations"][0]
        self.assertEqual(observation["clear_bracket_lo"], 40)
        self.assertEqual(observation["used_before"], 100.0)

    def test_a_missing_health_file_exports_an_absent_probe_block(self):
        # --export runs where the probe does not; it must still produce a file
        # whose probe block says so rather than failing.
        payload = self.export([clear_event()])
        probe = payload["probe"]
        self.assertEqual(probe["label"], "codex")
        self.assertEqual(probe["status"], "absent")
        self.assertIsNone(probe["last_verified_at"])
        self.assertIn("probe_health.json", probe["last_error"])

    def test_an_absent_block_still_reports_what_the_cursor_knows(self):
        with temp_state() as state:
            detector = qp.Detector()
            detector.observe(sample(500, 14, 1_000))
            qp.save_cursor(detector.cursor())
            target = state / "observed" / "openai.json"
            with captured_log():
                qp.export_observations(target, now=999)
            probe = json.loads(target.read_text(encoding="utf-8"))["probe"]
        self.assertEqual(probe["status"], "absent")
        self.assertEqual(probe["detectable_now"], {"codex/10080": True})
        # The plan type is read from the same cursor, so the coverage sentence
        # still names the population when no health file has ever been written.
        self.assertIn("One Codex Pro account", probe["coverage"])

    def test_no_events_file_exports_an_empty_list(self):
        payload = self.export(None)
        self.assertEqual(payload["observations"], [])

    def test_a_half_written_line_costs_one_record_not_the_export(self):
        with temp_state() as state:
            qp.append_jsonl(qp.EVENTS_FILE, [clear_event()])
            with qp.EVENTS_FILE.open("a", encoding="utf-8") as handle:
                handle.write('{"event_id": "truncated"')
            target = state / "observed" / "openai.json"
            with captured_log():
                qp.export_observations(target, now=999)
            payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["observations"]), 1)

    def test_records_written_before_the_current_schema_are_reconstructed(self):
        # The two rows in the live log predate `kind` and `event_id` (they were
        # written 2026-09-02, before commit d1015b0). Without this the export
        # would carry two records that no consumer can identify or pair up.
        legacy_clear = {
            "detected_at": 1788322209,
            "slot": "codex/10080",
            "limit_id": "codex",
            "used_before": 100.0,
            "used_after": 0.0,
            "classification": qp.CLASS_SELF,
            "confirmed": True,
        }
        payload = self.export(
            [legacy_clear, {**legacy_clear, "reverted_at": 1788326949, "confirmed": False}]
        )
        first, second = payload["observations"]
        self.assertEqual(first["kind"], qp.EVENT_CLEAR)
        self.assertEqual(second["kind"], qp.EVENT_RETRACTION)
        self.assertEqual(first["event_id"], "codex/10080:1788322209")
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertTrue(first["retracted"])

    def test_a_reconstructed_global_clear_is_reclassified_as_private(self):
        # An old record's unchanged balance is not a redemption ledger.
        payload = self.export(
            [
                {
                    "detected_at": 100,
                    "slot": "codex/10080",
                    "classification": qp.CLASS_GLOBAL,
                    "confirmed": True,
                    "credits_before": 1,
                    "credits_after": 1,
                }
            ]
        )
        self.assertFalse(payload["observations"][0]["public"])
        self.assertEqual(payload["observations"][0]["verdict"], "unresolved")

    def test_a_reconstructed_clear_without_credit_evidence_is_withheld(self):
        payload = self.export(
            [
                {
                    "detected_at": 100,
                    "slot": "codex/10080",
                    "classification": qp.CLASS_GLOBAL,
                    "confirmed": True,
                }
            ]
        )
        self.assertFalse(payload["observations"][0]["public"])

    def test_parent_directories_are_created(self):
        with temp_state() as state:
            target = state / "data" / "observed" / "openai.json"
            with captured_log():
                qp.export_observations(target, now=999)
            self.assertTrue(target.exists())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


# ─── The published vocabulary ────────────────────────────────────────────────


# The 2026-08-30 clear exactly as it stands in quota_events.jsonl: 16% to 0%
# with the window re-anchored and the banked credit count read as 1 on both
# sides, 140.6 hours before the expiry that was on record. @thsottiaux posted
# the announcement two and a half minutes LATER, so this account saw the reset
# before the announcement existed. It is the only public observation the probe
# has ever produced, which makes it the regression case for every sentence.
LIVE_2026_08_30 = {
    "event_id": "codex/10080:1788143216",
    "kind": qp.EVENT_CLEAR,
    "detected_at": 1788143216,
    "slot": "codex/10080",
    "limit_id": "codex",
    "window_minutes": WEEK,
    "used_before": 16.0,
    "used_after": 0.0,
    "clear_bracket_lo": 1788136976,
    "clear_bracket_hi": 1788143216,
    "confirmations": 2,
    "classification": qp.CLASS_GLOBAL,
    "reason": "cleared 506134s early with the credit bank unchanged",
    "early_by_seconds": 506134,
    "prior_active_resets_at": 1788649350,
    "credits_before": 1,
    "credits_after": 1,
    "credits_carried": False,
    "reanchored": True,
    "confirmed": True,
}


# The 2026-09-19 switch as the detector now records it: pro at 100% on
# 2026-09-15 (the last row written), prolite at 0% when next read.
SEP_19_CHANGE = {
    "event_id": "codex/10080:account:1789789445",
    "kind": qp.EVENT_ACCOUNT_CHANGE,
    "classification": qp.CLASS_ACCOUNT_CHANGE,
    "slot": "codex/10080",
    "limit_id": "codex",
    "window_minutes": WEEK,
    "account": None,
    "t": 1789789445,
    "seen_before": 1789455761,
    "account_fp_before": None,
    "account_fp_after": None,
    "plan_before": "pro",
    "plan_after": "prolite",
    "used_before": 100.0,
    "used_after": 0.0,
    "checks": {"identity": "unread", "plan": "changed"},
}


def observe(event, retracted=()):
    return qp.observation(event, set(retracted))


class VerdictTest(unittest.TestCase):
    """CLASS_* in, publishable vocabulary out."""

    def verdict(self, classification):
        return observe({**clear_event(), "classification": classification})["verdict"]

    def test_a_legacy_global_candidate_has_an_unresolved_cause(self):
        self.assertEqual(self.verdict(qp.CLASS_GLOBAL), "unresolved")

    def test_the_other_three_classes_keep_their_names(self):
        self.assertEqual(self.verdict(qp.CLASS_NATURAL), "natural_expiry")
        self.assertEqual(self.verdict(qp.CLASS_SELF), "self_applied")
        self.assertEqual(self.verdict(qp.CLASS_UNRESOLVED), "unresolved")

    def test_the_two_attribution_classes_have_private_verdicts_of_their_own(self):
        self.assertEqual(self.verdict(qp.CLASS_UNATTRIBUTED), "unattributed")
        self.assertEqual(self.verdict(qp.CLASS_ACCOUNT_CHANGE), "account_change")
        for classification in (qp.CLASS_UNATTRIBUTED, qp.CLASS_ACCOUNT_CHANGE):
            self.assertNotIn(classification, qp.PUBLIC_CLASSIFICATIONS)
            self.assertFalse(observe({**clear_event(), "classification": classification})["public"])

    def test_a_credit_grant_and_a_limit_change_are_named_by_their_kind(self):
        grant = observe({"kind": qp.EVENT_CREDIT_GRANTED, "slot": "codex/10080", "t": 5})
        self.assertEqual(grant["verdict"], "credit_granted")
        self.assertEqual(
            observe({"kind": qp.EVENT_LIMIT_CHANGE, "slot": "codex/10080"})["verdict"],
            "limit_change",
        )

    def test_a_retraction_carries_no_verdict_at_all(self):
        # `public` is already False for it. This is the second lock: a consumer
        # that filtered on the verdict alone still could not publish a clear we
        # have withdrawn.
        row = observe({**clear_event(), "kind": qp.EVENT_RETRACTION, "reverted_at": 200})
        self.assertIsNone(row["verdict"])

    def test_an_unrecognised_classification_gets_no_verdict(self):
        # Reaching for the nearest verdict would be a claim. Every consumer
        # drops a row whose verdict is not in the published set.
        row = observe({**clear_event(), "classification": "something_new"})
        self.assertIsNone(row["verdict"])
        self.assertFalse(row["public"])

    def test_no_internal_class_name_reaches_a_sentence(self):
        for classification in (
            qp.CLASS_GLOBAL,
            qp.CLASS_SELF,
            qp.CLASS_NATURAL,
            qp.CLASS_UNRESOLVED,
            qp.CLASS_UNATTRIBUTED,
            qp.CLASS_ACCOUNT_CHANGE,
        ):
            row = observe({**LIVE_2026_08_30, "classification": classification})
            said = f"{row['headline']} {row['evidence']}"
            for internal in ("global_candidate", "self_applied_credit", "codex/10080"):
                self.assertNotIn(internal, said, classification)


class SentenceTest(unittest.TestCase):
    """The two sentences are the deliverable; they are printed from fields."""

    def test_the_2026_08_30_vendor_reset_reads_back_its_own_numbers(self):
        row = observe(LIVE_2026_08_30)
        self.assertEqual(
            row["headline"],
            "Our Codex weekly window cleared 5.9 days early; the cause is unverified.",
        )
        self.assertEqual(
            row["evidence"],
            "16% to 0% between Aug 30, 2026, 5:42 PM PDT and Aug 30, 2026, "
            "7:26 PM PDT, at least 140.6 hours before its scheduled expiry of Sep 5, 2026, "
            "4:02 PM PDT; the banked credit count was read on both sides (1 then 1)"
            ", but a balance is not a redemption history. Personal reset use cannot be ruled "
            "out. This is not evidence of a provider-wide reset.",
        )
        self.assertFalse(row["public"])

    def test_a_vendor_reset_is_bounded_to_this_account(self):
        # The strongest claim available, and still not "the vendor reset
        # everyone": one Pro account on one plan tier cannot say that.
        row = observe(LIVE_2026_08_30)
        self.assertIn("the cause is unverified", row["headline"])
        self.assertIn("Personal reset use cannot be ruled out", row["evidence"])
        self.assertNotIn("everyone", row["headline"])

    def test_a_self_applied_clear_names_the_credit_that_explains_it(self):
        row = observe(
            {**LIVE_2026_08_30, "classification": qp.CLASS_SELF, "credits_after": 0}
        )
        self.assertIn("possible personal reset-credit use", row["headline"])
        self.assertIn("fell from 1 to 0 across the clear", row["evidence"])
        self.assertFalse(row["public"])

    def test_a_natural_expiry_is_a_fact_about_our_window(self):
        row = observe({**LIVE_2026_08_30, "classification": qp.CLASS_NATURAL})
        self.assertEqual(
            row["headline"], "Our Codex weekly window reached its scheduled expiry."
        )
        self.assertIn("not an extra reset", row["evidence"])
        # It says what OUR window did. It says nothing about the vendor, and it
        # never claims a reset did not happen somewhere else.
        self.assertNotIn("vendor", row["evidence"].lower())

    def test_an_unresolved_clear_refuses_to_call_itself_anything(self):
        row = observe(
            {
                **LIVE_2026_08_30,
                "classification": qp.CLASS_UNRESOLVED,
                "early_by_seconds": None,
                "prior_active_resets_at": None,
            }
        )
        self.assertIn("we cannot say whether it was a reset", row["headline"])
        self.assertIn("a natural expiry cannot be ruled out", row["evidence"])
        self.assertIsNone(row["early_by_hours"])

    def test_a_credit_grant_says_the_window_did_not_move(self):
        row = observe(
            {
                "event_id": "codex/10080:credit:1788582036",
                "kind": qp.EVENT_CREDIT_GRANTED,
                "slot": "codex/10080",
                "limit_id": "codex",
                "t": 1788582036,
                "credits_before": 1,
                "credits_after": 2,
            }
        )
        self.assertEqual(
            row["headline"], "A banked reset credit arrived in this account's bank."
        )
        self.assertIn("Sep 4, 2026, 9:20 PM PDT", row["evidence"])
        self.assertIn("did not move", row["evidence"])
        self.assertFalse(row["public"])

    def test_a_retraction_explains_itself_from_the_fields_it_has(self):
        row = observe(
            {
                **LIVE_2026_08_30,
                "kind": qp.EVENT_RETRACTION,
                "reverted_at": 1788150000,
                "used_at_revert": 16.0,
                "resets_at_at_revert": 1788649350,
            }
        )
        self.assertIn("did not hold; we withdrew it", row["headline"])
        self.assertIn("on the expiry time in force before the clear", row["evidence"])

    def test_a_retraction_without_those_fields_claims_nothing(self):
        # The two live 2026-09-02 records predate used_at_revert and
        # resets_at_at_revert. Naming a signal they never recorded would be
        # inventing evidence in the one place it does the most damage.
        row = observe(
            {**LIVE_2026_08_30, "kind": qp.EVENT_RETRACTION, "reverted_at": 1788150000}
        )
        self.assertIn("predates the fields", row["evidence"])
        self.assertNotIn("Usage returned", row["evidence"])

    def test_no_sentence_ever_says_no_reset_happened(self):
        # One account on one plan tier cannot say that, so the phrase must not
        # be constructible from any record this exporter can be handed.
        records = [
            {**LIVE_2026_08_30, "classification": name}
            for name in (
                qp.CLASS_GLOBAL, qp.CLASS_SELF, qp.CLASS_NATURAL, qp.CLASS_UNRESOLVED,
                qp.CLASS_UNATTRIBUTED, qp.CLASS_ACCOUNT_CHANGE,
            )
        ] + [
            {**SEP_19_CHANGE},
            {**LIVE_2026_08_30, "kind": qp.EVENT_RETRACTION, "reverted_at": 1788150000},
            {"kind": qp.EVENT_CREDIT_GRANTED, "slot": "codex/10080", "t": 5},
            {"kind": qp.EVENT_LIMIT_CHANGE, "slot": "codex/10080"},
            {},
        ]
        for record in records:
            row = observe(record)
            said = f"{row['headline']} {row['evidence']}".lower()
            self.assertNotIn("no reset", said)
            self.assertNotIn("did not reset", said)

    def test_an_unattributed_clear_says_what_was_checked_and_no_more(self):
        row = observe({**LIVE_2026_08_30, "classification": qp.CLASS_UNATTRIBUTED,
                       "clear_bracket_lo": 1788143156})
        self.assertEqual(
            row["headline"],
            "Our Codex weekly window cleared 5.9 days early, with no cause on our side "
            "that this probe can see.",
        )
        self.assertIn("read on both sides of a 60-second bracket", row["evidence"])
        self.assertIn("cannot say whose reset this was", row["evidence"])
        self.assertFalse(row["public"])
        # 2026-08-30 read the COUNT only (no credits[] then), so the sentence
        # says what was measured and claims nothing about credits leaving: a
        # grant and a redemption between two polls cancel in a count.
        self.assertIn("the banked credit count read 1 then 1. ", row["evidence"])
        self.assertNotIn("no credit leaving", row["evidence"])
        itemised = observe({**LIVE_2026_08_30, "classification": qp.CLASS_UNATTRIBUTED,
                            "credit_itemised": True})
        self.assertIn(
            "read 1 then 1 with no credit leaving the bank before its expiry.",
            itemised["evidence"],
        )

    def test_only_a_literal_true_earns_the_itemised_clause(self):
        # A legacy record has no field; a hand-edited one may hold a string.
        for value in (None, False, "true", 1):
            with self.subTest(value=value):
                record = {**LIVE_2026_08_30, "classification": qp.CLASS_UNATTRIBUTED}
                if value is not None:
                    record["credit_itemised"] = value
                self.assertNotIn("no credit leaving", observe(record)["evidence"])

    def test_an_account_change_names_what_changed_and_no_fingerprint(self):
        row = observe(SEP_19_CHANGE)
        self.assertEqual(row["headline"], "The account behind our Codex weekly window changed.")
        self.assertIn("The plan changed from pro to prolite between", row["evidence"])
        self.assertIn("100% to 0%", row["evidence"])
        both = observe({**SEP_19_CHANGE, "account_fp_before": FP_A, "account_fp_after": FP_B,
                        "checks": {"identity": "changed", "plan": "changed"}})
        self.assertIn("A different account on a different plan", both["evidence"])
        for said in (row, both):
            text = f"{said['headline']} {said['evidence']}"
            self.assertNotIn(FP_A, text)
            self.assertNotIn(FP_B, text)

    def test_the_new_sentences_never_name_the_vendor(self):
        # Neither verdict is evidence about the vendor in either direction.
        for record in (
            {**LIVE_2026_08_30, "classification": qp.CLASS_UNATTRIBUTED},
            SEP_19_CHANGE,
        ):
            row = observe(record)
            said = f"{row['headline']} {row['evidence']}".lower()
            self.assertNotIn("vendor", said)
            self.assertNotIn("provider", said)

    def test_an_empty_record_still_produces_two_sentences(self):
        # read_events() hands over whatever parsed as an object. A row with no
        # sentence on it would be a blank bullet on the page.
        row = observe({})
        self.assertTrue(row["headline"])
        self.assertTrue(row["evidence"])
        self.assertIsNone(row["verdict"])
        self.assertFalse(row["public"])


class ObservationShapeTest(unittest.TestCase):
    """The fields build.py copies onto the site, in the shapes it expects."""

    CONTRACT = (
        "observed_at",
        "window",
        "verdict",
        "public",
        "headline",
        "evidence",
        "used_before",
        "used_after",
        "early_by_hours",
        "observed_before",
        "observed_after",
    )

    def test_every_contract_field_is_present(self):
        row = observe(LIVE_2026_08_30)
        for field in self.CONTRACT:
            self.assertIn(field, row)

    def test_timestamps_are_iso_8601_z(self):
        row = observe(LIVE_2026_08_30)
        self.assertEqual(row["observed_at"], "2026-08-31T02:26:56Z")
        self.assertEqual(row["observed_before"], "2026-08-31T00:42:56Z")
        self.assertEqual(row["observed_after"], "2026-08-31T02:26:56Z")

    def test_observed_at_sorts_chronologically_as_a_plain_string(self):
        # build.py sorts on this without parsing it, so it can never raise on a
        # malformed row — but only while the format stays fixed-width Z.
        older = observe({**LIVE_2026_08_30, "clear_bracket_hi": 1788136976})
        self.assertLess(older["observed_at"], observe(LIVE_2026_08_30)["observed_at"])

    def test_a_credit_grant_states_no_bracket_it_never_measured(self):
        row = observe({"kind": qp.EVENT_CREDIT_GRANTED, "slot": "codex/10080", "t": 5})
        self.assertEqual(row["observed_at"], "1970-01-01T00:00:05Z")
        self.assertIsNone(row["observed_before"])
        self.assertIsNone(row["observed_after"])

    def test_early_by_hours_is_the_seconds_field_rounded(self):
        self.assertEqual(observe(LIVE_2026_08_30)["early_by_hours"], 140.6)
        self.assertIsNone(observe({**LIVE_2026_08_30, "early_by_seconds": None})["early_by_hours"])

    def test_the_window_is_named_from_the_record(self):
        self.assertEqual(observe(LIVE_2026_08_30)["window"], "weekly")
        self.assertEqual(
            observe({"kind": qp.EVENT_CREDIT_GRANTED, "slot": "codex_secondary/300"})["window"],
            "5-hour",
        )

    def test_a_window_length_nobody_named_is_described_not_guessed(self):
        self.assertEqual(qp.window_name(120), "2-hour")
        self.assertEqual(qp.window_name(90), "90-minute")
        self.assertEqual(qp.window_name(None), "quota")

    def test_the_raw_record_survives_underneath_the_contract(self):
        # It is the audit trail for every sentence above. build.py copies only
        # the whitelisted keys, so nothing here can leak onto the site.
        row = observe(LIVE_2026_08_30)
        self.assertEqual(row["classification"], qp.CLASS_GLOBAL)
        self.assertEqual(row["prior_active_resets_at"], 1788649350)


class DescribeEarlyTest(unittest.TestCase):
    def test_under_two_days_reads_in_hours(self):
        self.assertEqual(qp.describe_early(3600), "1.0 hours")
        self.assertEqual(qp.describe_early(47 * 3600), "47.0 hours")

    def test_past_two_days_reads_in_days(self):
        # "140.6 hours early" is read as a big number; "5.9 days early" is read
        # as almost a whole window, which is what it is.
        self.assertEqual(qp.describe_early(506134), "5.9 days")

    def test_nothing_measured_stays_none(self):
        self.assertIsNone(qp.describe_early(None))


# ─── Coverage and probe status ───────────────────────────────────────────────


class CoverageSentenceTest(unittest.TestCase):
    """What this probe can and cannot see, measured rather than written down."""

    LIVE_PEAKS = {
        "codex/10080": 100.0,
        "codex_secondary/300": 0.0,
        "codex_secondary/10080": 0.0,
    }
    FIRST = 1788082616  # 2026-08-30 2:36 AM PDT, the oldest sample on the host

    def test_the_live_state_names_the_account_and_the_unusable_window(self):
        self.assertEqual(
            qp.coverage_sentence(self.LIVE_PEAKS, self.FIRST, "pro"),
            "One Codex Pro account, weekly window only. The 5-hour window has read 0% "
            "since Aug 30, 2026, 2:36 AM PDT, so nothing can be observed on it.",
        )

    def test_a_window_used_below_the_detection_floor_is_still_unusable(self):
        # A 4% window cannot produce a drop this detector would call a clear,
        # so "we saw no reset on it" is not evidence of anything.
        sentence = qp.coverage_sentence(
            {"codex/10080": 100.0, "codex_secondary/300": 4.0}, self.FIRST, "pro"
        )
        self.assertIn("never read above 4%", sentence)
        self.assertIn(f"{qp.CLEAR_DROP_MIN:g}-point drop", sentence)

    def test_a_window_that_carries_usage_is_not_called_unusable(self):
        sentence = qp.coverage_sentence(
            {"codex/10080": 100.0, "codex_secondary/300": 40.0}, self.FIRST, "pro"
        )
        self.assertEqual(sentence, "One Codex Pro account, 5-hour and weekly windows only.")

    def test_an_account_that_has_never_filled_a_window_says_so(self):
        sentence = qp.coverage_sentence({"codex/10080": 3.0}, self.FIRST, "pro")
        self.assertIn("No window has carried more than 3%", sentence)
        self.assertIn("nothing can be observed yet", sentence)

    def test_no_samples_at_all_says_that_and_not_less(self):
        self.assertIn("No samples are on record", qp.coverage_sentence({}, None, "pro"))

    def test_an_unknown_plan_is_omitted_rather_than_guessed(self):
        self.assertTrue(
            qp.coverage_sentence(self.LIVE_PEAKS, self.FIRST, None).startswith(
                "One Codex account,"
            )
        )

    def test_the_floor_it_quotes_is_the_detector_s_own(self):
        # The sentence stops being true the moment CLEAR_DROP_MIN moves, so it
        # is printed from the constant, never from a number typed here.
        self.assertIn(
            f"{qp.CLEAR_DROP_MIN:g}-point",
            qp.coverage_sentence({"codex/10080": 2.0}, self.FIRST, "pro"),
        )


class SlotHistoryTest(unittest.TestCase):
    def test_peaks_and_the_oldest_sample_come_off_the_log(self):
        with temp_state():
            qp.append_jsonl(
                qp.SAMPLES_FILE,
                [sample(500, 14), sample(400, 100), sample(600, 0, limit_id="other")],
            )
            peaks, first, counts = qp.slot_history(qp.SAMPLES_FILE)
        self.assertEqual(peaks, {"codex/10080": 100.0, "other/10080": 0.0})
        self.assertEqual(first, 400)

    def test_a_missing_log_is_empty_not_an_error(self):
        with temp_state():
            self.assertEqual(qp.slot_history(qp.SAMPLES_FILE), ({}, None, {}))

    def test_an_unparseable_line_costs_one_row(self):
        with temp_state():
            qp.append_jsonl(qp.SAMPLES_FILE, [sample(500, 14)])
            with qp.SAMPLES_FILE.open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")
            peaks, _first, _counts = qp.slot_history(qp.SAMPLES_FILE)
        self.assertEqual(peaks, {"codex/10080": 14.0})


class ProbeStatusTest(unittest.TestCase):
    """Only a probe that is demonstrably seeing something may read `ok`."""

    def status(self, now=1000, **health):
        return qp.probe_status(health, now)

    def test_a_fresh_block_with_a_successful_poll_is_ok(self):
        self.assertEqual(self.status(updated_at=990, last_ok_at=990), "ok")

    def test_a_block_nobody_ever_wrote_is_absent(self):
        self.assertEqual(self.status(), "absent")

    def test_a_block_that_stopped_updating_is_blind(self):
        # The 2026-09-03 shape: 35 consecutive HTTP 404s while the rest of the
        # pipeline looked healthy. The site must not say "verified" through it.
        stale = 1000 - qp.HEALTH_CARRY_SECONDS - 1
        self.assertEqual(self.status(updated_at=stale, last_ok_at=stale), "blind")

    def test_a_failure_streak_is_blind_even_while_the_block_is_fresh(self):
        self.assertEqual(
            self.status(updated_at=990, last_ok_at=900, consecutive_failures=3), "blind"
        )

    def test_a_recorded_outage_is_blind(self):
        self.assertEqual(
            self.status(updated_at=990, last_ok_at=900, blind_since=950), "blind"
        )

    def test_a_probe_that_has_never_succeeded_is_blind(self):
        self.assertEqual(self.status(updated_at=990, last_ok_at=None), "blind")


# ─── The publish step that carries the export to the site ────────────────────


class PublishExportStepTest(unittest.TestCase):
    """infra/publish.sh: the export runs before the build and cannot stop it.

    The harness is a small copy of the one in tests/test_publish.py rather than
    an import of it: these two files are edited independently, and a shared
    fixture would couple a failure in one lane to the other. Nothing here
    touches /srv/ai-resets, the real data/ directory, or /var/lib/ai-resets —
    AI_RESETS_STATE points the exporter at a throwaway directory.
    """

    @classmethod
    def setUpClass(cls):
        for tool in ("bash", "rsync", "python3"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} is not available on this machine")
        cls.publish_sh = Path(__file__).resolve().parent.parent / "infra" / "publish.sh"
        if not cls.publish_sh.is_file():
            raise unittest.SkipTest(f"{cls.publish_sh} not found")

    def setUp(self):
        root = self.publish_sh.parent.parent
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / "infra").mkdir(parents=True)
        for name in ("site", "scripts", "data"):
            shutil.copytree(
                root / name,
                self.repo / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        shutil.copy2(self.publish_sh, self.repo / "infra" / "publish.sh")
        # build.py reads the site identity from here at import. A fake repo
        # without it is a deployment without it, and both should fail the same
        # way, so the fixture supplies one rather than special-casing the code.
        shutil.copy2(root / "site.config.json", self.repo / "site.config.json")
        # check_site.py requires one event inside build.py's 30-day window, and
        # the tracked seeds are months old.
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        stamp = recent.strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.repo / "data" / "openai.json").write_text(
            json.dumps(
                {
                    "vendor": "openai",
                    "source": {"name": "codex-resets.com", "url": "https://codex-resets.com/"},
                    "fetched_at": stamp,
                    "events": [
                        {
                            "id": "cached-1",
                            "text": "Cached announcement.",
                            "url": "https://x.com/thsottiaux/status/1",
                            "announced_at": stamp,
                            "kind": "reset",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def publish(self, exporter=None):
        env = dict(os.environ)
        env["AI_RESETS_DEPLOY_TARGET"] = str(self.tmp / "docroot")
        # Point this at the temp tree, always. Left alone it defaults to
        # /etc/ai-resets/publish.env, and a suite whose result depends on a
        # file outside the repository is a suite that passes on the maintainer's
        # host and fails on a clean runner — which is exactly how the quiet-month
        # test went wrong.
        env["AI_RESETS_PUBLISH_ENV"] = str(self.tmp / "publish.env")
        env["AI_RESETS_STATE"] = str(self.tmp / "state")
        env["AI_RESETS_FETCH_OPENAI"] = str(self.stub("noop", "pass\n"))
        if exporter is not None:
            env["AI_RESETS_EXPORT_OBSERVED"] = str(exporter)
        self.assertNotEqual(self.repo.resolve(), self.publish_sh.parent.parent)
        return subprocess.run(
            ["bash", str(self.repo / "infra" / "publish.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def stub(self, name, body):
        path = self.tmp / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        return path

    def test_the_export_runs_before_the_build_and_writes_where_build_reads(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(
            result.stdout.index("[publish] export observed"),
            result.stdout.index("[publish] build"),
            result.stdout,
        )
        written = self.repo / "data" / "observed" / "openai.json"
        self.assertTrue(written.is_file(), result.stdout)
        payload = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(payload["vendor"], "openai")
        # data/observed/ is a directory, so build.py's data/*.json glob cannot
        # read this as an announcement seed and clobber the real openai.json.
        self.assertEqual(
            json.loads((self.repo / "data" / "openai.json").read_text())["events"][0]["id"],
            "cached-1",
        )

    def test_a_failing_export_is_loud_and_still_publishes(self):
        result = self.publish(self.stub("boom", "import sys\nsys.exit(3)\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export observed FAILED (exit 3)", result.stdout)
        self.assertIn("published site/ ->", result.stdout)
        self.assertIn("index.html", [p.name for p in (self.tmp / "docroot").iterdir()])



if __name__ == "__main__":
    unittest.main()
