"""The second Codex account (docs/reset-attribution.md, "Second Codex account").

Account b is a separate Pro Lite account, read through its own
`codex app-server` started with CODEX_HOME set in that child alone. It is the
discriminator the measured data says works: it cleared with the Pro account
on 2026-10-02 and 10-11 (the vendor) and did not move on 10-04 and 10-10 (the
Pro account's own credits). These tests hold what the contract asks of it:

  * b's rows are prefixed `b:` and labelled `b`; a's ids, cursor slots and
    event ids are the ones a cursor written before b existed already holds;
  * each child is started with its own account's home, and b's never reaches
    this process's environment;
  * a failure on b, of any kind, never blinds a or ends the process;
  * each account has its own health block, `codex` and `codex:b`;
  * the export speaks for both, and a clear on each pairs into a vendor reset.

Offline throughout: the app-server is either a mock or a fake `codex` script
in a temp dir that answers with the home it was started under. Nothing here
reads a real Codex home or spends anything.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import quota_probe as qp
from tests.test_quota_probe import (
    RPC_RESULT,
    WEEK,
    WEEK_SECONDS,
    _StopLoop,
    captured_log,
    fp,
    temp_state,
)

# Placeholders standing in for two different ChatGPT accounts' ids.
ACCOUNT_A = "placeholder-account-a"
ACCOUNT_B = "placeholder-account-b"


def rpc(used, resets_at, *, account_id, plan="pro", credits=0):
    """One `account/rateLimits/read` result for one weekly window."""
    return {
        "accountId": account_id,
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": used, "windowDurationMins": WEEK, "resetsAt": resets_at},
                "secondary": None,
                "planType": plan,
            },
        },
        "rateLimitResetCredits": {"availableCount": credits, "credits": []},
    }


def by_home(outcomes):
    """A read_rate_limits stand-in answering per account, by the home it is given.

    `outcomes` maps a home (None for account a) to a result, an exception to
    raise, or a list of either, consumed one per call.
    """
    queues = {home: list(o) if isinstance(o, list) else None for home, o in outcomes.items()}

    def read(timeout=qp.APP_SERVER_TIMEOUT, *, codex_home=None):
        queue = queues[codex_home]
        outcome = queue.pop(0) if queue is not None else outcomes[codex_home]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return read


@contextlib.contextmanager
def second_account(home="/srv/codex-b"):
    """temp_state, with account b configured at `home` and account a elsewhere."""
    with temp_state() as state, mock.patch.object(qp, "CODEX_HOME_B", home), \
            mock.patch.dict(os.environ, {"CODEX_HOME": "/srv/codex-a"}):
        yield state


def drive(read, *, once=False):
    """run() over a scripted read; (exit code or the exception, the log)."""
    with mock.patch.object(qp, "read_rate_limits", side_effect=read), \
            mock.patch.object(qp.time, "sleep"), captured_log() as out:
        try:
            outcome = qp.run(once=once, with_upstream=False)
        except BaseException as exc:  # SystemExit is not an Exception
            outcome = exc
    return outcome, out.getvalue()


def rows_written():
    if not qp.SAMPLES_FILE.exists():
        return []
    return [json.loads(line) for line in qp.SAMPLES_FILE.read_text().splitlines()]


def events_written():
    if not qp.EVENTS_FILE.exists():
        return []
    return [json.loads(line) for line in qp.EVENTS_FILE.read_text().splitlines()]


# ─── Rows ────────────────────────────────────────────────────────────────────


class RowTest(unittest.TestCase):
    def test_account_b_s_rows_are_prefixed_and_labelled(self):
        rows = qp.normalise(RPC_RESULT, 100, "b")
        self.assertEqual(
            sorted({row["limit_id"] for row in rows}), ["b:codex", "b:codex_secondary"]
        )
        self.assertEqual({row["account"] for row in rows}, {"b"})
        self.assertEqual(
            sorted(qp.slot_key(row) for row in rows),
            ["b:codex/10080", "b:codex_secondary/10080", "b:codex_secondary/300"],
        )

    def test_account_a_s_rows_keep_the_ids_they_always_had(self):
        rows = qp.normalise(RPC_RESULT, 100)
        self.assertEqual(sorted({row["limit_id"] for row in rows}), ["codex", "codex_secondary"])
        self.assertEqual({row["account"] for row in rows}, {"a"})

    def test_a_slot_key_names_its_account(self):
        self.assertEqual(qp.account_of_slot("b:codex/10080"), "b")
        for key in ("codex/10080", "codex_secondary/300", None, ""):
            self.assertEqual(qp.account_of_slot(key), "a")

    def test_a_record_with_no_label_is_its_slot_s_account(self):
        self.assertEqual(qp.record_account({"slot": "b:codex/10080"}), "b")
        self.assertEqual(qp.record_account({"slot": "codex/10080"}), "a")
        self.assertEqual(qp.record_account({"account": "b", "slot": "codex/10080"}), "b")

    def test_both_accounts_clears_share_one_window_family(self):
        d = qp.Detector()
        families = set()
        for account, account_id in (("a", ACCOUNT_A), ("b", ACCOUNT_B)):
            for t, used, anchor in ((0, 80, 1_000_000), (60, 0, 60 + WEEK_SECONDS)):
                for row in qp.normalise(rpc(used, anchor, account_id=account_id), t, account):
                    d.observe(row)
            families |= {state["pending"]["window_family"] for key, state in d.slots.items()
                         if qp.account_of_slot(key) == account and state.get("pending")}
        self.assertEqual(families, {"codex/10080"})


# ─── Which home each child reads ─────────────────────────────────────────────


FAKE_CODEX = """\
import json, os, sys
home = os.environ.get("CODEX_HOME", "")
for line in sys.stdin:
    if json.loads(line).get("id") != 2:
        continue
    result = {
        "accountId": "home:" + home,
        "rateLimitsByLimitId": {"codex": {
            "limitId": "codex", "planType": "pro", "secondary": None,
            "primary": {"usedPercent": 14, "windowDurationMins": 10080, "resetsAt": 1788649350},
        }},
        "rateLimitResetCredits": {"availableCount": 0, "credits": []},
    }
    print(json.dumps({"id": 2, "result": result}), flush=True)
    break
