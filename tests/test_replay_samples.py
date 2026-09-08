"""Re-deriving the event log from the samples the probe kept.

The point of the script under test is that the raw samples outlive the rules:
the 2026-08-30 vendor reset was thrown away by a threshold that has since been
corrected, and it is recoverable because the samples were never lost. These
tests use that exact shape.
"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts import replay_samples

WEEK_SECONDS = 10080 * 60


def sample(t, used, resets_at, credits=1):
    return {
        "t": t,
        "limit_id": "codex",
        "slot": "primary",
        "window_minutes": 10080,
        "used_percent": float(used),
        "resets_at": resets_at,
        "plan_type": "pro",
        "credits_available": credits,
    }


# The real 2026-08-30 rows: 16% held on a frozen anchor, then zero on a fresh
# one, with the banked credit count unchanged on both sides.
AUG_30 = [
    sample(1788136976, 16, 1788649350),
    sample(1788143156, 16, 1788649350),
    sample(1788143216, 0, 1788748017),
    sample(1788143276, 0, 1788748077),
]


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.samples = self.root / "quota_samples.jsonl"
        self.events = self.root / "quota_events.jsonl"

    def write(self, path, records):
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def run_replay(self, *extra):
        return replay_samples.main(
            ["--samples", str(self.samples), "--events", str(self.events), *extra]
        )

    def events_now(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines() if line]

    def test_a_clear_the_old_rules_discarded_is_recovered(self):
        self.write(self.samples, AUG_30)
        self.write(self.events, [])
        self.assertEqual(self.run_replay("--apply"), 0)
        clears = [e for e in self.events_now() if e.get("kind") == "clear"]
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0]["classification"], "global_candidate")
        self.assertEqual(clears[0]["used_before"], 16.0)
        # Provenance is not optional: a re-derivation must never be mistaken
        # for something the probe saw live.
        self.assertTrue(clears[0]["backfilled"])

    def test_a_dry_run_writes_nothing(self):
        self.write(self.samples, AUG_30)
        self.write(self.events, [])
        self.assertEqual(self.run_replay(), 0)
        self.assertEqual(self.events_now(), [])

    def test_records_already_in_the_log_are_not_duplicated(self):
        self.write(self.samples, AUG_30)
        self.write(self.events, [])
        self.run_replay("--apply")
        before = self.events_now()
        self.run_replay("--apply")
        self.assertEqual(len(self.events_now()), len(before))

    def test_a_record_written_before_event_id_existed_still_matches(self):
        # The two rows already in production carry no kind and no event_id.
        self.write(self.samples, AUG_30)
        legacy = {
            "slot": "codex/10080",
            "detected_at": 1788143216,
            "used_before": 16.0,
            "used_after": 0.0,
            "classification": "global_candidate",
            "confirmed": True,
        }
        self.write(self.events, [legacy])
        self.run_replay("--apply")
        self.assertEqual(len(self.events_now()), 1)

    def test_missing_samples_are_reported_rather_than_guessed(self):
        self.assertEqual(self.run_replay(), 1)


if __name__ == "__main__":
    unittest.main()
