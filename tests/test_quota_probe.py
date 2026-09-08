"""Tests for the ground-truth quota detector.

The cases here are drawn from shapes actually observed in this account's
history rather than invented ones — see the module docstring of
scripts/quota_probe.py for the measurements behind each.
"""
import contextlib
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


def sample(t, used, resets_at=None, credits=1, limit_id="codex", window=WEEK):
    return {
        "t": t,
        "limit_id": limit_id,
        "slot": "primary",
        "window_minutes": window,
        "used_percent": float(used),
        "resets_at": resets_at,
        "plan_type": "pro",
        "credits_available": credits,
    }


@contextlib.contextmanager
def temp_state():
    """Point every STATE_DIR path at a throwaway directory."""
    originals = {name: getattr(qp, name) for name in STATE_FILES}
    with tempfile.TemporaryDirectory() as tmp:
        for name, filename in STATE_FILES.items():
            setattr(qp, name, Path(tmp) / filename)
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
        retractions = d.observe(sample(60 * 20, 92, anchor))
        self.assertEqual(len(retractions), 1)
        self.assertEqual(retractions[0]["kind"], qp.EVENT_RETRACTION)
        self.assertFalse(retractions[0]["confirmed"])
        self.assertEqual(retractions[0]["event_id"], events[0]["event_id"])
        self.assertEqual(retractions[0]["reverted_at"], 60 * 20)
        self.assertEqual(retractions[0]["used_at_revert"], 92.0)
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
        retraction = d.observe(sample(180, 92, 1_000_000))[0]
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
        events = d.observe(sample(120, 92, anchor))
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
        d.observe(sample(120, 55, 900_000))
        state = d.slots["codex/10080"]
        self.assertIsNone(state["pending"])
        self.assertEqual(state["active_anchor"], 900_000)

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

    def test_both_halves_of_the_question_are_publishable(self):
        # The site has to be able to say "this one was just our window
        # expiring" as well as "this one was a reset"; publishing only the
        # reset direction answers half the question. What stays private is the
        # verdict that describes the OWNER, not the vendor.
        payload = self.export(
            [
                clear_event("a", qp.CLASS_GLOBAL),
                clear_event("b", qp.CLASS_SELF),
                clear_event("c", qp.CLASS_NATURAL),
                clear_event("d", qp.CLASS_UNRESOLVED),
            ]
        )
        self.assertEqual(
            {o["event_id"]: o["public"] for o in payload["observations"]},
            {"a": True, "b": False, "c": True, "d": True},
        )

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

    def test_a_reconstructed_global_clear_is_still_ranked_public(self):
        # Records written before kind/event_id existed still carry the credit
        # evidence their verdict rested on, so reconstruction is enough to
        # publish them.
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
        self.assertTrue(payload["observations"][0]["public"])

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


def observe(event, retracted=()):
    return qp.observation(event, set(retracted))


class VerdictTest(unittest.TestCase):
    """CLASS_* in, publishable vocabulary out."""

    def verdict(self, classification):
        return observe({**clear_event(), "classification": classification})["verdict"]

    def test_a_global_candidate_is_published_as_a_vendor_reset(self):
        self.assertEqual(self.verdict(qp.CLASS_GLOBAL), "vendor_reset")

    def test_the_other_three_classes_keep_their_names(self):
        self.assertEqual(self.verdict(qp.CLASS_NATURAL), "natural_expiry")
        self.assertEqual(self.verdict(qp.CLASS_SELF), "self_applied")
        self.assertEqual(self.verdict(qp.CLASS_UNRESOLVED), "unresolved")

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
            "Our Codex weekly window cleared 5.9 days early, and nothing this "
            "account did explains it.",
        )
        self.assertEqual(
            row["evidence"],
            "16% to 0% between Aug 30, 2026, 5:42 PM PDT and Aug 30, 2026, "
            "7:26 PM PDT, at least 140.6 hours before its scheduled expiry of Sep 5, 2026, "
            "4:02 PM PDT; the banked credit count was read on both sides (1 then 1) "
            "and did not change.",
        )
        self.assertTrue(row["public"])

    def test_a_vendor_reset_is_bounded_to_this_account(self):
        # The strongest claim available, and still not "the vendor reset
        # everyone": one Pro account on one plan tier cannot say that.
        row = observe(LIVE_2026_08_30)
        self.assertIn("nothing this account did explains it", row["headline"])
        self.assertNotIn("everyone", row["headline"])

    def test_a_self_applied_clear_names_the_credit_that_explains_it(self):
        row = observe(
            {**LIVE_2026_08_30, "classification": qp.CLASS_SELF, "credits_after": 0}
        )
        self.assertIn("this account spent a banked reset credit", row["headline"])
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
            for name in (qp.CLASS_GLOBAL, qp.CLASS_SELF, qp.CLASS_NATURAL, qp.CLASS_UNRESOLVED)
        ] + [
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
