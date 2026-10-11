"""The two-account rule: when an account clear becomes the vendor's reset.

docs/reset-attribution.md, "The rule". Every guard below has a case that
fails without it; the fixtures are the shapes stage 1's detectors write.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import attribution as at
from scripts import claude_probe as cp
from scripts import quota_probe as qp
# Never the owner's real profile files: the same isolation that suite uses.
from tests.test_claude_probe import setUpModule, tearDownModule  # noqa: F401
from tests.test_claude_probe import history_file, usage_payload

T0 = 1791700000
DAY = 86400
FP_A = "aaaaaaaaaaaa"
FP_B = "bbbbbbbbbbbb"


def clear(label="a", fp=FP_A, hi=T0, **edit):
    """One confirmed unattributed clear, as stage 1's _clear_record writes it."""
    event = {
        "event_id": f"{label}:codex/10080:{hi}",
        "kind": "clear",
        "slot": f"{label}:codex/10080",
        "detected_at": hi,
        "account": label,
        "window_family": "codex/10080",
        "used_before": 60.0,
        "used_after": 0.0,
        "clear_bracket_lo": hi - 60,
        "clear_bracket_hi": hi,
        "account_fp_before": fp,
        "account_fp_after": fp,
        "plan_before": "pro",
        "plan_after": "pro",
        "classification": at.CLASS_UNATTRIBUTED,
        "confirmed": True,
    }
    event.update(edit)
    return event


def upgraded(events):
    return {
        event["event_id"]: event["witness"]
        for event in at.attribute(events)
        if at.is_vendor_reset(event)
    }


class VocabularyTests(unittest.TestCase):
    def test_the_restated_strings_match_the_probe(self):
        # attribution imports nothing from scripts/ (quota_probe imports it),
        # so a rename on either side must fail here, not silently unpair.
        self.assertEqual(at.CLASS_UNATTRIBUTED, qp.CLASS_UNATTRIBUTED)
        self.assertEqual(at.VERDICT_VENDOR_RESET, qp.VERDICT_VENDOR_RESET)
        self.assertEqual(at.EVENT_CLEAR, qp.EVENT_CLEAR)
        self.assertEqual(at.EVENT_RETRACTION, qp.EVENT_RETRACTION)

    def test_the_rules_numbers_are_the_contracts(self):
        self.assertEqual(at.PAIR_WINDOW_SECONDS, 3600)
        self.assertEqual(at.AT_LIMIT_PERCENT, 95.0)