"""


class ChildEnvironmentTest(unittest.TestCase):
    """Each app-server child is started under its own account's home."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.fake = root / "codex"
        self.fake.write_text(f"#!{sys.executable}\n{FAKE_CODEX}", encoding="utf-8")
        self.fake.chmod(0o755)
        self.home_a, self.home_b = root / "home-a", root / "home-b"
        self.home_a.mkdir()
        self.home_b.mkdir()

    def run_once(self, process_home):
        """One real round through run(): (exit code, rows, CODEX_HOME afterwards)."""
        with temp_state(), mock.patch.object(qp, "CODEX_BIN", str(self.fake)), \
                mock.patch.object(qp, "CODEX_HOME_B", str(self.home_b)), \
                mock.patch.dict(os.environ, {"CODEX_HOME": process_home}), captured_log():
            code = qp.run(once=True, with_upstream=False)
            return code, rows_written(), os.environ.get("CODEX_HOME")

    def test_each_account_s_child_reads_its_own_home(self):
        code, rows, after = self.run_once(str(self.home_a))
        self.assertEqual(code, 0)
        prints = {row["account"]: row["account_fp"] for row in rows}
        self.assertEqual(prints["a"], fp("account", f"home:{self.home_a}"))
        self.assertEqual(prints["b"], fp("account", f"home:{self.home_b}"))
        # Account b's home was the child's alone: every later read of account
        # a still starts from the home this process was given.
        self.assertEqual(after, str(self.home_a))

    def test_a_leaked_interactive_codex_home_does_not_move_account_b(self):
        # The owner's login shell exports CODEX_HOME for a profile-based Codex.
        # Should it ever reach this process, account b must still read b.
        code, rows, _ = self.run_once("/home/bogus/interactive")
        self.assertEqual(code, 0)
        (b,) = [row for row in rows if row["account"] == "b"]
        self.assertEqual(b["account_fp"], fp("account", f"home:{self.home_b}"))

    def test_the_spawn_itself_carries_each_home(self):
        # The same property at the Popen boundary: account a's child inherits
        # this process's environment unchanged, account b's is a copy of it
        # with CODEX_HOME replaced, and nothing else in it moved.
        calls = []

        def popen(*args, **kwargs):
            calls.append(kwargs.get("env", "unset"))
            raise FileNotFoundError("no codex in this test")

        with mock.patch.dict(os.environ, {"CODEX_HOME": "/srv/codex-a", "MARKER": "kept"}), \
                mock.patch.object(qp.subprocess, "Popen", side_effect=popen):
            for home in (None, "/srv/codex-b"):
                with self.assertRaises(qp.ProbeError):
                    qp.read_rate_limits(codex_home=home)
            self.assertEqual(os.environ["CODEX_HOME"], "/srv/codex-a")
        self.assertIsNone(calls[0])
        self.assertEqual(calls[1]["CODEX_HOME"], "/srv/codex-b")
        self.assertEqual(calls[1]["MARKER"], "kept")


# ─── Which second home is read at all ────────────────────────────────────────


