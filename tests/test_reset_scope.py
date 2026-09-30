"""Account clears must not cross the public vendor-evidence boundary."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts import build, claude_probe as cp, groundtruth as gt, quota_probe as qp
from tests.test_claude_probe import DAY, T0, usage_payload


class ResetScopeTests(unittest.TestCase):
    def test_stale_exports_cannot_bypass_the_new_policy(self):
        for verdict in ("vendor_reset", "unresolved", "self_applied", "credit_granted"):
            with self.subTest(verdict=verdict):
                stale = {"verdict": verdict, "public": True,
                         "headline": "An old exporter claimed a vendor reset."}
                self.assertEqual(build.public_observations([stale]), [])
                self.assertEqual(build.build_observations({"observations": [stale]}), "")

    def assert_private_clear(self, vendor, events):
        exporter = cp if vendor == "anthropic" else qp
        clears = [e for e in events if e.get("kind") == "clear" and e.get("confirmed")]
        self.assertTrue(clears)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = gt.PROBE_SPECS[vendor]
            (root / spec.events_filename).write_text(
                "\n".join(json.dumps(e) for e in events) + "\n"
            )
            (root / gt.HEALTH_FILENAME).write_text(json.dumps({
                label: {"updated_at": T0 + 600, "last_ok_at": T0 + 600}
                for label in spec.labels
            }))
            # Observe across both routes: export -> public feed, and raw log
            # -> announcement/email corroboration. The old code protected only
            # the first route when the Codex Bank field was absent.
            for event in clears:
                row = exporter.observation(event, set())
                self.assertFalse(row["public"])
                self.assertNotEqual(row["verdict"], "vendor_reset")
                self.assertEqual(build.public_observations([row]), [])
            self.assertTrue(all(o["verdict"] != "vendor_reset" for o in
                                gt.clear_observations(root, vendor=vendor)))
            for kind in ("reset", "banked"):
                vendors = {vendor: {"events": [{
                    "announced_at": qp.iso_utc(T0 + 300), "kind": kind,
                }]}}
                build.annotate_announcements(vendors, root)
                self.assertNotEqual(vendors[vendor]["events"][0]["observed"]["status"],
                                    "confirmed")
                line = gt.ground_truth_line(vendor, is_forecast=False, kind=kind, announced_at=T0 + 300,
                                            now=T0 + 600, state_dir=root)
                self.assertNotEqual(line.get("verdict"), "vendor_reset")
                self.assertIsNot(line.get("observed"), True)

    def test_codex_bank_snapshots_do_not_prove_a_vendor_reset(self):
        for bank in ((2, 1, 1), (None, None, None), (1, None, 1),
                     (1, 1, 1), (0, 0, 0), (1, 2, 2), (1, None, 0)):
            with self.subTest(bank=bank):
                detector, events = qp.Detector(), []
                for i, used in enumerate((80, 0, 0)):
                    events += detector.observe({
                        "t": T0 + i * 300, "limit_id": "codex", "slot": "secondary",
                        "window_minutes": 10080, "used_percent": used,
                        "resets_at": T0 + (3 if i == 0 else 7) * DAY,
                        "credits_available": bank[i],
                    })
                    # The decision must survive the same cursor round-trip
                    # used when the deployed probe restarts.
                    detector = qp.Detector(json.loads(json.dumps(detector.cursor())))
                self.assert_private_clear("openai", events)

    def test_claude_weekly_personal_reset_is_not_vendor_evidence(self):
        for labels in (("a",), ("a", "b")):
            with self.subTest(accounts=labels):
                detector, state, events = qp.Detector(), {}, []
                for i, used in enumerate((80, 0, 0)):
                    for label in labels:
                        rows = cp.normalise_usage(
                            usage_payload(weekly=used, weekly_resets=T0 + 3 * DAY),
                            label, T0 + i * 300,
                        )
                        events += cp.observe_rows(detector, rows, state)
                self.assert_private_clear("anthropic", events)


if __name__ == "__main__":
    unittest.main()