class PairTests(unittest.TestCase):
    def test_two_accounts_clearing_together_name_each_other(self):
        a, b = clear("a", FP_A, T0), clear("b", FP_B, T0 + 180, used_before=88.0)
        out = at.attribute([a, b])
        self.assertEqual(upgraded([a, b]), {a["event_id"]: b["event_id"], b["event_id"]: a["event_id"]})
        first = out[0]
        self.assertEqual(first["attribution"], at.ATTRIBUTION_TWO_ACCOUNTS)
        self.assertEqual(first["witness_account"], "b")
        self.assertEqual(first["witness_at"], T0 + 180)
        self.assertEqual((first["witness_used_before"], first["witness_used_after"]), (88.0, 0.0))
        # The raw record is untouched underneath: the class is the detector's.
        self.assertEqual(first["classification"], at.CLASS_UNATTRIBUTED)

    def test_the_input_is_never_modified(self):
        a, b = clear("a", FP_A, T0), clear("b", FP_B, T0 + 60)
        before = json.dumps([a, b], sort_keys=True)
        at.attribute([a, b])
        self.assertEqual(json.dumps([a, b], sort_keys=True), before)

    def test_one_account_alone_is_never_the_vendor(self):
        self.assertEqual(upgraded([clear()]), {})

    def test_the_same_fingerprint_on_two_profiles_is_one_account(self):
        # Both Claude profiles logged into one account would clear together
        # for any reason that one account's holder had.
        self.assertEqual(upgraded([clear("a", FP_A, T0), clear("b", FP_A, T0 + 60)]), {})

    def test_one_probe_slot_relogged_into_another_account_is_not_two_witnesses(self):
        self.assertEqual(upgraded([clear("a", FP_A, T0), clear("a", FP_B, T0 + 60)]), {})

    def test_different_windows_do_not_corroborate_each_other(self):
        weekly = clear("a", FP_A, T0)
        other = clear("b", FP_B, T0 + 60, window_family="codex/300")
        self.assertEqual(upgraded([weekly, other]), {})

    def test_the_hour_is_inclusive_and_a_second_more_is_not(self):
        for gap, paired in ((at.PAIR_WINDOW_SECONDS, True), (at.PAIR_WINDOW_SECONDS + 1, False)):
            with self.subTest(gap=gap):
                found = upgraded([clear("a", FP_A, T0), clear("b", FP_B, T0 + gap)])
                self.assertEqual(bool(found), paired)

    def test_two_accounts_at_their_limit_stay_private(self):
        for levels, paired in (
            ((95.0, 100.0), False),
            ((94.9, 100.0), True),
            ((100.0, 30.0), True),
            # A level never read cannot show it was not an owner's paid reset.
            ((None, None), False),
            ((None, 96.0), False),
            ((None, 40.0), True),
            ((True, 40.0), True),
            ((True, 99.0), False),
        ):
            with self.subTest(levels=levels):
                events = [
                    clear("a", FP_A, T0, used_before=levels[0]),
                    clear("b", FP_B, T0 + 60, used_before=levels[1]),
                ]
                self.assertEqual(bool(upgraded(events)), paired)

    def test_a_retracted_clear_cannot_witness_or_be_witnessed(self):
        a, b = clear("a", FP_A, T0), clear("b", FP_B, T0 + 60)
        withdrawn = {"kind": "retraction", "event_id": b["event_id"], "detected_at": T0 + 900}
        self.assertEqual(upgraded([a, b, withdrawn]), {})
        # The question fusion asks about a withdrawn pair is answered apart.
        self.assertEqual(
            at.witnesses([a, b, withdrawn], ignore_retractions=True),
            {a["event_id"]: b["event_id"], b["event_id"]: a["event_id"]},
        )

    def test_only_an_unattributed_clear_can_pair(self):
        for classification in (
            qp.CLASS_GLOBAL, qp.CLASS_SELF, qp.CLASS_NATURAL, qp.CLASS_UNRESOLVED,
            qp.CLASS_ACCOUNT_CHANGE, cp.CLASS_SESSION_ONLY,
        ):
            with self.subTest(classification=classification):
                events = [clear("a", FP_A, T0, classification=classification),
                          clear("b", FP_B, T0 + 60)]
                self.assertEqual(upgraded(events), {})

    def test_an_account_change_record_is_never_a_clear(self):
        change = clear("b", FP_B, T0 + 60, kind="account_change")
        self.assertEqual(upgraded([clear("a", FP_A, T0), change]), {})

    def test_an_unconfirmed_clear_is_not_evidence(self):
        self.assertEqual(upgraded([clear("a", FP_A, T0), clear("b", FP_B, T0 + 60, confirmed=False)]), {})

    def test_legacy_and_unreadable_identities_never_pair(self):
        for edit in (
            {"account_fp_before": None, "account_fp_after": None},  # every pre-stage-1 row
            {"account_fp_after": None},
            {"account_fp_before": None},
            {"account_fp_before": FP_A},  # changed across the clear: not one account
            {"account_fp_after": ""},
            {"account": None},
            {"account": ""},
            {"window_family": None},
            {"clear_bracket_hi": None},
            {"clear_bracket_hi": str(T0 + 60)},
            {"clear_bracket_hi": True},
            {"event_id": None},
            {"event_id": ""},
        ):
            with self.subTest(edit=edit):
                events = [clear("a", FP_A, T0), clear("b", FP_B, T0 + 60, **edit)]
                self.assertEqual(upgraded(events), {})

    def test_a_legacy_record_without_a_kind_is_still_read_as_a_clear(self):
        # Records written before `kind` existed: they predate fingerprints, but
        # one that carried both would be judged like any other clear.
        legacy = clear("b", FP_B, T0 + 60)
        legacy.pop("kind")
        self.assertEqual(len(upgraded([clear("a", FP_A, T0), legacy])), 2)

    def test_the_nearest_witness_wins_whatever_the_log_order(self):
        a = clear("a", FP_A, T0)
        near = clear("b", FP_B, T0 + 120)
        far = clear("c", "cccccccccccc", T0 + 1800)
        for order in ([a, near, far], [far, near, a], [near, far, a]):
            with self.subTest(order=[e["account"] for e in order]):
                self.assertEqual(upgraded(order)[a["event_id"]], near["event_id"])

    def test_a_tie_is_broken_by_event_id_not_by_order(self):
        a = clear("a", FP_A, T0)
        b = clear("b", FP_B, T0 + 60)
        c = clear("c", "cccccccccccc", T0 - 60)
        expected = min(b["event_id"], c["event_id"])
        for order in ([a, b, c], [a, c, b]):
            self.assertEqual(upgraded(order)[a["event_id"]], expected)

    def test_is_candidate_judges_one_record_on_its_own(self):
        self.assertTrue(at.is_candidate(clear(), set()))
        for edit in ({"event_id": None}, {"kind": "retraction"}, {"kind": "account_change"}):
            with self.subTest(edit=edit):
                self.assertFalse(at.is_candidate(clear(**edit), set()))
        self.assertFalse(at.is_candidate(clear(), {clear()["event_id"]}))
        self.assertFalse(at.is_candidate("not a record", set()))

    def test_only_the_record_that_qualifies_carries_the_stamp(self):
        # Two records under one id: the detector's pending copy and the
        # confirmed one. Only the confirmed record is a vendor reset.
        a, b = clear("a", FP_A, T0), clear("b", FP_B, T0 + 60)
        pending = dict(a, confirmed=False)
        out = at.attribute([pending, a, b])
        self.assertEqual([at.is_vendor_reset(e) for e in out], [False, True, True])
        self.assertNotIn("witness", out[0])

    def test_the_last_record_for_an_id_is_the_one_judged(self):
        a = clear("a", FP_A, T0)
        b = clear("b", FP_B, T0 + 60)
        rewritten = dict(b, classification=qp.CLASS_SELF)
        self.assertEqual(upgraded([a, b, rewritten]), {})