class SecondHomeTest(unittest.TestCase):
    def resolve(self, raw, codex_home="/srv/codex-a"):
        with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home}), captured_log() as out:
            return qp.second_codex_home(raw), out.getvalue()

    def test_no_second_home_is_no_second_account(self):
        self.assertEqual(self.resolve(""), (None, ""))
        self.assertEqual(self.resolve("   "), (None, ""))

    def test_an_absolute_home_is_read(self):
        self.assertEqual(self.resolve("/srv/codex-b"), ("/srv/codex-b", ""))

    def test_a_relative_home_is_refused_out_loud(self):
        home, log = self.resolve("codex-b")
        self.assertIsNone(home)
        self.assertIn("not an absolute path", log)

    def test_account_a_s_own_home_is_never_read_twice(self):
        # One account read twice would be two fingerprints-equal "accounts".
        home, log = self.resolve("/srv/codex-a/")
        self.assertIsNone(home)
        self.assertIn("account a's own home", log)

    def test_account_a_s_home_is_found_through_a_symlink_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            (Path(tmp) / "link").symlink_to(real)
            home, _ = self.resolve(str(Path(tmp) / "link"), codex_home=str(real))
        self.assertIsNone(home)

    def test_without_codex_home_account_a_is_the_default_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {key: value for key, value in os.environ.items() if key != "CODEX_HOME"}
            with mock.patch.dict(os.environ, {**env, "HOME": tmp}, clear=True), \
                    captured_log() as out:
                self.assertIsNone(qp.second_codex_home(str(Path(tmp) / ".codex")))
                self.assertEqual(qp.second_codex_home(str(Path(tmp) / "b")), str(Path(tmp) / "b"))
        self.assertIn("account a's own home", out.getvalue())


# ─── A failure on b never blinds a ───────────────────────────────────────────


class FailureIsolationTest(unittest.TestCase):
    def health(self):
        return qp.load_health()

    def test_account_b_failing_every_poll_never_blinds_or_ends_account_a(self):
        with second_account():
            outcome, log = drive(by_home({
                None: [RPC_RESULT] * (qp.BLIND_EXIT_FAILURES + 2) + [_StopLoop()],
                "/srv/codex-b": qp.ProbeError("app-server rejected the read: http 401"),
            }))
            health = self.health()
        self.assertIsInstance(outcome, _StopLoop)
        self.assertNotIn("PROBE EXITING", log)
        a, b = health["codex"], health["codex:b"]
        self.assertEqual(a["consecutive_failures"], 0)
        self.assertIsNone(a["blind_since"])
        self.assertIsNotNone(a["last_ok_at"])
        self.assertEqual(b["label"], "codex:b")
        self.assertEqual(b["consecutive_failures"], qp.BLIND_EXIT_FAILURES + 2)
        self.assertIsNotNone(b["blind_since"])
        self.assertIsNone(b["last_ok_at"])
        self.assertIn("http 401", b["last_error"])
        self.assertIn("PROBE FAILED codex:b (1 in a row)", log)
        self.assertIn("PROBE BLIND codex:b: 5 consecutive failures", log)

    def test_an_unexpected_error_on_b_is_b_s_failure_alone(self):
        with second_account():
            outcome, log = drive(by_home({
                None: [RPC_RESULT, RPC_RESULT, _StopLoop()],
                "/srv/codex-b": ValueError("a malformed answer"),
            }))
            health = self.health()
        self.assertIsInstance(outcome, _StopLoop)
        self.assertEqual(health["codex"]["consecutive_failures"], 0)
        self.assertEqual(health["codex:b"]["last_error"], "ValueError: a malformed answer")

    def test_an_unexpected_error_on_a_still_ends_the_process(self):
        # Account a's semantics are unchanged: only ProbeError is a failed poll.
        with second_account():
            outcome, _ = drive(by_home({None: ValueError("bug"), "/srv/codex-b": RPC_RESULT}))
        self.assertIsInstance(outcome, ValueError)

    def test_account_a_failing_still_exits_for_systemd_while_b_reads(self):
        with second_account():
            outcome, log = drive(by_home({
                None: qp.ProbeError("http 404"), "/srv/codex-b": RPC_RESULT,
            }))
            health = self.health()
        self.assertIsInstance(outcome, SystemExit)
        self.assertEqual(outcome.code, 2)
        self.assertEqual(health["codex"]["consecutive_failures"], qp.BLIND_EXIT_FAILURES)
        self.assertEqual(health["codex:b"]["consecutive_failures"], 0)
        self.assertIn("PROBE EXITING", log)

    def test_account_a_is_read_and_persisted_before_account_b(self):
        seen_by_b = []

        def read_b():
            seen_by_b.append({row["account"] for row in rows_written()})
            return RPC_RESULT

        with second_account():
            reads = by_home({None: RPC_RESULT, "/srv/codex-b": None})

            def read(timeout=qp.APP_SERVER_TIMEOUT, *, codex_home=None):
                return read_b() if codex_home else reads(codex_home=None)

            outcome, _ = drive(read, once=True)
        self.assertEqual(outcome, 0)
        self.assertEqual(seen_by_b, [{"a"}])

    def test_once_fails_when_any_configured_account_did_not_read(self):
        with second_account():
            outcome, _ = drive(
                by_home({None: RPC_RESULT, "/srv/codex-b": qp.ProbeError("x")}), once=True
            )
        self.assertEqual(outcome, 1)
        with second_account():
            outcome, _ = drive(by_home({None: RPC_RESULT, "/srv/codex-b": RPC_RESULT}), once=True)
        self.assertEqual(outcome, 0)


