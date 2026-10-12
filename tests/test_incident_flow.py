"""Raw probe events to subscriber inboxes, through every real stage.

The notifier's own tests hand it data.json rows written by hand. These run the
whole path instead — the detectors' event records, the exporter, build.py's
public gate, annotation and fusion, then subscriptions.py's flatten, plan and
send — for the six cases docs/reset-attribution.md decides:

  (a) two accounts clear together, no post: held, then one mail
  (b) the same, then the post: still one mail
  (c) one Codex account clears 2 min before a trusted post: the post's mail
      says our account saw it
  (d) one account clears, no post: nothing public, no mail
  (e) an account change: nothing public, no mail
  (f) two accounts clear together, both at their limit: nothing public, no mail
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import build, claude_probe as cp, quota_probe as qp
from scripts.incidents import probe_incident_key
from scripts.subscriptions import Config, Store, SubscriptionApp, flatten_events, run_notifier
# Never the owner's real profile files: the same isolation that suite uses.
from tests.test_claude_probe import setUpModule, tearDownModule  # noqa: F401
from tests.test_claude_probe import history_file, usage_payload
from tests.test_subscriptions import FakeMailer

T0 = 1791700000
DAY = 86400
HOLD = 120 * 60
FP_A, FP_B, FP_C = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trusted_post(post_id, at, *, vendor, text, announcer):
    return {
        "id": post_id,
        "kind": "reset",
        "text": text,
        "announced_at": iso(at),
        "first_seen_at": iso(at + 60),
        "text_verified": True,
        "post_is_reply": False,
        "announcer": announcer,
        "url": f"https://x.com/{announcer}/status/{post_id}",
    }


class FlowCase(unittest.TestCase):
    """A state directory, an export, a build and a notifier, all in a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.observed = self.root / "observed"
        self.state.mkdir()
        self.observed.mkdir()
        histories = {label: self.root / f"history-{label}.jsonl" for label in ("a", "b")}
        for path in histories.values():
            # A profile someone typed into, with no reset menu anywhere in it.
            history_file(path, [("/model", T0 - 100)])
        patcher = mock.patch.dict(os.environ, {
            f"AI_RESETS_CLAUDE_HISTORY_{label.upper()}": str(path)
            for label, path in histories.items()
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.state / "probe_health.json").write_text(json.dumps({
            label: {"label": label, "updated_at": T0 + 30 * DAY, "last_ok_at": T0 + 30 * DAY}
            for label in ("codex", "claude:a", "claude:b")
        }))
        self.config = Config(
            api_key="test-key",
            secret="s" * 48,
            db_path=self.root / "subscriptions.db",
            public_url="https://resets.example.test",
            from_email="AI Reset Watch <updates@example.test>",
            data_file=self.root / "data.json",
            state_dir=self.state,
            owner_email="",
        )
        self.store = Store(self.config.db_path)
        self.store.init()
        self.mailer = FakeMailer()
        self.app = SubscriptionApp(self.config, self.store, self.mailer)
        for address in ("one@example.com", "two@example.com"):
            self.store.request_subscription(address, ("openai", "anthropic"))
            self.store.activate(address)
        # The first refresh a notifier ever reads is a baseline that sends
        # nothing; every case here starts one refresh in.
        self.store.discover_events(
            [{"event_key": "openai:seed", "vendor": "openai", "id": "seed", "kind": "reset",
              "announced_at": iso(T0 - 30 * DAY), "text": "Seeded."}],
            now=T0,
        )
        self.posts = {"openai": [], "anthropic": []}

    # ─── The probes ──────────────────────────────────────────────────────────

    def claude_polls(self, levels, *, fps=None, start=T0):
        """Three polls a cadence apart; `levels` is {label: (before, after)}."""
        fps = fps or {"a": [FP_A] * 3, "b": [FP_B] * 3}
        detector, state, events = qp.Detector(), {}, []
        anchor = T0 + 3 * DAY
        for i in range(3):
            for label, (before, after) in levels.items():
                level = before if i == 0 else after
                rows = cp.normalise_usage(
                    usage_payload(weekly=level, weekly_resets=anchor), label,
                    start + 300 * i, plan_type="team" if label == "a" else "max",
                    account_fp=fps[label][i],
                )
                events += cp.observe_rows(detector, rows, state)
        return events

    def claude_window_polls(self, plan, *, start=T0):
        """`plan` is {label: [(weekly_all, weekly_scoped) per poll]}, five minutes apart."""
        detector, state, events = qp.Detector(), {}, []
        anchor = T0 + 3 * DAY
        fps = {"a": FP_A, "b": FP_B}
        for i in range(max(len(levels) for levels in plan.values())):
            for label, levels in plan.items():
                weekly, scoped = levels[min(i, len(levels) - 1)]
                rows = cp.normalise_usage(
                    usage_payload(weekly=weekly, scoped=scoped, weekly_resets=anchor), label,
                    start + 300 * i, plan_type="team" if label == "a" else "max",
                    account_fp=fps[label],
                )
                events += cp.observe_rows(detector, rows, state)
        return events

    def codex_polls(self, before=80):
        detector, events = qp.Detector(), []
        for i, level in enumerate((before, 0, 0)):
            events += detector.observe({
                "t": T0 + 60 * i, "limit_id": "codex", "slot": "secondary",
                "window_minutes": 10080, "used_percent": level,
                "resets_at": T0 + (3 if i == 0 else 7) * DAY,
                "credits_available": 1, "plan_type": "pro", "account": "a",
                "account_fp": FP_A, "credit_ids": [],
            })
        return events

    def export(self, vendor, events):
        module = cp if vendor == "anthropic" else qp
        spec_file = "claude_events.jsonl" if vendor == "anthropic" else "quota_events.jsonl"
        log = self.state / spec_file
        log.write_text("".join(json.dumps(event) + "\n" for event in events))
        patches = [
            mock.patch.object(module, "EVENTS_FILE", log),
            mock.patch.object(module, "SAMPLES_FILE", self.root / "none.jsonl"),
            mock.patch.object(module, "CURSOR_FILE", self.root / "none.json"),
            mock.patch.object(qp, "HEALTH_FILE", self.state / "probe_health.json"),
            mock.patch.object(module, "log"),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            module.export_observations(self.observed / f"{vendor}.json", now=T0)

    # ─── The build and the notifier ──────────────────────────────────────────

    def build(self, now):
        vendors = {
            key: {"source": None, "fetched_at": None, "events": [dict(p) for p in posts]}
            for key, posts in self.posts.items()
        }
        with contextlib.redirect_stdout(io.StringIO()):
            build.attach_observations(vendors, self.observed)
            build.annotate_announcements(vendors, self.state)
            build.fuse_incidents(vendors, state_dir=self.state, now=iso(now))
        data = {"generated_at": iso(now), "vendors": vendors}
        self.config.data_file.write_text(json.dumps(data))
        return data

    def notify(self, now):
        with contextlib.redirect_stdout(io.StringIO()):
            run_notifier(self.config, self.app, now=now)

    def mails(self):
        return list(self.mailer.messages)

    def deliveries(self):
        with self.store.connect() as conn:
            return sorted(
                (row["incident_key"], row["email"], row["stage"], row["claim"])
                for row in conn.execute("SELECT * FROM deliveries")
            )

    def observations(self, data, vendor):
        return data["vendors"].get(vendor, {}).get("observations", [])


class TwoAccountFlowTests(FlowCase):
    KEY = probe_incident_key("anthropic", "weekly", T0)

    def setUp(self):
        super().setUp()
        self.export("anthropic", self.claude_polls({"a": (48, 0), "b": (60, 0)}))

    def test_a_two_accounts_and_no_post_is_held_then_mailed_once(self):
        pending = self.build(T0 + 600 + 60)
        rows = self.observations(pending, "anthropic")
        self.assertEqual([r["verdict"] for r in rows], ["vendor_reset", "vendor_reset"])
        # The hand-fed shape the notifier's own tests use, written by build.
        lead = next(r for r in rows if "status" in r)
        self.assertEqual(lead["status"], "observed_pending")
        self.assertEqual(lead["incident_key"], self.KEY)
        self.assertEqual(
            {k: lead[k] for k in ("origin", "kind", "accounts")},
            {"origin": "probe", "kind": "reset", "accounts": ["a", "b"]},
        )
        for field in ("event_id", "observed_at", "observed_before", "observed_after"):
            self.assertIn(field, lead)
        # Exactly one row of the pair can ever be mailed.
        self.assertEqual(sum("status" in r for r in rows), 1)
        self.assertEqual({r["incident_key"] for r in rows}, {self.KEY})
        self.notify(T0 + 600 + 60)
        self.assertEqual(self.mails(), [])

        self.build(T0 + 600 + HOLD)
        self.notify(T0 + 600 + HOLD)
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertEqual(
            mails[0]["subject"],
            "Claude Code: weekly limits reset on two of our accounts — not announced yet",
        )
        body = mails[0]["text_body"]
        self.assertIn("Seen on two of our Claude accounts before anyone announced it: account a", body)
        self.assertIn("and account b", body)
        self.assertIn("no reset menu was opened on this machine", body)
        self.assertNotIn("Not observed", body)
        self.assertNotIn("the vendor posted", body)
        # Nobody posted it: the meta line and the link say what did happen.
        self.assertIn("\nSeen on our own accounts · ", body)
        self.assertNotIn("Posted", body)
        self.assertIn("Tracker: https://resets.example.test", body)
        self.assertNotIn("View the original post", mails[0]["html_body"])
        # The mail settled its own claim: nothing more is owed.
        self.assertEqual({claim for *_, claim in self.deliveries()}, {"settled"})

        self.build(T0 + 600 + HOLD + 300)
        self.notify(T0 + 600 + HOLD + 300)
        self.assertEqual(len(self.mails()), 2)

    def test_b_the_post_that_follows_is_not_a_second_mail(self):
        self.build(T0 + 600 + HOLD)
        self.notify(T0 + 600 + HOLD)
        self.assertEqual(len(self.mails()), 2)

        post = trusted_post("2109132167112782291", T0 + 1800, vendor="anthropic",
                            text="We've just reset weekly limits for everyone.",
                            announcer="lydiahallie")
        self.posts["anthropic"] = [post]
        data = self.build(T0 + 600 + HOLD + 300)
        (event,) = data["vendors"]["anthropic"]["events"]
        self.assertEqual(event["status"], "confirmed")
        self.assertEqual(event["merged_from"], self.KEY)
        self.assertEqual(event["observed"]["accounts"], ["a", "b"])
        self.assertEqual(event["observed"]["verdict"], "vendor_reset")
        rows = self.observations(data, "anthropic")
        self.assertEqual({r["incident_key"] for r in rows}, {"anthropic:2109132167112782291"})
        self.assertFalse(any("status" in r for r in rows))

        self.notify(T0 + 600 + HOLD + 300)
        self.notify(T0 + 600 + HOLD + 600)
        self.assertEqual(len(self.mails()), 2)
        self.assertEqual({key for key, *_ in self.deliveries()}, {"anthropic:2109132167112782291"})

    def test_b_a_post_inside_the_hold_is_the_only_mail(self):
        self.build(T0 + 660)
        self.notify(T0 + 660)
        self.posts["anthropic"] = [trusted_post(
            "2109132167112782291", T0 + 900, vendor="anthropic",
            text="We've just reset weekly limits for everyone.", announcer="lydiahallie",
        )]
        self.build(T0 + 1200)
        self.notify(T0 + 1200)
        self.build(T0 + 600 + HOLD)
        self.notify(T0 + 600 + HOLD)
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertEqual(mails[0]["subject"], "Claude Code: usage limits reset")
        self.assertIn("Seen on two of our Claude accounts: account a", mails[0]["text_body"])

    def test_a_retraction_of_one_half_corrects_the_mail_it_sent(self):
        self.build(T0 + 600 + HOLD)
        self.notify(T0 + 600 + HOLD)
        self.assertEqual(len(self.mails()), 2)
        events = [json.loads(line) for line in
                  (self.state / "claude_events.jsonl").read_text().splitlines()]
        withdrawn = next(e for e in events if e.get("account") == "b"
                         and e.get("limit_kind") == "weekly_all" and e.get("kind") == "clear")
        events.append({"kind": "retraction", "event_id": withdrawn["event_id"],
                       "reverted_at": T0 + 9000, "used_at_revert": 60.0})
        self.export("anthropic", events)
        data = self.build(T0 + 9100)
        rows = self.observations(data, "anthropic")
        self.assertEqual([r["status"] for r in rows], ["retracted"])
        self.assertIsNone(rows[0]["verdict"])
        self.assertEqual(rows[0]["incident_key"], self.KEY)
        self.notify(T0 + 9100)
        retractions = [m for m in self.mails() if "withdrawing" in m["subject"]]
        self.assertEqual(len(retractions), 2)

    def test_f_two_accounts_at_their_limit_stay_private(self):
        self.export("anthropic", self.claude_polls({"a": (97, 0), "b": (100, 0)}))
        data = self.build(T0 + 600 + HOLD)
        self.assertEqual(self.observations(data, "anthropic"), [])
        self.notify(T0 + 600 + HOLD)
        self.assertEqual(self.mails(), [])


class CodexTwoAccountFlowTests(FlowCase):
    """Case (a) for Codex: both of our Codex accounts, read as the probe reads them.

    The rows come through quota_probe.normalise for account a and account b
    (the second Codex home, `b:`-prefixed) into ONE detector, exactly as the
    probe's poll loop feeds them, so the pairing is proven on the ids and
    labels the probe really writes rather than on hand-made records.
    """

    def setUp(self):
        super().setUp()
        health = json.loads((self.state / "probe_health.json").read_text())
        health["codex:b"] = {**health["codex"], "label": "codex:b"}
        (self.state / "probe_health.json").write_text(json.dumps(health))

    def codex_pair_polls(self, before=(80, 60)):
        detector, events = qp.Detector(), []
        accounts = (("a", "placeholder-account-a", "pro", 0), ("b", "placeholder-account-b", "prolite", 5))
        for i in range(3):
            for (account, account_id, plan, offset), level in zip(accounts, before):
                moment = T0 + 60 * i + offset
                result = {
                    "accountId": account_id,
                    "rateLimitsByLimitId": {"codex": {
                        "limitId": "codex", "planType": plan, "secondary": None,
                        "primary": {
                            "usedPercent": level if i == 0 else 0, "windowDurationMins": 10080,
                            "resetsAt": T0 + 3 * DAY if i == 0 else moment + 7 * DAY,
                        },
                    }},
                    "rateLimitResetCredits": {"availableCount": 0, "credits": []},
                }
                for row in qp.normalise(result, moment, account):
                    events += detector.observe(row)
        return events

    def test_a_clear_on_both_codex_accounts_is_one_held_mail(self):
        events = self.codex_pair_polls()
        self.assertEqual(
            sorted((e["account"], e["slot"]) for e in events),
            [("a", "codex/10080"), ("b", "b:codex/10080")],
        )
        self.export("openai", events)
        key = probe_incident_key("openai", "weekly", T0)
        pending = self.build(T0 + 65 + 60)
        rows = self.observations(pending, "openai")
        self.assertEqual([r["verdict"] for r in rows], ["vendor_reset", "vendor_reset"])
        lead = next(r for r in rows if "status" in r)
        self.assertEqual((lead["status"], lead["incident_key"]), ("observed_pending", key))
        self.assertEqual(lead["accounts"], ["a", "b"])
        self.notify(T0 + 65 + 60)
        self.assertEqual(self.mails(), [])

        self.build(T0 + 65 + HOLD)
        self.notify(T0 + 65 + HOLD)
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertEqual(
            mails[0]["subject"],
            "Codex: weekly limits reset on two of our accounts — not announced yet",
        )
        body = mails[0]["text_body"]
        self.assertIn("Seen on two of our Codex accounts before anyone announced it: account a", body)
        self.assertIn("and account b", body)
        self.assertIn("No banked credit was spent and the account did not change.", body)
        self.assertEqual({k for k, *_ in self.deliveries()}, {key})

    def test_both_codex_accounts_at_their_limit_stay_private(self):
        self.export("openai", self.codex_pair_polls(before=(97, 100)))
        data = self.build(T0 + 65 + HOLD)
        self.assertEqual(self.observations(data, "openai"), [])
        self.notify(T0 + 65 + HOLD)
        self.assertEqual(self.mails(), [])


class OneResetOneIncidentFlowTests(FlowCase):
    """A vendor reset clears every window it touches; it is still one mail.

    Review case [14]: the per-model weekly window cleared one poll after
    weekly_all on both Claude accounts, each window family formed its own
    pair, and every subscriber got two identical "not announced yet" mails
    under two probe keys. Pairing is now judged on the primary weekly window
    alone (attribution.PAIRING_FAMILIES).
    """

    def setUp(self):
        super().setUp()
        health = json.loads((self.state / "probe_health.json").read_text())
        health["codex:b"] = {**health["codex"], "label": "codex:b"}
        (self.state / "probe_health.json").write_text(json.dumps(health))

    def mailed_once_per_recipient(self, vendor, at):
        data = self.build(at)
        rows = self.observations(data, vendor)
        self.notify(at)
        self.build(at + 300)
        self.notify(at + 300)
        return rows

    def test_a_scoped_window_clearing_a_poll_later_is_not_a_second_incident(self):
        self.export("anthropic", self.claude_window_polls({
            "a": [(48, 55), (0, 55), (0, 0), (0, 0), (0, 0)],
            "b": [(60, 40), (0, 40), (0, 0), (0, 0), (0, 0)],
        }))
        rows = self.mailed_once_per_recipient("anthropic", T0 + 600 + HOLD + 600)
        self.assertEqual([r["verdict"] for r in rows], ["vendor_reset", "vendor_reset"])
        self.assertEqual({r["incident_key"] for r in rows},
                         {probe_incident_key("anthropic", "weekly", T0)})
        self.assertEqual(sum("status" in r for r in rows), 1)
        self.assertEqual(
            [m["subject"] for m in self.mails()],
            ["Claude Code: weekly limits reset on two of our accounts — not announced yet"] * 2,
        )
        self.assertEqual(len({key for key, *_ in self.deliveries()}), 1)

    def test_a_pair_at_its_limit_publishes_nothing_through_a_scoped_window(self):
        # Review case [12]: both weekly_all windows at the limit, the per-model
        # ones well below, all cleared together.
        self.export("anthropic", self.claude_window_polls({
            "a": [(100, 19), (0, 0), (0, 0)],
            "b": [(97, 38), (0, 0), (0, 0)],
        }))
        rows = self.mailed_once_per_recipient("anthropic", T0 + 600 + HOLD)
        self.assertEqual(rows, [])
        self.assertEqual(self.mails(), [])

    def test_codex_s_secondary_window_is_not_a_second_incident(self):
        detector, events = qp.Detector(), []
        accounts = (("a", "placeholder-account-a", "pro", 0), ("b", "placeholder-account-b", "prolite", 5))
        for i in range(4):
            for account, account_id, plan, offset in accounts:
                moment = T0 + 60 * i + offset

                def window(level, cleared_at):
                    return {
                        "usedPercent": level if i < cleared_at else 0, "windowDurationMins": 10080,
                        "resetsAt": T0 + 3 * DAY if i < cleared_at else moment + 7 * DAY,
                    }

                result = {
                    "accountId": account_id,
                    "rateLimitsByLimitId": {
                        "codex": {"limitId": "codex", "planType": plan,
                                  "primary": window(70, 1), "secondary": None},
                        # The secondary limit clears one poll later.
                        "codex_bengalfox": {"limitId": "codex_bengalfox", "planType": plan,
                                            "primary": window(40, 2), "secondary": None},
                    },
                    "rateLimitResetCredits": {"availableCount": 0, "credits": []},
                }
                for row in qp.normalise(result, moment, account):
                    events += detector.observe(row)
        clears = [e for e in events if e.get("kind") == "clear"]
        self.assertEqual({e["window_family"] for e in clears},
                         {"codex/10080", "codex_bengalfox/10080"})
        self.export("openai", events)
        rows = self.mailed_once_per_recipient("openai", T0 + 125 + HOLD + 300)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["incident_key"] for r in rows},
                         {probe_incident_key("openai", "weekly", T0)})
        self.assertEqual(len(self.mails()), 2)
        self.assertEqual(len({key for key, *_ in self.deliveries()}), 1)