class StampTests(unittest.TestCase):
    def test_a_claim_already_on_a_record_is_stripped_not_believed(self):
        forged = clear(attribution=at.ATTRIBUTION_TWO_ACCOUNTS, witness="x:1",
                       witness_account="b", witness_at=T0, witness_used_before=1,
                       witness_used_after=0)
        self.assertTrue(at.is_vendor_reset(forged))  # the check reads; it does not derive
        (out,) = at.attribute([forged])
        for field in at.STAMPED_FIELDS:
            self.assertNotIn(field, out)
        self.assertFalse(at.is_vendor_reset(out))

    def test_non_records_are_skipped_and_order_is_kept(self):
        a, b = clear("a", FP_A, T0), clear("b", FP_B, T0 + 60)
        out = at.attribute([None, a, "junk", 3, b])
        self.assertEqual([e["event_id"] for e in out], [a["event_id"], b["event_id"]])

    def test_is_vendor_reset_needs_every_part_of_the_stamp(self):
        good = at.attribute([clear("a", FP_A, T0), clear("b", FP_B, T0 + 60)])[0]
        self.assertTrue(at.is_vendor_reset(good))
        for edit in (
            {"attribution": None},
            {"witness": None},
            {"witness": ""},
            {"witness": good["event_id"]},
            {"classification": qp.CLASS_GLOBAL},
            {"kind": "retraction"},
        ):
            with self.subTest(edit=edit):
                self.assertFalse(at.is_vendor_reset({**good, **edit}))
        for value in (None, [], "vendor_reset"):
            self.assertFalse(at.is_vendor_reset(value))


# ─── Through the real detectors and exporters ────────────────────────────────


def codex_row(account, fp, t, used, *, anchor):
    return {
        "t": t, "limit_id": "codex" if account == "a" else f"{account}:codex",
        "slot": "secondary", "window_minutes": 10080, "used_percent": used,
        "resets_at": anchor, "credits_available": 1, "plan_type": "pro",
        "account": account, "account_fp": fp, "credit_ids": [],
    }