# ─── One health block per account ────────────────────────────────────────────


class HealthPerAccountTest(unittest.TestCase):
    def detector(self):
        d = qp.Detector()
        for row in qp.normalise(RPC_RESULT, 500):
            d.observe(row)
        for row in qp.normalise(rpc(3, 1_000, account_id=ACCOUNT_B), 700, "b"):
            d.observe(row)
        return d

    def test_each_block_reports_only_its_own_account_s_slots(self):
        d = self.detector()
        a = qp.health_block(d, 800, label="codex", account="a")
        b = qp.health_block(d, 800, label="codex:b", account="b")
        self.assertEqual(
            a["detectable_now"],
            {"codex/10080": True, "codex_secondary/300": False, "codex_secondary/10080": False},
        )
        self.assertEqual(b["detectable_now"], {"b:codex/10080": False})
        self.assertEqual((a["last_sample_t"], b["last_sample_t"]), (500, 700))

    def test_a_single_account_probe_still_reports_every_slot(self):
        block = qp.health_block(self.detector(), 800)
        self.assertEqual(len(block["detectable_now"]), 4)
        self.assertEqual(block["last_sample_t"], 700)

    def test_both_blocks_are_written_every_round(self):
        with second_account():
            drive(by_home({None: RPC_RESULT, "/srv/codex-b": RPC_RESULT}), once=True)
            health = qp.load_health()
        self.assertEqual(sorted(health), ["codex", "codex:b"])
        self.assertEqual(health["codex:b"]["detectable_now"], {
            "b:codex/10080": True, "b:codex_secondary/300": False,
            "b:codex_secondary/10080": False,
        })

    def test_an_account_taken_out_leaves_no_block_behind(self):
        with temp_state():
            qp.write_health({"label": "codex:b", "updated_at": 1}, label="codex:b")
            qp.write_health({"label": "claude:a", "updated_at": 1}, label="claude:a")
            drive(by_home({None: RPC_RESULT}), once=True)
            health = qp.load_health()
        self.assertEqual(sorted(health), ["claude:a", "codex"])

    def test_dropping_an_absent_block_writes_nothing(self):
        with temp_state() as state:
            qp.drop_health("codex:b")
            self.assertFalse((state / "probe_health.json").exists())


# ─── Account a carries on from a cursor written before account b ─────────────


class CursorContinuityTest(unittest.TestCase):
    def test_account_a_resumes_its_slots_and_event_ids_from_an_old_cursor(self):
        now = int(time.time())
        anchor = now + 3 * 86400
        # The live cursor's shape before stage 1: unlabelled rows, no
        # fingerprint, and a second limit id beside the weekly one.
        old_row = {
            "t": now - 60, "limit_id": "codex", "slot": "primary", "window_minutes": WEEK,
            "used_percent": 80.0, "resets_at": anchor, "plan_type": "pro",
            "credits_available": 1,
        }
        cursor = {
            "slots": {
                "codex/10080": {
                    "last": old_row, "active_anchor": anchor,
                    "credits_known": {"value": 1, "t": now - 60}, "credits_absent": 0,
                    "pending": None,
                },
                "codex_secondary/300": {
                    "last": {**old_row, "limit_id": "codex_secondary", "window_minutes": 300,
                             "used_percent": 0.0},
                    "credits_known": {"value": 1, "t": now - 60}, "credits_absent": 0,
                },
            },
            "upstream_sigs": {"codex-resets.com": "kept"},
        }
        cleared = rpc(0, anchor + 4 * 86400, account_id=ACCOUNT_A, credits=1)
        with second_account():
            qp.CURSOR_FILE.write_text(json.dumps(cursor), encoding="utf-8")
            outcome, _ = drive(by_home({
                None: [cleared, cleared, _StopLoop()],
                "/srv/codex-b": rpc(40, anchor, account_id=ACCOUNT_B, plan="prolite"),
            }))
            saved = json.loads(qp.CURSOR_FILE.read_text(encoding="utf-8"))
            events = events_written()
        self.assertIsInstance(outcome, _StopLoop)
        slots = saved["slots"]
        self.assertTrue({"codex/10080", "codex_secondary/300"} <= set(slots))
        self.assertEqual(sorted(k for k in slots if k.startswith("b:")), ["b:codex/10080"])
        self.assertEqual(sorted(k for k in slots if not k.startswith("b:")),
                         ["codex/10080", "codex_secondary/300"])
        self.assertEqual(saved["upstream_sigs"], {"codex-resets.com": "kept"})
        (clear,) = [e for e in events if e.get("kind") == qp.EVENT_CLEAR]
        # The id and slot a cursor from before this change would have given it,
        # judged against the anchor that cursor carried.
        self.assertEqual(clear["slot"], "codex/10080")
        self.assertEqual(clear["event_id"], f"codex/10080:{clear['detected_at']}")
        self.assertEqual(clear["clear_bracket_lo"], now - 60)
        self.assertEqual(clear["prior_active_resets_at"], anchor)
        self.assertEqual(clear["account"], "a")