class SingleAccountFlowTests(FlowCase):
    def test_c_one_codex_account_confirms_the_post_that_follows(self):
        self.export("openai", self.codex_polls())
        clear_at = T0 + 60
        self.posts["openai"] = [trusted_post(
            "2109132167112782291", clear_at + 120, vendor="openai",
            text="Reset all propagated.", announcer="thsottiaux",
        )]
        data = self.build(clear_at + 600)
        # The clear itself never reaches the feed: one account is private.
        self.assertEqual(self.observations(data, "openai"), [])
        (event,) = data["vendors"]["openai"]["events"]
        self.assertEqual(event["status"], "confirmed")
        self.assertNotIn("merged_from", event)
        self.assertEqual(
            {k: event["observed"][k] for k in ("status", "verdict", "accounts", "lead_minutes")},
            {"status": "confirmed", "verdict": "unattributed", "accounts": ["a"], "lead_minutes": 2},
        )
        self.notify(clear_at + 600)
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertEqual(mails[0]["subject"], "Codex: usage limits reset")
        body = mails[0]["text_body"]
        self.assertIn("Seen on our Codex account a at", body)
        self.assertIn("2 min before this post", body)
        self.assertIn("No banked credit was spent and the account did not change.", body)
        self.assertEqual({claim for *_, claim in self.deliveries()}, {"settled"})

    def test_c_a_clear_after_the_post_sends_the_one_promised_confirmation(self):
        # The post first, then our account 30 min later: the first mail could
        # not say it was seen, so it owed one follow-up, and the clear pays it.
        self.posts["openai"] = [trusted_post(
            "2109132167112782291", T0 - 1800, vendor="openai",
            text="Reset all propagated.", announcer="thsottiaux",
        )]
        self.export("openai", [])
        self.build(T0 - 1700)
        self.notify(T0 - 1700)
        self.assertEqual(len(self.mails()), 2)
        self.assertEqual({claim for *_, claim in self.deliveries()}, {"pending"})

        self.export("openai", self.codex_polls())
        data = self.build(T0 + 700)
        self.assertEqual(data["vendors"]["openai"]["events"][0]["status"], "confirmed")
        self.notify(T0 + 700)
        self.notify(T0 + 1000)
        follow_ups = self.mails()[2:]
        self.assertEqual(len(follow_ups), 2)
        self.assertEqual(follow_ups[0]["subject"], "Codex: the announced reset landed")
        self.assertIn("Seen on our Codex account a at", follow_ups[0]["text_body"])
        self.assertIn("after this post", follow_ups[0]["text_body"])
        self.assertEqual(sorted(stage for _, _, stage, _ in self.deliveries()),
                         ["confirmed", "confirmed", "new", "new"])

    def test_c_a_post_from_a_curated_tracker_is_not_confirmed_by_one_account(self):
        self.export("openai", self.codex_polls())
        post = trusted_post("1", T0 + 180, vendor="openai", text="Reset all propagated.",
                            announcer="thsottiaux")
        post["text_verified"] = False
        self.posts["openai"] = [post]
        data = self.build(T0 + 600)
        (event,) = data["vendors"]["openai"]["events"]
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["observed"]["status"], "not_seen")

    def test_d_one_account_and_no_post_is_nothing_at_all(self):
        self.export("openai", self.codex_polls())
        data = self.build(T0 + 600 + HOLD)
        self.assertEqual(self.observations(data, "openai"), [])
        self.assertEqual(flatten_events(data), [])
        self.notify(T0 + 600 + HOLD)
        self.assertEqual(self.mails(), [])

    def test_e_an_account_change_is_nothing_at_all(self):
        # 2026-10-11 21:03 UTC, account a: a /login to another org, the weekly
        # window reading 100% on one account and 1% on the next.
        events = self.claude_polls(
            {"a": (100, 1)}, fps={"a": [FP_A, FP_C, FP_C], "b": [FP_B] * 3},
        )
        self.assertTrue(any(e.get("kind") == "account_change" for e in events))
        self.export("anthropic", events)
        data = self.build(T0 + 600 + HOLD)
        self.assertEqual(self.observations(data, "anthropic"), [])
        self.notify(T0 + 600 + HOLD)
        self.assertEqual(self.mails(), [])


if __name__ == "__main__":
    unittest.main()
