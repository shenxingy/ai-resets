"""The one sentence an email may say about our own accounts.

The point of every case here is that the sentence never claims more than the
files on disk support, and that a broken or missing probe reads as "could not
verify" rather than as "nothing happened".
"""

import json
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
    clear_observations,
    credit_grant_since,
    observation_near,
    PROBE_STALE_SECONDS,
    STATUS_ABSENT,
    STATUS_BLIND,
    STATUS_OK,
    STATUS_UNPROBED,
    codex_weekly_reading,
    ground_truth_line,
    probe_status,
)

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

    def line(self, vendor="openai", *, is_forecast=False, kind=None, announced_at=None):
        return ground_truth_line(
            vendor,
            is_forecast=is_forecast,
            now=NOW,
            state_dir=self.state,
            kind=kind,
            announced_at=announced_at,
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
        self.assertEqual(verdicts[self.VENDOR["detected_at"]], VERDICT_VENDOR)
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

    def test_an_observation_before_the_post_still_matches(self):
        # The normal case: a rollout reaches this account before the vendor
        # gets round to posting about it.
        self.write_events([self.VENDOR])
        found = observation_near(self.POST, self.state)
        self.assertIsNotNone(found)
        self.assertEqual(found["t"], self.VENDOR["detected_at"])

    def test_an_observation_outside_the_window_does_not_match(self):
        self.write_events([self.VENDOR])
        self.assertIsNone(observation_near(self.POST + 7 * 3600, self.state))

    def test_the_email_states_the_verdict_and_the_lead_time(self):
        self.write_events([self.VENDOR])
        result = self.line(kind="reset", announced_at=self.POST)
        self.assertEqual(result["verdict"], VERDICT_VENDOR)
        line = result["line"]
        self.assertIn("Observed on our Pro account", line)
        self.assertIn("before this post", line)
        self.assertIn("16% to 0%", line)
        self.assertIn("before its scheduled expiry", line)
        self.assertIn("Nothing this account did explains it", line)
        # The claim stays bounded: never "the vendor reset everyone".
        self.assertNotIn("everyone", line)
        self.assertNotIn("no reset", line.lower())

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

    def test_a_claude_reset_the_probe_saw_is_reported_as_observed(self):
        self.claude_events([self.CLEAR])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertEqual(result.get("verdict"), VERDICT_VENDOR)
        self.assertIn("Observed on our Max 20x accounts", result["line"])
        self.assertIn("48% to 0%", result["line"])

    def test_accounts_that_disagree_cannot_confirm_an_announcement(self):
        # The probe withholds this from its own export. Reading the raw event
        # log here must honour the same gate, or the withhold means nothing.
        self.claude_events([{**self.CLEAR, "concordance": "accounts_disagree"}])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertIsNone(result.get("verdict"))
        self.assertIn("Not observed on our Max 20x accounts", result["line"])

    def test_with_no_observation_it_says_it_looked_and_saw_nothing(self):
        self.claude_events([])
        result = self.line("anthropic", kind="reset", announced_at=self.POST)
        self.assertIn("Not observed on our Max 20x accounts", result["line"])
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


if __name__ == "__main__":
    unittest.main()