# ─── The export speaks for both accounts ─────────────────────────────────────


def block(label, *, ok_at=900, updated_at=900, **extra):
    return {"label": label, "updated_at": updated_at, "last_ok_at": ok_at,
            "consecutive_failures": 0, "blind_since": None, **extra}


def export(health=None, events=(), samples=(), cursor=None):
    with temp_state() as state:
        for label, entry in (health or {}).items():
            qp.write_health(entry, label=label)
        qp.append_jsonl(qp.EVENTS_FILE, list(events))
        qp.append_jsonl(qp.SAMPLES_FILE, list(samples))
        if cursor is not None:
            qp.save_cursor(cursor)
        target = state / "observed" / "openai.json"
        with captured_log():
            qp.export_observations(target, now=999)
        return json.loads(target.read_text(encoding="utf-8"))


class ExportProbeBlockTest(unittest.TestCase):
    def test_one_account_exports_as_it_always_did_plus_its_own_entry(self):
        probe = export({"codex": block("codex")})["probe"]
        self.assertEqual(probe["status"], "ok")
        self.assertEqual(probe["label"], "codex")
        self.assertEqual(list(probe["accounts"]), ["codex"])

    def test_two_healthy_accounts_are_ok_and_as_fresh_as_the_older(self):
        probe = export({"codex": block("codex", ok_at=950), "codex:b": block("codex:b", ok_at=900)})["probe"]
        self.assertEqual(probe["status"], "ok")
        self.assertEqual(probe["last_verified_at"], qp.iso_utc(900))
        self.assertEqual(sorted(probe["accounts"]), ["codex", "codex:b"])
        self.assertEqual(probe["accounts"]["codex:b"]["status"], "ok")

    def test_a_blind_account_b_is_not_a_clean_look(self):
        probe = export({
            "codex": block("codex"),
            "codex:b": block("codex:b", blind_since=800, consecutive_failures=12,
                             last_error="http 401"),
        })["probe"]
        self.assertEqual(probe["status"], "blind")
        self.assertEqual(probe["accounts"]["codex:b"]["status"], "blind")
        self.assertEqual(probe["accounts"]["codex:b"]["last_error"], "http 401")
        # Account a's own fields stay where every reader already looks.
        self.assertIsNone(probe["last_error"])

    def test_an_account_b_that_never_read_leaves_no_verified_time(self):
        probe = export({"codex": block("codex"), "codex:b": block("codex:b", ok_at=None)})["probe"]
        self.assertEqual(probe["status"], "blind")
        self.assertIsNone(probe["last_verified_at"])

    def test_both_accounts_detectable_slots_are_carried(self):
        probe = export({
            "codex": block("codex", detectable_now={"codex/10080": True}),
            "codex:b": block("codex:b", detectable_now={"b:codex/10080": False}),
        })["probe"]
        self.assertEqual(probe["detectable_now"], {"codex/10080": True, "b:codex/10080": False})

    def test_with_no_health_file_the_cursor_speaks_for_account_a_only(self):
        d = qp.Detector()
        for row in qp.normalise(RPC_RESULT, 500):
            d.observe(row)
        for row in qp.normalise(RPC_RESULT, 700, "b"):
            d.observe(row)
        probe = export(cursor=d.cursor())["probe"]
        self.assertEqual(probe["status"], "absent")
        self.assertEqual(sorted(probe["detectable_now"]),
                         ["codex/10080", "codex_secondary/10080", "codex_secondary/300"])

    def test_combined_status_rules(self):
        ok, blind = block("x", updated_at=990), block("x", updated_at=990, blind_since=980)
        never = {"label": "x"}
        self.assertEqual(qp.export_status([ok, ok], 1000), ("ok", 900))
        self.assertEqual(qp.export_status([ok, blind], 1000), ("blind", 900))
        self.assertEqual(qp.export_status([never, never], 1000), ("absent", None))
        self.assertEqual(qp.export_status([ok, never], 1000), ("blind", None))


