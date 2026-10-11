"""Account clears must not cross the public vendor-evidence boundary.

Since 2026-10-11 one thing may: two of our accounts clearing the same window
within the hour (scripts/attribution.py). Everything else that was private on
2026-09-30 still is, and an export that merely SAYS vendor_reset is still
dropped.
"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts import attribution, build, claude_probe as cp, groundtruth as gt, quota_probe as qp
from tests.test_claude_probe import DAY, T0, usage_payload
# Never the owner's real profile files: the same isolation that suite uses.
from tests.test_claude_probe import setUpModule, tearDownModule  # noqa: F401


class ResetScopeTests(unittest.TestCase):
    def test_stale_exports_cannot_bypass_the_new_policy(self):
        for verdict in ("vendor_reset", "unresolved", "self_applied", "credit_granted",
                        "unattributed", "account_change"):
            with self.subTest(verdict=verdict):
                stale = {"verdict": verdict, "public": True,
                         "headline": "An old exporter claimed a vendor reset."}
                self.assertEqual(build.public_observations([stale]), [])
                self.assertEqual(build.build_observations({"observations": [stale]}), "")

    def test_a_vendor_reset_needs_the_pair_not_the_word(self):
        # The 2026-09-30 exporter's legacy rows said vendor_reset about a
        # global_candidate. Even with a witness field written in, a row that
        # attribution did not stamp, or whose witness is not beside it, stays
        # out — and both stamped rows of a real pair go in.
        def row(event_id, witness, account, **edit):
            return {"event_id": event_id, "kind": "clear", "classification": "unattributed",
                    "verdict": "vendor_reset", "public": True, "account": account,
                    "attribution": attribution.ATTRIBUTION_TWO_ACCOUNTS, "witness": witness,
                    "headline": "Two accounts.", **edit}
        pair = [row("x", "y", "a"), row("y", "x", "b")]
        self.assertEqual(len(build.public_observations(pair)), 2)
        for broken in (
            [row("x", "y", "a", classification="global_candidate"), pair[1]],
            [row("x", "y", "a", attribution=None), pair[1]],
            [pair[0]],
            [pair[0], row("y", "x", "b", public=False)],
            # Stamped itself, but its witness is not.
            [pair[0], row("y", "x", "b", attribution=None)],
        ):
            with self.subTest(rows=broken):
                published = build.public_observations(broken)
                self.assertNotIn("a", {r.get("account") for r in published})

    def assert_private_clear(self, vendor, events, *, trusted_post_confirms=False):
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
                for trusted in (False, True):
                    line = gt.ground_truth_line(vendor, is_forecast=False, kind=kind,
                                                announced_at=T0 + 300, now=T0 + 600,
                                                state_dir=root, trusted=trusted)
                    self.assertNotEqual(line.get("verdict"), "vendor_reset")
                    # One clean account may confirm only a trusted reset post
                    # that says the reset already happened; nothing else may.
                    confirms = trusted_post_confirms and trusted and kind == "reset"
                    self.assertIs(line.get("observed") is True, confirms, (trusted, kind))

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

    def test_an_unattributed_codex_clear_is_still_not_vendor_evidence(self):
        # Every own-cause check passes: same fingerprint, same plan, bank read
        # on both sides and unspent, one poll wide. That is the most one
        # account can say, and it still cannot rule out a reset its holder
        # paid for, so neither route may treat it as the vendor's.
        detector, events = qp.Detector(), []
        for i, used in enumerate((80, 0, 0)):
            events += detector.observe({
                "t": T0 + i * 60, "limit_id": "codex", "slot": "secondary",
                "window_minutes": 10080, "used_percent": used,
                "resets_at": T0 + (3 if i == 0 else 7) * DAY,
                "credits_available": 1, "plan_type": "pro", "account": "a",
                "account_fp": "aaaaaaaaaaaa", "credit_ids": [],
            })
            detector = qp.Detector(json.loads(json.dumps(detector.cursor())))
        clears = [e for e in events if e.get("kind") == "clear"]
        self.assertEqual(clears[0]["classification"], qp.CLASS_UNATTRIBUTED)
        self.assert_private_clear("openai", events, trusted_post_confirms=True)

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
