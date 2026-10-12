"""The one sentence an email may say about our own accounts.

The point of every case here is that the sentence never claims more than the
files on disk support, and that a broken or missing probe reads as "could not
verify" rather than as "nothing happened".
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import quota_probe
from scripts.groundtruth import (
    DETECTION_FLOOR_PERCENT,
    PROBE_SPECS,
    probed_vendors,
    VERDICT_SELF,
    VERDICT_VENDOR,
    VERDICT_UNRESOLVED,
    clear_observations,
    credit_grant_since,
    incident_observations,
    observation_near,
    PROBE_STALE_SECONDS,
    STATUS_ABSENT,
    STATUS_BLIND,
    STATUS_OK,
    STATUS_UNPROBED,
    codex_weekly_reading,
    corroborating_clear,
    ground_truth_line,
    probe_first_line,
    probe_status,
    withdrawal_line,
)
from scripts.incidents import probe_incident_key

NOW = 1788573600


def health(**overrides):
    entry = {
        "label": "codex",
        "updated_at": NOW - 30,
        "last_ok_at": NOW - 30,
        "last_sample_t": NOW - 30,
        "consecutive_failures": 0,
        "blind_since": None,
        "throttled_until": None,
        "token_stale": False,
        "detectable_now": {"codex/10080": True},
        "last_error": None,
    }
    entry.update(overrides)
    return {"codex": entry}


def cursor(used=94.0, credits=1, t=NOW - 30):
    return {
        "slots": {
            "codex/10080": {
                "active_anchor": 1788926992,
                "last": {
                    "t": t,
                    "limit_id": "codex",
                    "window_minutes": 10080,
                    "used_percent": used,
                    "resets_at": 1788926992,
                    "credits_available": credits,
                },
            }
        }
    }


class StateDirCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state = Path(self.tempdir.name)

    def write(self, name, payload):
        (self.state / name).write_text(json.dumps(payload))

    def line(self, vendor="openai", *, is_forecast=False, kind=None, announced_at=None,
             trusted=False):
        return ground_truth_line(
            vendor,
            is_forecast=is_forecast,
            now=NOW,
            state_dir=self.state,
            kind=kind,
            announced_at=announced_at,
            trusted=trusted,
        )

    def write_events(self, events):
        (self.state / "quota_events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )


class DriftGuardTests(unittest.TestCase):
    def test_the_detection_floor_matches_the_probe(self):
        # The email says "too empty for a reset to be visible" using this
        # number; if the probe's threshold moves and this one does not, the
        # sentence becomes false without anything failing.
        self.assertEqual(DETECTION_FLOOR_PERCENT, quota_probe.CLEAR_DROP_MIN)


class ProbeStatusTests(unittest.TestCase):
    def test_a_fresh_healthy_probe_is_ok(self):
        self.assertEqual(probe_status(health(), "codex", NOW)["status"], STATUS_OK)

    def test_a_probe_that_stopped_writing_is_blind(self):
        stale = health(updated_at=NOW - PROBE_STALE_SECONDS - 1)
        self.assertEqual(probe_status(stale, "codex", NOW)["status"], STATUS_BLIND)

    def test_a_probe_failing_right_now_is_blind_even_though_it_is_writing(self):
        # The 2026-09-03 shape: the loop kept running and kept touching state
        # while every RPC returned 404.
        failing = health(blind_since=NOW - 600, consecutive_failures=10)
        self.assertEqual(probe_status(failing, "codex", NOW)["status"], STATUS_BLIND)

    def test_an_unknown_label_is_absent_not_healthy(self):
        self.assertEqual(probe_status({}, "codex", NOW)["status"], STATUS_ABSENT)


class GroundTruthLineTests(StateDirCase):
    def test_no_probe_state_at_all_cannot_verify(self):
        result = self.line()
        self.assertEqual(result["status"], STATUS_ABSENT)
        self.assertIn("Could not verify", result["line"])
        self.assertNotIn("not observed", result["line"].lower())

    def test_malformed_state_files_do_not_raise(self):
        (self.state / "probe_health.json").write_text("{not json")
        (self.state / "quota_cursor.json").write_text("")
        result = self.line()
        self.assertEqual(result["status"], STATUS_ABSENT)

    def test_a_blind_probe_says_so_instead_of_denying_the_reset(self):
        self.write("probe_health.json", health(blind_since=NOW - 1800, consecutive_failures=30))
        self.write("quota_cursor.json", cursor())
        result = self.line()
        self.assertEqual(result["status"], STATUS_BLIND)
        self.assertIn("offline", result["line"])

    def test_a_busy_window_that_did_not_clear_reads_as_not_observed(self):
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor(used=94.0, credits=1))
        result = self.line()
        self.assertEqual(result["status"], STATUS_OK)
        self.assertIn("Not observed", result["line"])
        self.assertIn("94% used", result["line"])
        self.assertIn("1 banked credit", result["line"])
        # The honest caveat is not optional.
        self.assertIn("cannot rule a reset out", result["line"])

    def test_an_almost_empty_window_says_it_could_not_have_seen_anything(self):
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor(used=3.0))
        result = self.line()
        self.assertIn("too empty", result["line"])
        self.assertNotIn("Not observed", result["line"])

    def test_a_forecast_reports_that_it_has_not_landed(self):
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor(used=98.0, credits=1))
        result = self.line(is_forecast=True)
        self.assertIn("Not landed", result["line"])
        self.assertIn("98% used", result["line"])
        self.assertIn("1 banked credit", result["line"])

    def test_a_vendor_with_no_probe_says_that_plainly(self):
        # Only Google now: the Claude probe ships, so anthropic has moved on to
        # "we measure it and it has not reported", which is a different
        # sentence and is covered in VendorProbeRegistryTests.
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor())
        for vendor in ("google",):
            result = self.line(vendor)
            self.assertEqual(result["status"], STATUS_UNPROBED)
            self.assertIn("do not yet run a ground-truth probe", result["line"])

    def test_a_healthy_probe_with_no_weekly_reading_cannot_verify(self):
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", {"slots": {}})
        result = self.line()
        self.assertEqual(result["status"], STATUS_ABSENT)
        self.assertIn("Could not verify", result["line"])

    def test_the_weekly_reading_comes_from_the_live_cursor_shape(self):
        self.write("quota_cursor.json", cursor(used=99.0, credits=1))
        reading = codex_weekly_reading(self.state)
        self.assertEqual(reading["used_percent"], 99.0)
        self.assertEqual(reading["credits_available"], 1)


class VendorProbeRegistryTests(StateDirCase):
    """Three states that must never be confused, because each has its own
    truthful sentence: we measure nothing for this vendor; we measure it but it
    has never reported; we measure it and it is blind right now."""

    def test_a_declared_probe_whose_script_is_absent_is_not_claimed(self):
        # The anthropic spec ships before its probe does. Until the script is
        # actually there, the email must not imply a measurement exists.
        for vendor, spec in PROBE_SPECS.items():
            with self.subTest(vendor=vendor):
                if not spec.exists():
                    result = self.line(vendor)
                    self.assertIn("do not yet run a ground-truth probe", result["line"])

    def test_a_shipped_probe_that_never_reported_says_no_reading_on_record(self):
        # Not "we do not measure this vendor" — we do, and it has not spoken.
        result = self.line("openai")
        self.assertIn("Could not verify", result["line"])
        self.assertNotIn("do not yet run", result["line"])

    def test_running_probes_are_read_off_the_health_file(self):
        self.assertEqual(probed_vendors(self.state), ())
        self.write("probe_health.json", health())
        self.assertEqual(probed_vendors(self.state), ("openai",))

    def test_a_vendor_with_no_spec_is_never_claimed(self):
        self.assertIn(
            "do not yet run a ground-truth probe", self.line("google")["line"]
        )

    def test_the_account_phrase_comes_from_the_spec_not_the_sentence(self):
        # Every sentence has to name the population it speaks for, and there is
        # exactly one place that decides what that population is called.
        self.write("probe_health.json", health(blind_since=NOW - 600))
        self.write("quota_cursor.json", cursor())
        self.assertIn(PROBE_SPECS["openai"].account_phrase, self.line("openai")["line"])


class ObservedClearTests(StateDirCase):
    """Was it the window expiring on schedule, or did the vendor push a reset?

    Every case is a real shape from this account's history. Across six days the
    probe recorded three clears and exactly one of them was a vendor reset; the
    other two were this account spending its own banked credit.
    """

    # 2026-08-30 7:26:56 PM PDT: 16% to 0%, 140.6 h early, credits 1 -> 1.
    # @thsottiaux posted the announcement 2.5 minutes LATER.
    VENDOR = {
        "event_id": "codex/10080:1788143216",
        "kind": "clear",
        "slot": "codex/10080",
        "detected_at": 1788143216,
        "used_before": 16.0,
        "used_after": 0.0,
        "classification": "global_candidate",
        "confirmed": True,
        "early_by_seconds": 506134,
        "credits_before": 1,
        "credits_after": 1,
    }
    # 2026-09-04 10:32:36 PM PDT: 100% to 0%, credits 2 -> 1. The owner's own.
    SELF = {
        "event_id": "codex/10080:1788586356",
        "kind": "clear",
        "slot": "codex/10080",
        "detected_at": 1788586356,
        "used_before": 100.0,
        "used_after": 0.0,
        "classification": "self_applied_credit",
        "confirmed": True,
        "early_by_seconds": 340636,
        "credits_before": 2,
        "credits_after": 1,
    }
    POST = 1788143365  # the announcement, 149 s after we saw the clear

    def setUp(self):
        super().setUp()
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor(used=94.0, credits=1))

    def test_the_three_kinds_are_told_apart(self):
        self.write_events([self.VENDOR, self.SELF])
        verdicts = {o["t"]: o["verdict"] for o in clear_observations(self.state)}
        self.assertEqual(verdicts[self.VENDOR["detected_at"]], VERDICT_UNRESOLVED)
        self.assertEqual(verdicts[self.SELF["detected_at"]], VERDICT_SELF)

    def test_a_retracted_clear_is_not_evidence(self):
        self.write_events(
            [self.VENDOR, {**self.VENDOR, "kind": "retraction", "confirmed": False}]
        )
        self.assertEqual(clear_observations(self.state), [])

    def test_an_unconfirmed_clear_is_not_evidence(self):
        self.write_events([{**self.VENDOR, "confirmed": False}])
        self.assertEqual(clear_observations(self.state), [])

    def test_only_a_vendor_reset_can_corroborate_an_announcement(self):
        # A self-applied clear says what the OWNER did. Offering it as
        # corroboration would both mislead and leak.
        self.write_events([self.SELF])
        self.assertIsNone(observation_near(self.SELF["detected_at"], self.state))

    def test_timing_before_a_post_does_not_establish_the_clear_cause(self):
        self.write_events([self.VENDOR])
        self.assertIsNone(observation_near(self.POST, self.state))

    def test_an_observation_outside_the_window_does_not_match(self):
        self.write_events([self.VENDOR])
        self.assertIsNone(observation_near(self.POST + 7 * 3600, self.state))

    def test_the_email_does_not_confirm_a_reset_from_account_only_data(self):
        self.write_events([self.VENDOR])
        result = self.line(kind="reset", announced_at=self.POST)
        self.assertNotIn("verdict", result)
        self.assertIn("Not observed", result["line"])
        self.assertNotIn("Nothing this account did explains it", result["line"])
        self.assertNotIn("16% to 0%", result["line"])

    def test_without_a_matching_observation_it_falls_back_to_not_observed(self):
        self.write_events([self.SELF])
        result = self.line(kind="reset", announced_at=self.POST)
        self.assertNotIn("verdict", result)
        self.assertIn("Not observed", result["line"])
        self.assertIn("cannot rule a reset out", result["line"])

    def test_a_banked_grant_match_admits_it_is_only_timing(self):
        # A credit can also arrive from the standing daily grant announced on
        # 2026-09-03, so the match must not be stated as causation.
        self.write_events(
            [{"kind": "credit_granted", "slot": "codex/10080", "t": NOW - 1800,
              "credits_before": 1, "credits_after": 2}]
        )
        self.write("quota_cursor.json", cursor(used=100.0, credits=2))
        result = self.line(is_forecast=True, kind="banked", announced_at=NOW - 4 * 3600)
        self.assertIn("match is by timing alone", result["line"])
        self.assertIn("standing daily grant", result["line"])


class ClaudeObservationTests(StateDirCase):
    """A vendor without a usage percentage still gets its observation read.

    The Claude probe reports no usage number to quote, and the reading branch
    used to return "not observed" before the observation was ever looked up —
    so the email said the OPPOSITE of what the probe had just measured. The
    observation is the strongest evidence there is and does not need a
    percentage to be true, so it is consulted first.
    """

    POST = NOW - 200
    CLEAR = {
        "event_id": "claude:a:weekly_all:1",
        "kind": "clear",
        "slot": "claude:a:weekly_all",
        "detected_at": NOW - 300,
        "used_before": 48.0,
        "used_after": 0.0,
        "classification": "global_candidate",
        "confirmed": True,
        "early_by_seconds": 260000,
    }

    def setUp(self):
        super().setUp()
        for label in ("claude:a", "claude:b"):
            self.write_health_label(label)

    def write_health_label(self, label):
        path = self.state / "probe_health.json"
        blocks = json.loads(path.read_text()) if path.exists() else {}
        blocks[label] = {"label": label, "updated_at": NOW - 30, "last_ok_at": NOW - 30}
        path.write_text(json.dumps(blocks))

    def claude_events(self, records):
        (self.state / "claude_events.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n"
        )

    def test_a_claude_account_clear_does_not_confirm_the_announcement(self):
        self.claude_events([self.CLEAR])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertNotIn("verdict", result)
        self.assertIn("Not observed on our two Claude accounts", result["line"])
        self.assertNotIn("48% to 0%", result["line"])

    def test_accounts_that_disagree_cannot_confirm_an_announcement(self):
        # The probe withholds this from its own export. Reading the raw event
        # log here must honour the same gate, or the withhold means nothing.
        self.claude_events([{**self.CLEAR, "concordance": "accounts_disagree"}])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertIsNone(result.get("verdict"))
        self.assertIn("Not observed on our two Claude accounts", result["line"])

    def test_with_no_observation_it_says_it_looked_and_saw_nothing(self):
        self.claude_events([])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertIn("Not observed on our two Claude accounts", result["line"])
        self.assertIn("cannot rule a reset out", result["line"])

    def test_a_blind_half_of_a_two_account_probe_is_not_a_clean_look(self):
        blocks = json.loads((self.state / "probe_health.json").read_text())
        blocks["claude:b"]["blind_since"] = NOW - 3600
        (self.state / "probe_health.json").write_text(json.dumps(blocks))
        self.claude_events([])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertIn("Could not verify", result["line"])


class BankedForecastTests(StateDirCase):
    """A banked reset lands as a credit, not as the window clearing.

    On 2026-09-04 the post said "Lands end of day" at 5:39 PM PDT and the
    credit arrived at 9:20 PM PDT, 3 hours and 41 minutes later. The window
    stayed at 100% throughout, because a banked reset is a credit to spend.
    Reading the window made the email say "not landed" while quoting "2 banked
    credits" in the same sentence.
    """

    ANNOUNCED = NOW - 4 * 3600
    GRANT = {
        "event_id": "codex/10080:credit:1",
        "kind": "credit_granted",
        "slot": "codex/10080",
        "t": NOW - 1800,
        "credits_before": 1,
        "credits_after": 2,
    }

    def setUp(self):
        super().setUp()
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor(used=100.0, credits=2))

    def test_a_grant_after_the_post_reads_as_landed(self):
        self.write_events([self.GRANT])
        result = self.line(is_forecast=True, kind="banked", announced_at=self.ANNOUNCED)
        self.assertTrue(result["landed"])
        self.assertIn("Landed on our Pro account", result["line"])
        self.assertIn("2 banked credits", result["line"])
        self.assertIn("after the post", result["line"])
        # And it explains why the window did not move, instead of contradicting itself.
        self.assertIn("still reads 100% used", result["line"])

    def test_a_grant_from_before_the_post_is_not_this_landing(self):
        stale = {**self.GRANT, "t": self.ANNOUNCED - 60}
        self.write_events([stale])
        result = self.line(is_forecast=True, kind="banked", announced_at=self.ANNOUNCED)
        self.assertFalse(result.get("landed"))
        self.assertIn("Not landed", result["line"])

    def test_no_grant_at_all_reads_as_not_landed(self):
        self.write_events([])
        result = self.line(is_forecast=True, kind="banked", announced_at=self.ANNOUNCED)
        self.assertIn("Not landed", result["line"])
        self.assertIn("the bank still holds", result["line"])

    def test_a_missing_event_log_does_not_raise(self):
        result = self.line(is_forecast=True, kind="banked", announced_at=self.ANNOUNCED)
        self.assertIn("Not landed", result["line"])

    def test_a_plain_reset_forecast_still_reads_the_window(self):
        # Only a BANKED reset lands as a credit. A forecast reset lands as the
        # window clearing, and a credit grant is not evidence of that.
        self.write_events([self.GRANT])
        result = self.line(is_forecast=True, kind="reset", announced_at=self.ANNOUNCED)
        self.assertFalse(result.get("landed"))
        self.assertIn("Not landed", result["line"])
        self.assertIn("the weekly window reads 100% used", result["line"])

    def test_the_earliest_matching_grant_wins(self):
        # One grant is emitted per limit window, so three records describe one
        # event; the first is the moment it landed.
        self.write_events(
            [
                {**self.GRANT, "slot": "codex_secondary/300", "t": NOW - 1700},
                self.GRANT,
                {**self.GRANT, "slot": "codex_secondary/10080", "t": NOW - 1600},
            ]
        )
        grant = credit_grant_since(self.ANNOUNCED, self.state)
        self.assertEqual(grant["t"], NOW - 1800)

    def test_a_corrupt_line_in_the_log_is_skipped(self):
        (self.state / "quota_events.jsonl").write_text(
            "not json\n" + json.dumps(self.GRANT) + "\n"
        )
        self.assertIsNotNone(credit_grant_since(self.ANNOUNCED, self.state))

    def test_the_second_account_s_grant_is_not_account_a_s_landing(self):
        # The sentence names "our Pro account" and quotes account a's bank and
        # weekly window, so account b's grant, however early, cannot be it.
        b_grant = {**self.GRANT, "event_id": "b:codex/10080:credit:1",
                   "slot": "b:codex/10080", "account": "b", "t": NOW - 3000}
        self.write_events([b_grant])
        self.assertIsNone(credit_grant_since(self.ANNOUNCED, self.state))
        result = self.line(is_forecast=True, kind="banked", announced_at=self.ANNOUNCED)
        self.assertFalse(result.get("landed"))
        self.write_events([b_grant, {**self.GRANT, "account": "a"}])
        self.assertEqual(credit_grant_since(self.ANNOUNCED, self.state)["t"], NOW - 1800)

    def test_an_unlabelled_grant_on_a_second_account_slot_is_still_b_s(self):
        # Labels arrived after the log began; the slot key always said whose.
        self.write_events([{**self.GRANT, "slot": "b:codex/10080"}])
        self.assertIsNone(credit_grant_since(self.ANNOUNCED, self.state))


if __name__ == "__main__":
    unittest.main()


class IncidentObservationStreamTests(StateDirCase):
    """The fusion layer's read of the probe log: clears, grants, rescales,
    and — flagged rather than dropped — clears we have since withdrawn."""

    CLEAR = {
        "event_id": "codex/10080:1788143216",
        "kind": "clear",
        "slot": "codex/10080",
        "detected_at": 1788143216,
        "used_before": 16.0,
        "used_after": 0.0,
        "classification": "global_candidate",
        "confirmed": True,
    }
    RETRACTION = {
        "kind": "retraction",
        "event_id": "codex/10080:1788143216",
        "detected_at": 1788153216,
    }

    def write_log(self, records, name="quota_events.jsonl"):
        (self.state / name).write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_a_retracted_clear_is_dropped_by_default_and_flagged_on_request(self):
        self.write_log([self.CLEAR, self.RETRACTION])
        self.assertEqual(clear_observations(self.state), [])
        rows = clear_observations(self.state, include_retracted=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["retracted"])
        self.assertEqual(rows[0]["verdict"], VERDICT_UNRESOLVED)

    def test_grants_and_rescales_join_the_stream_with_their_own_verdicts(self):
        self.write_log(
            [
                self.CLEAR,
                {"kind": "credit_granted", "t": 1788144000, "slot": "codex/10080"},
                {
                    "kind": "limit_change",
                    "slot": "codex/10080",
                    "clear_bracket_hi": 1788145000,
                },
            ]
        )
        rows = incident_observations(self.state)
        by_verdict = {row["verdict"]: row["t"] for row in rows}
        self.assertEqual(
            by_verdict,
            {
                VERDICT_UNRESOLVED: 1788143216,
                "credit_granted": 1788144000,
                "limit_change": 1788145000,
            },
        )
        self.assertFalse(any(row.get("retracted") for row in rows))

    def test_a_vendor_with_no_probe_has_no_observations(self):
        self.write_log([self.CLEAR])
        self.assertEqual(incident_observations(self.state, vendor="google"), [])

    def test_a_disagreeing_claude_clear_stays_withheld_even_for_fusion(self):
        disagreeing = dict(self.CLEAR, concordance="accounts_disagree")
        self.write_log([disagreeing], name="claude_events.jsonl")
        rows = incident_observations(self.state, vendor="anthropic")
        self.assertEqual(rows, [])


# ─── Attributed clears: what the email may now say it saw ────────────────────


def attributed_clear(label, fp, hi, *, used=60.0, family="codex/10080", **edit):
    """A stage-1 unattributed clear, as either detector writes it."""
    event = {
        "event_id": f"{label}:{family}:{hi}",
        "kind": "clear",
        "slot": f"{label}:{family}",
        "detected_at": hi,
        "account": label,
        "window_family": family,
        "window_minutes": 10080,
        "used_before": used,
        "used_after": 0.0,
        "clear_bracket_lo": hi - 60,
        "clear_bracket_hi": hi,
        "account_fp_before": fp,
        "account_fp_after": fp,
        "classification": "unattributed",
        "confirmed": True,
        "early_by_seconds": 3 * 86400,
        "credits_before": 1,
        "credits_after": 1,
    }
    event.update(edit)
    return event


class AttributedObservationTests(StateDirCase):
    POST = NOW - 3600

    def setUp(self):
        super().setUp()
        self.write("probe_health.json", health())
        self.write("quota_cursor.json", cursor(used=0.0, credits=1))

    def claude_events(self, records):
        (self.state / "claude_events.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
        blocks = {label: {"label": label, "updated_at": NOW - 30, "last_ok_at": NOW - 30}
                  for label in ("claude:a", "claude:b")}
        self.write("probe_health.json", blocks)

    def test_a_pair_reads_as_a_vendor_reset_naming_its_witness(self):
        a = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 300)
        b = attributed_clear("b", "bbbbbbbbbbbb", self.POST - 180, used=88.0)
        self.write_events([a, b])
        rows = clear_observations(self.state)
        self.assertEqual([r["verdict"] for r in rows], [VERDICT_VENDOR, VERDICT_VENDOR])
        self.assertEqual(rows[0]["witness"], b["event_id"])
        self.assertEqual((rows[0]["witness_account"], rows[0]["witness_used_before"]), ("b", 88.0))
        self.assertNotIn("account_fp_after", rows[0])

    def test_a_lone_clean_clear_reads_as_unattributed(self):
        self.write_events([attributed_clear("a", "aaaaaaaaaaaa", self.POST - 300)])
        self.assertEqual([r["verdict"] for r in clear_observations(self.state)], ["unattributed"])

    def test_a_record_claiming_a_witness_is_rederived(self):
        forged = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 300,
                                  attribution="two_accounts", witness="b:codex/10080:1")
        self.write_events([forged])
        self.assertEqual([r["verdict"] for r in clear_observations(self.state)], ["unattributed"])

    def test_the_first_of_a_claude_pair_is_kept_although_it_read_disagree(self):
        # Account a's clear is written while b is busy and has not cleared,
        # so it reads `disagree` about the very reset b confirms minutes later.
        a = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 600, family="weekly_all",
                             concordance="accounts_disagree")
        b = attributed_clear("b", "bbbbbbbbbbbb", self.POST - 300, family="weekly_all",
                             concordance="accounts_agreeing")
        self.claude_events([a, b])
        rows = clear_observations(self.state, vendor="anthropic")
        self.assertEqual([r["verdict"] for r in rows], [VERDICT_VENDOR, VERDICT_VENDOR])

    def test_a_lone_disagreeing_claude_clear_is_still_withheld(self):
        lone = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 120, family="weekly_all",
                                concordance="accounts_disagree")
        self.claude_events([lone])
        self.assertEqual(clear_observations(self.state, vendor="anthropic"), [])
        result = self.line("anthropic", kind="reset", announced_at=self.POST, trusted=True)
        self.assertNotIn("observed", result)

    def test_one_codex_account_confirms_a_trusted_post_two_minutes_later(self):
        self.write_events([attributed_clear("a", "aaaaaaaaaaaa", self.POST - 120)])
        result = self.line(kind="reset", announced_at=self.POST, trusted=True)
        self.assertTrue(result["observed"])
        self.assertEqual(result["verdict"], "unattributed")
        self.assertIn("Seen on our Codex account a at", result["line"])
        self.assertIn("(60% to 0%), 2 min before this post: the weekly window cleared 3.0 days "
                      "before its scheduled expiry.", result["line"])
        # Only the count was read (1 then 1): that is all the mail may say.
        self.assertIn("The banked credit count was unchanged and the account did not change.",
                      result["line"])
        self.assertNotIn("spent", result["line"])
        self.assertNotIn("landed", result)

    def test_no_credit_was_spent_only_when_the_bank_was_itemised(self):
        # Privacy review: an unchanged COUNT cannot show that nothing left,
        # because a grant and a redemption between two polls cancel. The
        # stronger clause needs credit ids listed on both sides, which the
        # probe records as `credit_itemised`.
        for edit, expected in (
            ({"credit_itemised": True},
             "No banked credit was spent and the account did not change."),
            ({"credit_itemised": "yes"},
             "The banked credit count was unchanged and the account did not change."),
            ({}, "The banked credit count was unchanged and the account did not change."),
            ({"credits_before": 1, "credits_after": 2},
             "The banked credit count did not fall and the account did not change."),
            ({"credits_before": None, "credits_after": None},
             "The banked credit count did not fall and the account did not change."),
        ):
            with self.subTest(edit=edit):
                self.write_events([attributed_clear("a", "aaaaaaaaaaaa", self.POST - 120, **edit)])
                result = self.line(kind="reset", announced_at=self.POST, trusted=True)
                self.assertIn(expected, result["line"])

    def test_a_pair_says_no_credit_was_spent_only_if_both_banks_were_itemised(self):
        for itemised, expected in (
            ((True, True), "No banked credit was spent"),
            ((True, False), "The banked credit count was unchanged"),
            ((False, True), "The banked credit count was unchanged"),
        ):
            with self.subTest(itemised=itemised):
                a = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 300, credit_itemised=itemised[0])
                b = attributed_clear("b", "bbbbbbbbbbbb", self.POST - 180, credit_itemised=itemised[1])
                self.write_events([a, b])
                result = self.line(kind="reset", announced_at=self.POST, trusted=False)
                self.assertIn(expected, result["line"])

    def test_one_claude_account_names_the_reset_menu_instead_of_the_bank(self):
        self.claude_events([attributed_clear("b", "bbbbbbbbbbbb", self.POST + 23, family="weekly_all")])
        result = self.line("anthropic", kind="reset", announced_at=self.POST, trusted=True)
        self.assertTrue(result["observed"])
        self.assertIn("Seen on our Claude account b", result["line"])
        self.assertIn("23 s after this post", result["line"])
        self.assertIn(
            "The account did not change and no reset menu was opened on this machine.",
            result["line"],
        )
        self.assertNotIn("banked", result["line"])

    def test_one_account_never_confirms_an_untrusted_post_or_a_far_one(self):
        self.write_events([attributed_clear("a", "aaaaaaaaaaaa", self.POST - 120)])
        for kwargs in (
            {"trusted": False},
            {"trusted": True, "is_forecast": True},
            {"trusted": True, "kind": "banked"},
            {"trusted": True, "announced_at": self.POST + 4 * 3600},
            {"trusted": True, "announced_at": self.POST - 3600 - 121},
        ):
            with self.subTest(kwargs=kwargs):
                args = {"kind": "reset", "announced_at": self.POST, **kwargs}
                result = self.line(**args)
                self.assertIsNot(result.get("observed"), True)
                self.assertNotIn("Seen on", result["line"])

    def test_two_accounts_give_both_times(self):
        a = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 300)
        b = attributed_clear("b", "bbbbbbbbbbbb", self.POST - 180, used=88.0)
        self.write_events([a, b])
        # A curated post is enough for two accounts: the pair needs no trust.
        result = self.line(kind="reset", announced_at=self.POST, trusted=False)
        self.assertTrue(result["observed"])
        self.assertEqual(result["verdict"], VERDICT_VENDOR)
        self.assertIn("Seen on two of our Codex accounts: account a at", result["line"])
        # Each account carries its own timing (review case [02]).
        self.assertIn("(60% to 0%), 5 min before this post, and account b at", result["line"])
        self.assertIn("(88% to 0%), 3 min before this post. On each", result["line"])

    def test_two_accounts_cleared_alike_are_timed_once_not_twice(self):
        # Review case [02]: "14 min before this post and 14 min before this
        # post". Two clears inside the same minute share one phrase.
        a = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 14 * 60 - 20)
        b = attributed_clear("b", "bbbbbbbbbbbb", self.POST - 14 * 60, used=88.0)
        self.write_events([a, b])
        line = self.line(kind="reset", announced_at=self.POST, trusted=True)["line"]
        self.assertIn("(60% to 0%) and account b at", line)
        self.assertIn("(88% to 0%), both 14 min before this post. On each", line)
        self.assertEqual(line.count("before this post"), 1)

    def test_a_forecast_reset_seen_by_two_accounts_has_landed(self):
        a = attributed_clear("a", "aaaaaaaaaaaa", self.POST + 5 * 3600)
        b = attributed_clear("b", "bbbbbbbbbbbb", self.POST + 5 * 3600 + 60)
        self.write_events([a, b])
        result = self.line(is_forecast=True, kind="reset", announced_at=self.POST)
        self.assertTrue(result["landed"])
        self.assertTrue(result["observed"])

    def test_a_forecast_is_matched_inside_the_six_hours_like_the_site(self):
        # The rule gives a forecast no upper bound; the match window does.
        a = attributed_clear("a", "aaaaaaaaaaaa", self.POST + 7 * 3600)
        b = attributed_clear("b", "bbbbbbbbbbbb", self.POST + 7 * 3600 + 60)
        self.write_events([a, b])
        result = self.line(is_forecast=True, kind="reset", announced_at=self.POST)
        self.assertNotIn("landed", result)

    def test_the_observation_outranks_a_probe_that_went_blind_since(self):
        self.write_events([attributed_clear("a", "aaaaaaaaaaaa", self.POST - 120)])
        self.write("probe_health.json", health(blind_since=NOW - 600))
        result = self.line(kind="reset", announced_at=self.POST, trusted=True)
        self.assertTrue(result["observed"])

    def test_the_nearest_corroborating_clear_wins(self):
        near = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 120)
        far = attributed_clear("a", "aaaaaaaaaaaa", self.POST - 7000)
        self.write_events([far, near])
        match = corroborating_clear("openai", kind="reset", is_forecast=False,
                                    announced_at=self.POST, trusted=True, state_dir=self.state)
        self.assertEqual(match["key"], near["event_id"])
        self.assertIsNone(corroborating_clear("openai", kind=None, is_forecast=False,
                                              announced_at=self.POST, trusted=True,
                                              state_dir=self.state))
        self.assertIsNone(corroborating_clear("openai", kind="reset", is_forecast=False,
                                              announced_at=None, trusted=True,
                                              state_dir=self.state))


class ProbeFirstLineTests(StateDirCase):
    """The ground-truth block of a mail our own accounts started."""

    ROW = {
        "vendor": "anthropic", "origin": "probe", "window": "weekly",
        "observed_at": "2026-09-05T02:00:00Z", "used_before": 48.0, "used_after": 0.0,
        "account": "a",
        "clears": [
            {"account": "a", "observed_at": "2026-09-05T02:00:00Z", "used_before": 48.0, "used_after": 0.0},
            {"account": "b", "observed_at": "2026-09-05T02:03:00Z", "used_before": 60.0, "used_after": 1.0},
        ],
    }

    def test_it_describes_both_clears_and_never_says_not_observed(self):
        result = probe_first_line(self.ROW, state_dir=self.state)
        self.assertTrue(result["observed"])
        self.assertEqual(result["verdict"], VERDICT_VENDOR)
        line = result["line"]
        # Review case [15]: an unverified post can sit beside the pair, so
        # "before anyone announced it" was more than we know.
        self.assertIn("Seen on two of our Claude accounts before any source we could verify "
                      "announced it", line)
        self.assertNotIn("anyone", line)
        self.assertIn("account a at Sep 4, 2026, 7:00 PM PDT (48% to 0%)", line)
        self.assertIn("account b at Sep 4, 2026, 7:03 PM PDT (60% to 1%)", line)
        self.assertIn("no reset menu was opened on this machine", line)
        self.assertNotIn("Not observed", line)

    def test_the_bank_clause_comes_from_each_clear_s_own_record(self):
        # The published row quotes the clears but not their banks; how much
        # the mail may say about the bank is read off the log, and a clear the
        # log no longer holds earns only the weakest clause.
        moment = 1788573600 - 3600
        row = {
            "vendor": "openai", "origin": "probe", "window": "weekly",
            "clears": [
                {"account": "a", "observed_at": "2026-09-05T01:00:00Z"},
                {"account": "b", "observed_at": "2026-09-05T01:03:00Z"},
            ],
        }
        for itemised, expected in (
            (True, "No banked credit was spent and the account did not change."),
            (False, "The banked credit count was unchanged and the account did not change."),
            (None, "The banked credit count did not fall and the account did not change."),
        ):
            with self.subTest(itemised=itemised):
                records = [] if itemised is None else [
                    attributed_clear("a", "aaaaaaaaaaaa", moment, credit_itemised=itemised),
                    attributed_clear("b", "bbbbbbbbbbbb", moment + 180, credit_itemised=itemised),
                ]
                (self.state / "quota_events.jsonl").write_text(
                    "".join(json.dumps(r) + "\n" for r in records))
                line = probe_first_line(row, state_dir=self.state)["line"]
                self.assertIn(expected, line)

    def test_without_stamped_clears_it_reads_the_log(self):
        moment = 1788573600 - 3600
        a = attributed_clear("a", "aaaaaaaaaaaa", moment, family="weekly_all")
        b = attributed_clear("b", "bbbbbbbbbbbb", moment + 180, family="weekly_all", used=60.0)
        (self.state / "claude_events.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in (a, b))
        )
        row = {k: v for k, v in self.ROW.items() if k != "clears"}
        row["observed_at"] = "2026-09-05T02:00:00Z"
        line = probe_first_line(row, state_dir=self.state)["line"]
        self.assertIn("account a at", line)
        self.assertIn("and account b at", line)

    def test_with_nothing_on_record_it_still_describes_the_row_itself(self):
        row = {k: v for k, v in self.ROW.items() if k != "clears"}
        result = probe_first_line(row, state_dir=self.state)
        self.assertTrue(result["observed"])
        self.assertIn("Seen on our Claude account a at Sep 4, 2026, 7:00 PM PDT (48% to 0%)",
                      result["line"])
        self.assertNotIn("Not observed", result["line"])

    def test_an_unknown_vendor_does_not_raise(self):
        result = probe_first_line({"vendor": "google", "origin": "probe"}, state_dir=self.state)
        self.assertIn("Seen on our Gemini one account at a time we never recorded", result["line"])


class StateDirTests(unittest.TestCase):
    def test_the_default_state_dir_follows_the_probes_variable(self):
        # build.py reads annotations and fusion from here, so a site built
        # against a copied state directory must read the copy.
        root = Path(__file__).resolve().parent.parent
        for env, expected in (({"AI_RESETS_STATE": "/tmp/copy"}, "/tmp/copy"), ({}, "/var/lib/ai-resets")):
            with self.subTest(env=env):
                clean = {k: v for k, v in os.environ.items() if k != "AI_RESETS_STATE"}
                out = subprocess.run(
                    [sys.executable, "-c",
                     "from scripts import groundtruth; print(groundtruth.DEFAULT_STATE_DIR)"],
                    cwd=root, env={**clean, **env}, capture_output=True, text=True, check=True,
                )
                self.assertEqual(out.stdout.strip(), expected)


class WithdrawalLineTests(StateDirCase):
    """What a correction says it withdraws (review case [06b])."""

    LO = NOW - 3 * 3600 - 60

    def pair_with(self, *retracted):
        a = attributed_clear("a", "aaaaaaaaaaaa", NOW - 3 * 3600, confirmed_at=NOW - 3 * 3600 + 120)
        b = attributed_clear("b", "bbbbbbbbbbbb", NOW - 3 * 3600 + 60,
                             confirmed_at=NOW - 3 * 3600 + 180)
        clears = {"a": a, "b": b}
        self.write_events([a, b] + [
            {"kind": "retraction", "event_id": clears[label]["event_id"], "reverted_at": NOW - 600}
            for label in retracted
        ])
        return {"vendor": "openai", "incident_key": probe_incident_key("openai", "weekly", self.LO)}

    def test_half_a_pair_withdrawn_names_whose_clear_reverted(self):
        line = withdrawal_line(self.pair_with("b"), state_dir=self.state)
        self.assertTrue(line.startswith("Account b's clear reverted: its window went back"), line)
        self.assertIn("Account a's clear still stands", line)
        self.assertIn("our claim that two of our accounts saw a vendor reset is withdrawn", line)
        # The clear that stands is never said not to have happened.
        self.assertNotIn("did not happen", line)
        self.assertNotIn("Account a's clear reverted", line)

    def test_a_pair_withdrawn_whole_says_so(self):
        line = withdrawal_line(self.pair_with("a", "b"), state_dir=self.state)
        self.assertTrue(line.startswith("The clears on account a and account b both reverted"),
                        line)
        self.assertNotIn("still stands", line)

    def test_a_group_it_cannot_find_claims_nothing_about_either_account(self):
        event = self.pair_with("b")
        for edit in ({"incident_key": "openai:probe-weekly-1"}, {"vendor": "nobody"}):
            with self.subTest(edit=edit):
                line = withdrawal_line({**event, **edit}, state_dir=self.state)
                self.assertTrue(line.startswith("A clear this observation rested on reverted"))
                self.assertNotIn("account", line.lower().replace("our own observation", ""))