class CoverageTest(unittest.TestCase):
    FIRST_A, FIRST_B = 1788082616, 1791800000

    def samples(self, b_peak=3.0):
        a = [{"t": self.FIRST_A, "limit_id": "codex", "window_minutes": WEEK, "used_percent": 80.0},
             {"t": self.FIRST_A + 60, "limit_id": "codex", "window_minutes": WEEK, "used_percent": 0.0}]
        b = [{"t": self.FIRST_B, "limit_id": "b:codex", "window_minutes": WEEK, "used_percent": b_peak},
             {"t": self.FIRST_B + 60, "limit_id": "b:codex", "window_minutes": WEEK, "used_percent": 0.0}]
        return a + b

    def cursor(self, plan_b="prolite"):
        return {"slots": {
            "codex/10080": {"last": {"t": 2, "plan_type": "pro"}},
            "b:codex/10080": {"last": {"t": 3, "plan_type": plan_b}},
        }}

    def test_two_accounts_name_both_plans_and_the_quiet_one(self):
        probe = export({"codex": block("codex"), "codex:b": block("codex:b")},
                       samples=self.samples(), cursor=self.cursor())["probe"]
        self.assertEqual(
            probe["coverage"],
            "Two Codex accounts (Pro and Pro Lite), weekly window only. Account b's "
            "weekly window has never read above 3% on all 2 readings taken since "
            f"{qp.format_epoch_pacific(self.FIRST_B)}, under the "
            f"{qp.CLEAR_DROP_MIN:g}-point drop a clear needs, so nothing can be observed on it.",
        )

    def test_two_busy_accounts_on_one_plan(self):
        probe = export({"codex": block("codex"), "codex:b": block("codex:b")},
                       samples=self.samples(b_peak=60.0), cursor=self.cursor("pro"))["probe"]
        self.assertEqual(probe["coverage"], "Two Codex Pro accounts, weekly window only.")

    def test_a_single_reading_is_not_all_of_one(self):
        # Right after b is added its idle windows have written one row each.
        probe = export({"codex": block("codex"), "codex:b": block("codex:b")},
                       samples=self.samples()[:3], cursor=self.cursor())["probe"]
        self.assertIn("Account b's weekly window has never read above 3% on the one "
                      f"reading taken since {qp.format_epoch_pacific(self.FIRST_B)}",
                      probe["coverage"])
        self.assertEqual(qp.readings_taken(0, " since then"), " since then")

    def test_an_account_b_with_no_samples_says_so(self):
        probe = export({"codex": block("codex"), "codex:b": block("codex:b")},
                       samples=self.samples()[:2], cursor=self.cursor())["probe"]
        self.assertIn("Account b has no samples on record", probe["coverage"])

    def test_account_b_s_history_does_not_outlive_its_block(self):
        # Taken out: the samples stay in the log, the second account does not
        # stay in the sentence — not even through a window only b ever had.
        only_b = {"t": self.FIRST_B, "limit_id": "b:codex_secondary", "window_minutes": 300,
                  "used_percent": 0.0}
        probe = export({"codex": block("codex")}, samples=[*self.samples(), only_b],
                       cursor=self.cursor())["probe"]
        self.assertEqual(probe["coverage"], "One Codex Pro account, weekly window only.")

    def test_each_account_s_plan_is_its_newest_row_s(self):
        # A slot the RPC stopped reporting keeps the plan it last read; the
        # account is on the plan its newest row says.
        d = qp.Detector({"slots": {
            "codex_secondary/300": {"last": {"t": 2, "plan_type": "plus"}},
            "codex/10080": {"last": {"t": 5, "plan_type": "pro"}},
            "b:codex/10080": {"last": {"t": 4, "plan_type": "prolite"}},
            "b:codex_secondary/300": {"last": {"t": 1, "plan_type": None}},
            "codex_broken/300": {"last": {"plan_type": "free"}},
        }})
        self.assertEqual(qp.account_plans(d), {"a": "pro", "b": "prolite"})

    def test_a_window_only_account_b_lacks_is_named(self):
        sentence = qp.coverage_sentence(
            {"codex/10080": 80.0, "b:codex_secondary/300": 50.0}, self.FIRST_A, "pro",
            others={"b": "prolite"},
        )
        self.assertIn("Account a has no 5-hour samples on record", sentence)
        self.assertIn("Account b has no weekly samples on record", sentence)

    def test_the_population_names_a_plan_only_when_every_account_s_was_read(self):
        self.assertEqual(qp.population_phrase({"a": "pro", "b": None}), "Two Codex accounts")
        self.assertEqual(qp.population_phrase({"a": "pro"}), "One Codex Pro account")
        self.assertEqual(qp.population_phrase({"a": None}), "One Codex account")
        self.assertEqual(qp.plan_name("prolite"), "Pro Lite")
        self.assertEqual(qp.plan_name("plus"), "Plus")