class DetectorPairTests(unittest.TestCase):
    """Stage 1's detector output, paired here, published by the exporters."""

    def codex_events(self, offset_b=120, used_b=88):
        detector, events = qp.Detector(), []
        # Two polls a minute apart per account, the second account `offset_b`
        # seconds behind: the shape of 2026-10-11, Pro and Pro Lite.
        for account, fp, start, used in (("a", FP_A, T0, 60), ("b", FP_B, T0 + offset_b, used_b)):
            for i, level in enumerate((used, 0, 0)):
                anchor = T0 + (3 * DAY if i == 0 else 7 * DAY)
                events += detector.observe(codex_row(account, fp, start + 60 * i, level, anchor=anchor))
        return [e for e in events if e.get("kind") == "clear"]

    def test_the_codex_detector_output_pairs_across_accounts(self):
        clears = self.codex_events()
        self.assertEqual([c["classification"] for c in clears], [qp.CLASS_UNATTRIBUTED] * 2)
        self.assertEqual({c["window_family"] for c in clears}, {"codex/10080"})
        rows = [qp.observation(e, set()) for e in at.attribute(clears)]
        self.assertEqual([r["verdict"] for r in rows], [qp.VERDICT_VENDOR_RESET] * 2)
        self.assertEqual([r["public"] for r in rows], [True, True])
        self.assertEqual(rows[0]["headline"],
                         "Our Codex weekly window cleared 3.0 days early, and so did a "
                         "second account of ours.")
        self.assertIn("Our account b's Codex weekly window cleared too, 2 minutes later "
                      "(88% to 0%)", rows[0]["evidence"])
        self.assertIn("counted as the vendor's reset", rows[0]["evidence"])

    def test_without_the_pass_over_the_whole_log_nothing_is_public(self):
        for event in self.codex_events():
            row = qp.observation(event, set())
            self.assertEqual(row["verdict"], qp.VERDICT_UNATTRIBUTED)
            self.assertFalse(row["public"])

    def test_two_accounts_at_their_limit_stay_unattributed_in_the_export(self):
        rows = [qp.observation(e, set()) for e in at.attribute(self.codex_events())]
        self.assertTrue(all(r["public"] for r in rows))
        clears = self.codex_events()
        for event in clears:
            event["used_before"] = 97.0
        rows = [qp.observation(e, set()) for e in at.attribute(clears)]
        self.assertEqual({r["verdict"] for r in rows}, {qp.VERDICT_UNATTRIBUTED})
        self.assertFalse(any(r["public"] for r in rows))

    def test_a_pair_is_exported_public_and_a_lone_clear_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "quota_events.jsonl"
            lone = clear("a", FP_A, T0 - 5 * DAY)
            records = self.codex_events() + [lone]
            events_file.write_text("".join(json.dumps(e) + "\n" for e in records))
            target = Path(tmp) / "openai.json"
            with mock.patch.object(qp, "EVENTS_FILE", events_file), \
                    mock.patch.object(qp, "SAMPLES_FILE", Path(tmp) / "none.jsonl"), \
                    mock.patch.object(qp, "HEALTH_FILE", Path(tmp) / "health.json"), \
                    mock.patch.object(qp, "CURSOR_FILE", Path(tmp) / "cursor.json"), \
                    mock.patch.object(qp, "log"):
                qp.export_observations(target, now=T0 + DAY)
            rows = json.loads(target.read_text())["observations"]
        public = {r["event_id"]: r["witness"] for r in rows if r["public"]}
        self.assertEqual(len(public), 2)
        self.assertNotIn(lone["event_id"], public)
        self.assertEqual(set(public.values()), set(public))

    def test_the_claude_detector_output_pairs_across_profiles(self):
        anchor = T0 + 3 * DAY
        with tempfile.TemporaryDirectory() as tmp:
            histories = {label: Path(tmp) / f"history-{label}.jsonl" for label in ("a", "b")}
            for path in histories.values():
                history_file(path, [("/model", T0 - 100)])
            env = {f"AI_RESETS_CLAUDE_HISTORY_{label.upper()}": str(path)
                   for label, path in histories.items()}
            with mock.patch.dict(os.environ, env):
                detector, state, events = qp.Detector(), {}, []
                for i, level in enumerate((48, 0, 0)):
                    for label, fp in (("a", FP_A), ("b", FP_B)):
                        rows = cp.normalise_usage(
                            usage_payload(weekly=level, weekly_resets=anchor),
                            label, T0 + 300 * i, plan_type="max", account_fp=fp,
                        )
                        events += cp.observe_rows(detector, rows, state)
        weekly = [e for e in events if e.get("limit_kind") == "weekly_all" and e.get("kind") == "clear"]
        self.assertEqual([e["classification"] for e in weekly], [qp.CLASS_UNATTRIBUTED] * 2)
        rows = [cp.observation(e, set()) for e in at.attribute(events)
                if e.get("limit_kind") == "weekly_all" and e.get("kind") == "clear"]
        self.assertEqual([r["verdict"] for r in rows], [qp.VERDICT_VENDOR_RESET] * 2)
        self.assertTrue(all(r["public"] for r in rows))
        self.assertIn("and so did our other account's", rows[0]["headline"])
        self.assertIn("Our account b's Claude Code weekly window cleared too", rows[0]["evidence"])
        # The concordance written while b had not cleared yet is not repeated
        # under a verdict the witness settled.
        self.assertNotIn("did not clear", rows[0]["evidence"])
        # A 5-hour clear on both accounts at the same moment never pairs.
        session = [cp.observation(e, set()) for e in at.attribute(events)
                   if e.get("limit_kind") == "session" and e.get("kind") == "clear"]
        self.assertTrue(all(r["verdict"] is None and not r["public"] for r in session))


if __name__ == "__main__":
    unittest.main()