# ─── Pairing and the published words ─────────────────────────────────────────


def clears_on_both(t0=1_791_700_000, b_offset=10, used_a=80, used_b=60):
    """Both accounts' weekly windows clearing early, every own-cause excluded."""
    d = qp.Detector()
    events = []
    for account, account_id, start, used, plan in (
        ("a", ACCOUNT_A, t0, used_a, "pro"), ("b", ACCOUNT_B, t0 + b_offset, used_b, "prolite"),
    ):
        for i, level in enumerate((used, 0, 0)):
            anchor = t0 + 3 * 86400 if i == 0 else start + 60 * i + WEEK_SECONDS
            result = rpc(level, anchor, account_id=account_id, plan=plan)
            for row in qp.normalise(result, start + 60 * i, account):
                events.extend(d.observe(row))
    return events


class PairingTest(unittest.TestCase):
    def test_a_clear_on_each_account_is_one_vendor_reset(self):
        events = clears_on_both()
        self.assertEqual([e["classification"] for e in events], ["unattributed"] * 2)
        rows = export({"codex": block("codex"), "codex:b": block("codex:b")},
                      events=events)["observations"]
        by_account = {row["account"]: row for row in rows}
        self.assertEqual({row["verdict"] for row in rows}, {"vendor_reset"})
        self.assertTrue(all(row["public"] for row in rows))
        self.assertEqual(by_account["a"]["witness"], by_account["b"]["event_id"])
        self.assertEqual(by_account["b"]["witness"], by_account["a"]["event_id"])
        self.assertTrue(by_account["b"]["event_id"].startswith("b:codex/10080:"))

    def test_one_stale_read_on_a_does_not_break_the_pair(self):
        # The 2026-10-11 backtest: a paired reset, then one read of a's
        # pre-reset level on its pre-reset anchor (the 2026-09-09 replica
        # shape) while a still idles. One read used to retract a's clear, and
        # withdrawn_pairs() then mailed a correction for a reset that stood.
        from scripts import attribution
        t0, old_anchor = 1_791_700_000, 1_791_700_000 + 3 * 86400
        d = qp.Detector()
        events = []

        def read(account, account_id, plan, t, used, anchor):
            result = rpc(used, anchor, account_id=account_id, plan=plan)
            for row in qp.normalise(result, t, account):
                events.extend(d.observe(row))

        for account, account_id, start, plan in (
            ("a", ACCOUNT_A, t0, "pro"), ("b", ACCOUNT_B, t0 + 10, "prolite"),
        ):
            for i, level in enumerate((80, 0, 2)):
                anchor = old_anchor if i == 0 else start + 60 * i + WEEK_SECONDS
                read(account, account_id, plan, start + 60 * i, level, anchor)
        stale_at = t0 + 60 * 200
        read("a", ACCOUNT_A, "pro", stale_at, 80, old_anchor)
        read("a", ACCOUNT_A, "pro", stale_at + 60, 2, stale_at + 60 + WEEK_SECONDS)
        self.assertEqual([e["kind"] for e in events], [qp.EVENT_CLEAR] * 2)
        self.assertEqual(attribution.withdrawn_pairs(events), [])
        rows = export({"codex": block("codex"), "codex:b": block("codex:b")},
                      events=events)["observations"]
        self.assertEqual({row["verdict"] for row in rows}, {"vendor_reset"})
        self.assertTrue(all(row["public"] for row in rows))
        # A revert that HOLDS is still the clear un-happening: it breaks the pair.
        read("a", ACCOUNT_A, "pro", stale_at + 120, 80, old_anchor)
        read("a", ACCOUNT_A, "pro", stale_at + 180, 80, old_anchor)
        self.assertEqual(events[-1]["kind"], qp.EVENT_RETRACTION)
        self.assertEqual(len(attribution.withdrawn_pairs(events)), 1)

    def test_account_a_alone_stays_private(self):
        events = [e for e in clears_on_both() if e["account"] == "a"]
        (row,) = export({"codex": block("codex")}, events=events)["observations"]
        self.assertEqual(row["verdict"], "unattributed")
        self.assertFalse(row["public"])

    def test_two_accounts_at_their_limit_stay_private(self):
        events = clears_on_both(used_a=97, used_b=100)
        rows = export({"codex": block("codex")}, events=events)["observations"]
        self.assertEqual({row["verdict"] for row in rows}, {"unattributed"})

    def test_account_b_s_sentences_say_whose_window_it_is(self):
        rows = export({"codex": block("codex")}, events=clears_on_both())["observations"]
        by_account = {row["account"]: row for row in rows}
        self.assertTrue(by_account["b"]["headline"].startswith(
            "Our Codex weekly window on account b cleared"))
        self.assertIn("Our account a's Codex weekly window cleared too", by_account["b"]["evidence"])
        # Account a's words are the ones its rows always carried.
        self.assertTrue(by_account["a"]["headline"].startswith("Our Codex weekly window cleared"))
        self.assertIn("Our account b's Codex weekly window cleared too", by_account["a"]["evidence"])

    def test_every_sentence_kind_names_account_b(self):
        base = {"account": "b", "slot": "b:codex/10080", "window_minutes": WEEK}
        for kind, verdict in (
            (qp.EVENT_CLEAR, qp.VERDICT_NATURAL_EXPIRY),
            (qp.EVENT_CLEAR, qp.VERDICT_UNRESOLVED),
            (qp.EVENT_CREDIT_GRANTED, qp.VERDICT_CREDIT_GRANTED),
            (qp.EVENT_ACCOUNT_CHANGE, qp.VERDICT_ACCOUNT_CHANGE),
            (qp.EVENT_LIMIT_CHANGE, qp.VERDICT_LIMIT_CHANGE),
            (qp.EVENT_RETRACTION, None),
        ):
            with self.subTest(kind=kind, verdict=verdict):
                headline, evidence = qp.observation_sentences({**base, "kind": kind}, verdict)
                self.assertIn("account b", f"{headline} {evidence}")


class CreditGrantTest(unittest.TestCase):
    def test_a_grant_names_the_account_whose_bank_it_filled(self):
        d = qp.Detector()
        events = []
        for credits in (0, 1):
            for row in qp.normalise(rpc(40, 1_000, account_id=ACCOUNT_B, credits=credits),
                                    100 + credits, "b"):
                events.extend(d.observe(row))
        (grant,) = events
        self.assertEqual((grant["kind"], grant["account"]), (qp.EVENT_CREDIT_GRANTED, "b"))

    def test_grants_on_two_accounts_in_one_second_are_two_observations(self):
        grants = [
            {"kind": qp.EVENT_CREDIT_GRANTED, "event_id": f"{slot}:credit:500", "slot": slot,
             "account": account, "t": 500, "credits_before": 0, "credits_after": 1}
            for account, slot in (("a", "codex/10080"), ("b", "b:codex/10080"))
        ]
        rows = export({"codex": block("codex")}, events=grants)["observations"]
        self.assertEqual(sorted(row["account"] for row in rows), ["a", "b"])

    def test_one_account_s_grant_on_every_window_is_still_one(self):
        grants = [
            {"kind": qp.EVENT_CREDIT_GRANTED, "event_id": f"{slot}:credit:500", "slot": slot,
             "t": 500, "credits_before": 0, "credits_after": 1}
            for slot in ("codex/10080", "codex_secondary/300")
        ]
        rows = export({"codex": block("codex")}, events=grants)["observations"]
        self.assertEqual(len(rows), 1)


# ─── --show ──────────────────────────────────────────────────────────────────


class ShowTest(unittest.TestCase):
    def show(self, read, home_b):
        out = io.StringIO()
        with mock.patch.object(qp, "read_rate_limits", side_effect=read), \
                mock.patch.object(qp, "CODEX_HOME_B", home_b), \
                mock.patch.dict(os.environ, {"CODEX_HOME": "/srv/codex-a"}), \
                contextlib.redirect_stdout(out):
            code = qp.main(["--show"])
        return code, json.loads(out.getvalue())

    def test_one_account_prints_its_rows_as_it_always_did(self):
        code, shown = self.show(by_home({None: RPC_RESULT}), "")
        self.assertEqual(code, 0)
        self.assertIsInstance(shown, list)

    def test_two_accounts_print_one_entry_each_and_name_a_failure(self):
        code, shown = self.show(
            by_home({None: RPC_RESULT, "/srv/codex-b": qp.ProbeError("http 401")}), "/srv/codex-b"
        )
        self.assertEqual(code, 1)
        self.assertEqual(sorted(shown), ["codex", "codex:b"])
        self.assertEqual({row["account"] for row in shown["codex"]}, {"a"})
        self.assertEqual(shown["codex:b"], {"error": "http 401"})

    def test_two_readable_accounts_exit_zero(self):
        code, shown = self.show(
            by_home({None: RPC_RESULT, "/srv/codex-b": RPC_RESULT}), "/srv/codex-b"
        )
        self.assertEqual(code, 0)
        self.assertEqual({row["account"] for row in shown["codex:b"]}, {"b"})


if __name__ == "__main__":
    unittest.main()
