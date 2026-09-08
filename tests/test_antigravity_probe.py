#!/usr/bin/env python3
"""The Google probe, tested entirely against fixtures.

`agy` is not installed on this host and its Google sign-in is interactive, so
there is no live path to test. Everything here drives the module through an
injected fetcher or an injected `subprocess.run`, and the two things that most
need proving are proved directly:

  * the unauthenticated path fails with ONE line that names the install command
    and the login step, whichever of the four ways it fails, and
  * the rows this module produces are consumed by a REAL `quota_probe.Detector`
    and classified the way a Codex row would be — the point of parsing into
    that shape at all.

Nothing here touches /var/lib/ai-resets: the health file and the cursor are
redirected into a temp directory for every test.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import antigravity_probe as agy
from scripts import quota_probe

# ─── Recorded payloads ───────────────────────────────────────────────────────
#
# The documented `v1internal:retrieveUserQuota` body. Kept inline rather than in
# tests/fixtures/ because it is a WRITTEN-DOWN SHAPE, not a measurement: no
# authenticated call has ever been made from this host. The first real response
# belongs in tests/fixtures/antigravity-usage.json, and this constant should be
# replaced by it at that point.

USAGE_PAYLOAD = {
    "buckets": [
        {
            "modelId": "gemini-3-pro",
            "tokenType": "REQUESTS",
            "remainingFraction": 0.4,
            "remainingAmount": 120,
            "resetTime": "2026-09-07T05:00:00Z",
        },
        {
            "modelId": "gemini-3-flash",
            "tokenType": "REQUESTS",
            "remainingFraction": 1.0,
            "resetTime": "2026-09-07T05:00:00Z",
        },
    ]
}

# 2026-09-07T00:00:00Z — exactly five hours before the resetTime above, so the
# inferred window lands on the ladder's 5-hour rung with no rounding slack.
NOW = 1788739200


def payload(*buckets: dict) -> dict:
    return {"buckets": [dict(b) for b in buckets]}


def bucket(**overrides: object) -> dict:
    base = {
        "modelId": "gemini-3-pro",
        "tokenType": "REQUESTS",
        "remainingFraction": 0.4,
        "resetTime": NOW + 5 * 3600,
    }
    base.update(overrides)
    return base


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=["agy"], returncode=returncode, stdout=stdout, stderr=stderr)


class ProbeTestCase(unittest.TestCase):
    """Redirects state off /var/lib and captures the module's log lines."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ai-resets-agy-"))
        self.addCleanup(self._cleanup)
        self.health = self.tmp / "probe_health.json"
        self.cursor = self.tmp / "antigravity_cursor.json"
        self.logs: list[str] = []
        for patcher in (
            mock.patch.object(quota_probe, "HEALTH_FILE", self.health),
            mock.patch.object(quota_probe, "log", self.logs.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _cleanup(self) -> None:
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        self.tmp.rmdir()

    def allow_cadence(self) -> None:
        """Let the loop start: the default 180 s grace refuses a 300 s cadence."""
        patcher = mock.patch.object(
            quota_probe, "NATURAL_GRACE_SECONDS", 2 * agy.POLL_SECONDS + 60
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def health_blocks(self) -> dict:
        return json.loads(self.health.read_text(encoding="utf-8"))


# ─── The unauthenticated path ────────────────────────────────────────────────


class UnavailableMessageTests(ProbeTestCase):
    def test_the_message_is_one_line_and_names_install_and_login(self):
        message = str(agy.unavailable())
        self.assertNotIn("\n", message)
        self.assertIn("curl -fsSL https://antigravity.google/cli/install.sh | bash", message)
        self.assertIn("agy", message)
        self.assertIn("sign-in", message)
        # Verified against antigravity.google/docs/cli/install/ on 2026-09-06:
        # there is no `agy login` and no `agy auth`, so the line must not send
        # an operator looking for one.
        self.assertNotIn("agy login", message)
        self.assertNotIn("agy auth", message)

    def test_missing_binary_gives_the_actionable_line(self):
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(which=lambda _name: None)
        self.assertIn(agy.UNAVAILABLE, str(caught.exception))
        self.assertIn("is not on PATH", str(caught.exception))

    def test_a_sign_in_prompt_on_stderr_gives_the_same_line(self):
        stderr = "Not signed in. Visit https://accounts.google.com/o/oauth2/... and enter code ABCD-EFGH\n"
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(stderr=stderr, returncode=1),
            )
        message = str(caught.exception)
        self.assertIn(agy.UNAVAILABLE, message)
        self.assertIn("Not signed in.", message)
        self.assertNotIn("\n", message)

    def test_the_real_unauthenticated_envelope_sends_the_reader_to_the_login(self):
        """The exact stdout of an unsigned-in `agy` 1.1.27, captured 2026-09-07.

        It is VALID JSON, so the json.loads guard passes it, and the only thing
        stopping it today is the non-zero exit. Pinned here with the exit code
        forced to 0 so the envelope check is what is actually under test: if the
        check were removed, this payload would reach extract_buckets and the
        journal would say "record it as a fixture and re-check the parser"
        against a host whose real problem is that nobody has signed it in.
        """
        envelope = (
            '{"conversation_id":"","status":"ERROR","response":"",'
            '"error":"authentication failed or timed out","duration_seconds":0,'
            '"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,'
            '"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}'
        )
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(stdout=envelope, returncode=0),
            )
        message = str(caught.exception)
        self.assertIn(agy.UNAVAILABLE, message)
        self.assertIn("authentication failed or timed out", message)
        self.assertNotIn("buckets", message)

    def test_a_status_error_with_no_message_is_still_named(self):
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(stdout='{"status":"ERROR"}'),
            )
        self.assertIn("status ERROR", str(caught.exception))

    def test_a_successful_envelope_is_not_mistaken_for_an_error(self):
        # `error` present but empty, and a non-ERROR status, is what a good
        # answer looks like; a truthiness check on the key alone would reject it.
        payload = agy.read_usage(
            which=lambda _name: "/usr/local/bin/agy",
            runner=lambda *a, **k: completed(
                stdout='{"status":"SUCCESS","error":"","buckets":[]}'
            ),
        )
        self.assertEqual(payload["status"], "SUCCESS")

    def test_the_call_budget_outlasts_the_cli_own_sign_in_wait(self):
        # Measured: `agy` waits 60 s for a pasted code before exiting 1 with a
        # usable stderr line. A budget equal to that races it, and losing the
        # race replaces the CLI's own words with a bare "no answer in 60s".
        self.assertGreater(agy.CALL_TIMEOUT_SECONDS, agy.AGY_INTERNAL_AUTH_WAIT_SECONDS)

    def test_a_non_zero_exit_with_no_stderr_still_reports_the_code(self):
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(returncode=7),
            )
        self.assertIn("exit 7", str(caught.exception))

    def test_an_oauth_url_where_json_was_expected_gives_the_same_line(self):
        # `agy` exiting 0 while printing a sign-in URL is the shape a first run
        # takes, and it is not distinguishable from success by exit code alone.
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(stdout="Open https://antigravity.google/auth\n"),
            )
        self.assertIn(agy.UNAVAILABLE, str(caught.exception))
        self.assertIn("Open https://antigravity.google/auth", str(caught.exception))

    def test_empty_stdout_is_named_rather_than_left_blank(self):
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(stdout="   \n"),
            )
        self.assertIn("no JSON on stdout", str(caught.exception))

    def test_a_hang_at_the_interactive_prompt_times_out(self):
        def hang(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="agy", timeout=60)

        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(which=lambda _name: "/usr/local/bin/agy", runner=hang, timeout=60)
        self.assertIn("no answer in 60s", str(caught.exception))
        self.assertIn("interactive sign-in", str(caught.exception))

    def test_an_os_error_gives_the_same_line(self):
        def broken(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(which=lambda _name: "/usr/local/bin/agy", runner=broken)
        self.assertIn(agy.UNAVAILABLE, str(caught.exception))
        self.assertIn("Permission denied", str(caught.exception))

    def test_a_json_array_is_refused(self):
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.read_usage(
                which=lambda _name: "/usr/local/bin/agy",
                runner=lambda *a, **k: completed(stdout="[]"),
            )
        self.assertIn("expected a JSON object", str(caught.exception))

    def test_the_documented_argv_is_what_gets_run(self):
        seen: dict = {}

        def record(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return completed(stdout=json.dumps(USAGE_PAYLOAD))

        result = agy.read_usage(
            which=lambda _name: "/usr/local/bin/agy", runner=record, binary="agy"
        )
        self.assertEqual(seen["argv"], ["agy", "-p", "/usage", "--output-format", "json"])
        self.assertFalse(seen["kwargs"]["check"], "a non-zero exit must reach our own handler")
        self.assertEqual(seen["kwargs"]["timeout"], agy.CALL_TIMEOUT_SECONDS)
        self.assertEqual(result, USAGE_PAYLOAD)


# ─── Envelope and field parsing ──────────────────────────────────────────────


class ExtractBucketsTests(unittest.TestCase):
    def test_top_level_buckets(self):
        self.assertEqual(len(agy.extract_buckets(USAGE_PAYLOAD)), 2)

    def test_one_level_of_cli_envelope(self):
        for key in ("result", "data", "response"):
            with self.subTest(envelope=key):
                self.assertEqual(len(agy.extract_buckets({key: USAGE_PAYLOAD})), 2)

    def test_non_dict_entries_are_dropped(self):
        mixed = {"buckets": [bucket(), "not-a-bucket", None, 7]}
        self.assertEqual(len(agy.extract_buckets(mixed)), 1)

    def test_a_payload_with_no_buckets_raises_and_names_the_endpoint(self):
        # Returning [] here would read as "the account has no quota buckets",
        # which is a claim about the account rather than about our parser.
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.extract_buckets({"error": {"code": 401}})
        self.assertIn("no `buckets` list", str(caught.exception))
        self.assertIn(agy.QUOTA_ENDPOINT, str(caught.exception))


class ResetTimeTests(unittest.TestCase):
    def test_rfc3339(self):
        self.assertEqual(agy.parse_reset_time("2026-09-07T05:00:00Z"), NOW + 5 * 3600)

    def test_offset_form(self):
        self.assertEqual(agy.parse_reset_time("2026-09-06T22:00:00-07:00"), NOW + 5 * 3600)

    def test_epoch_number(self):
        self.assertEqual(agy.parse_reset_time(NOW), NOW)
        self.assertEqual(agy.parse_reset_time(float(NOW) + 0.7), NOW)

    def test_protobuf_seconds(self):
        # google.protobuf.Timestamp serialises as {"seconds": n} through some
        # gRPC-transcoding gateways and as RFC3339 through others.
        self.assertEqual(agy.parse_reset_time({"seconds": NOW}), NOW)
        self.assertEqual(agy.parse_reset_time({"seconds": str(NOW)}), NOW)

    def test_unreadable_values_are_none_not_zero(self):
        # None means "no anchor on record", which classify() turns into
        # `unresolved`. Zero would mean "the window expired in 1970" and make
        # every clear look early.
        for value in (None, "", "tomorrow", True, False, {"nanos": 5}, {"seconds": "x"}, []):
            with self.subTest(value=value):
                self.assertIsNone(agy.parse_reset_time(value))


class WindowInferenceTests(unittest.TestCase):
    def test_five_hours_or_less_is_the_first_rung(self):
        for seconds in (60, 3600, 5 * 3600):
            with self.subTest(seconds=seconds):
                self.assertEqual(agy.infer_window_minutes(seconds, None), (300, True))

    def test_more_than_five_hours_climbs_to_daily(self):
        self.assertEqual(agy.infer_window_minutes(6 * 3600, None)[0], 1440)
        self.assertEqual(agy.infer_window_minutes(24 * 3600, None)[0], 1440)

    def test_more_than_a_day_climbs_to_weekly(self):
        self.assertEqual(agy.infer_window_minutes(48 * 3600, None)[0], 10080)
        self.assertEqual(agy.infer_window_minutes(7 * 86400, None)[0], 10080)

    def test_beyond_the_last_rung_reports_the_last_rung(self):
        self.assertEqual(agy.infer_window_minutes(30 * 86400, None)[0], 10080)

    def test_a_widened_window_never_narrows_again(self):
        # The whole point: a weekly bucket sampled with twenty minutes left must
        # not rename its slot to the 5-hour rung and orphan its stored anchor.
        self.assertEqual(agy.infer_window_minutes(20 * 60, 10080)[0], 10080)

    def test_a_missing_or_past_reset_time_falls_back_to_the_first_rung(self):
        self.assertEqual(agy.infer_window_minutes(None, None), (300, True))
        self.assertEqual(agy.infer_window_minutes(-5, None), (300, True))
        self.assertEqual(agy.infer_window_minutes(None, 1440), (1440, True))

    def test_every_window_is_flagged_inferred(self):
        # The payload never states a duration; nothing downstream may present
        # this number as something the vendor said.
        for seconds in (None, 60, 6 * 3600, 30 * 86400):
            with self.subTest(seconds=seconds):
                self.assertTrue(agy.infer_window_minutes(seconds, None)[1])


# ─── parse_quota ─────────────────────────────────────────────────────────────


class ParseQuotaTests(ProbeTestCase):
    def test_rows_carry_every_field_the_detector_reads(self):
        rows = agy.parse_quota(USAGE_PAYLOAD, NOW)
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(limit=row["limit_id"]):
                for key in ("t", "limit_id", "window_minutes", "used_percent", "resets_at"):
                    self.assertIn(key, row)
                self.assertEqual(row["t"], NOW)
                self.assertIsNone(row["credits_available"])
                self.assertTrue(row["window_inferred"])
                # slot_key must be buildable, which is what the detector indexes
                # every piece of state by.
                self.assertEqual(
                    quota_probe.slot_key(row), f"{row['limit_id']}/{row['window_minutes']}"
                )

    def test_remaining_fraction_becomes_used_percent(self):
        rows = agy.parse_quota(payload(bucket(remainingFraction=0.4)), NOW)
        self.assertEqual(rows[0]["used_percent"], 60.0)
        rows = agy.parse_quota(payload(bucket(remainingFraction=1.0)), NOW)
        self.assertEqual(rows[0]["used_percent"], 0.0)
        rows = agy.parse_quota(payload(bucket(remainingFraction=0.0)), NOW)
        self.assertEqual(rows[0]["used_percent"], 100.0)

    def test_out_of_range_fractions_are_clamped(self):
        # A negative remaining fraction (over-quota) would otherwise render as
        # "103% used" on the site.
        self.assertEqual(agy.parse_quota(payload(bucket(remainingFraction=-0.03)), NOW)[0][
            "used_percent"
        ], 100.0)
        self.assertEqual(agy.parse_quota(payload(bucket(remainingFraction=1.4)), NOW)[0][
            "used_percent"
        ], 0.0)

    def test_a_bucket_with_no_readable_fraction_is_skipped_loudly(self):
        for value in (None, "0.5", True, {}):
            with self.subTest(value=value):
                self.logs.clear()
                rows = agy.parse_quota(payload(bucket(remainingFraction=value)), NOW)
                self.assertEqual(rows, [])
                self.assertTrue(any("SKIPPED bucket" in line for line in self.logs))

    def test_the_token_type_is_part_of_the_slot_identity(self):
        rows = agy.parse_quota(
            payload(bucket(tokenType="REQUESTS"), bucket(tokenType="TOKENS")), NOW
        )
        self.assertEqual(
            [row["limit_id"] for row in rows],
            ["gemini-3-pro:REQUESTS", "gemini-3-pro:TOKENS"],
        )

    def test_missing_identifiers_do_not_crash(self):
        rows = agy.parse_quota({"buckets": [{"remainingFraction": 0.5}]}, NOW)
        self.assertEqual(rows[0]["limit_id"], "unknown-model:UNKNOWN")

    def test_the_window_ledger_is_updated_in_place_and_stays_monotone(self):
        ledger: dict[str, int] = {}
        agy.parse_quota(payload(bucket(resetTime=NOW + 6 * 86400)), NOW, windows=ledger)
        self.assertEqual(ledger, {"gemini-3-pro:REQUESTS": 10080})
        # Same bucket, sampled late in the same weekly window.
        rows = agy.parse_quota(payload(bucket(resetTime=NOW + 600)), NOW, windows=ledger)
        self.assertEqual(rows[0]["window_minutes"], 10080)

    def test_plan_type_is_carried_when_the_payload_states_one(self):
        self.assertEqual(
            agy.parse_quota({"planType": "AI Ultra", "buckets": [bucket()]}, NOW)[0]["plan_type"],
            "AI Ultra",
        )
        self.assertIsNone(agy.parse_quota(payload(bucket()), NOW)[0]["plan_type"])

    def test_rows_come_back_in_a_stable_order(self):
        rows = agy.parse_quota(
            payload(bucket(modelId="z-model"), bucket(modelId="a-model")), NOW
        )
        self.assertEqual([r["limit_id"] for r in rows], ["a-model:REQUESTS", "z-model:REQUESTS"])


# ─── The rows really are Detector rows ───────────────────────────────────────


class DetectorInteropTests(ProbeTestCase):
    """The reason for parsing into quota_probe's shape at all.

    These feed a REAL `quota_probe.Detector` — not a stand-in — so a change to
    the detector's row contract breaks here rather than silently producing a
    Google column that never classifies anything.
    """

    def feed(self, samples: list[tuple[int, float, int | None]]) -> list[dict]:
        detector = quota_probe.Detector()
        ledger: dict[str, int] = {}
        events: list[dict] = []
        for now, fraction, reset in samples:
            events.extend(
                agy.poll_once(
                    detector,
                    ledger,
                    now=now,
                    fetch=lambda f=fraction, r=reset: payload(  # type: ignore[misc]
                        bucket(remainingFraction=f, resetTime=r)
                    ),
                )
            )
        return events

    def test_a_clear_at_the_declared_expiry_is_a_scheduled_expiry(self):
        anchor = NOW + 300
        events = self.feed(
            [
                (NOW, 0.4, anchor),              # 60% used, expiry in 5 minutes
                (NOW + 360, 1.0, NOW + 18360),   # cleared, re-anchored
                (NOW + 420, 1.0, NOW + 18360),   # still idle: confirms the clear
            ]
        )
        self.assertEqual([e["kind"] for e in events], [quota_probe.EVENT_CLEAR])
        self.assertEqual(events[0]["classification"], quota_probe.CLASS_NATURAL)

    def test_a_clear_long_before_the_declared_expiry_is_a_global_candidate(self):
        anchor = NOW + 4 * 3600
        events = self.feed(
            [
                (NOW, 0.4, anchor),
                (NOW + 60, 1.0, anchor),
                (NOW + 120, 1.0, anchor),
            ]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["classification"], quota_probe.CLASS_GLOBAL)
        # With no credit bank on either side, "the bank was unchanged" is the
        # only reading available — and for Google it is the right one: there is
        # no self-service reset an account holder could have applied.
        self.assertIsNone(events[0]["credits_before"])
        self.assertIsNone(events[0]["credits_after"])

    def test_a_clear_with_no_anchor_on_record_is_unresolved(self):
        # The first samples after a restart carry no active anchor, and the
        # module must not let that become a vendor-reset candidate.
        events = self.feed(
            [
                (NOW, 0.4, None),
                (NOW + 60, 1.0, None),
                (NOW + 120, 1.0, None),
            ]
        )
        self.assertEqual(events[0]["classification"], quota_probe.CLASS_UNRESOLVED)

    def test_ordinary_usage_produces_no_events(self):
        events = self.feed(
            [(NOW + 60 * i, 1.0 - 0.05 * i, NOW + 4 * 3600) for i in range(6)]
        )
        self.assertEqual(events, [])

    def test_detectable_now_reports_a_window_too_empty_to_show_a_reset(self):
        detector = quota_probe.Detector()
        agy.poll_once(
            detector, {}, now=NOW,
            fetch=lambda: payload(bucket(remainingFraction=0.97)),  # 3% used
        )
        self.assertEqual(list(detector.detectable_now().values()), [False])
        agy.poll_once(
            detector, {}, now=NOW + 60,
            fetch=lambda: payload(bucket(remainingFraction=0.4)),  # 60% used
        )
        self.assertEqual(list(detector.detectable_now().values()), [True])


# ─── Cadence precondition ────────────────────────────────────────────────────


class CadenceTests(ProbeTestCase):
    def test_the_default_grace_refuses_the_default_cadence(self):
        # quota_probe's grace is 2*60+60 = 180 s, this probe polls every 300 s.
        # Running anyway would classify every scheduled expiry as an early clear.
        with self.assertRaises(quota_probe.ProbeError) as caught:
            agy.check_cadence()
        message = str(caught.exception)
        self.assertIn("AI_RESETS_POLL_SECONDS=300", message)
        self.assertIn("scheduled expiry", message)

    def test_a_matching_grace_is_accepted(self):
        with mock.patch.object(quota_probe, "NATURAL_GRACE_SECONDS", 660):
            agy.check_cadence(300)

    def test_a_fast_cadence_is_accepted_under_the_default_grace(self):
        agy.check_cadence(60)


# ─── The loop, health, and the cursor ────────────────────────────────────────


class RunTests(ProbeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.allow_cadence()

    def test_one_successful_poll_writes_health_and_a_cursor(self):
        code = agy.run(
            once=True, fetch=lambda: USAGE_PAYLOAD, cursor_path=self.cursor, clock=lambda: NOW
        )
        self.assertEqual(code, 0)
        block = self.health_blocks()[agy.PROBE_LABEL]
        self.assertEqual(
            sorted(block),
            sorted(
                [
                    "label",
                    "updated_at",
                    "last_ok_at",
                    "last_sample_t",
                    "consecutive_failures",
                    "blind_since",
                    "throttled_until",
                    "token_stale",
                    "detectable_now",
                    "last_error",
                ]
            ),
        )
        self.assertEqual(block["label"], agy.PROBE_LABEL)
        self.assertEqual(block["last_ok_at"], NOW)
        self.assertEqual(block["last_sample_t"], NOW)
        self.assertEqual(block["consecutive_failures"], 0)
        self.assertIsNone(block["blind_since"])
        self.assertIsNone(block["last_error"])
        cursor = json.loads(self.cursor.read_text(encoding="utf-8"))
        self.assertIn("slots", cursor)
        self.assertEqual(cursor["windows"], {"gemini-3-flash:REQUESTS": 300, "gemini-3-pro:REQUESTS": 300})

    def test_health_merges_and_never_clobbers_the_codex_block(self):
        # The shared contract: probe_health.json holds one block per label and
        # the codex block is the only one on this host that currently works.
        # Overwriting it would blind the notifier's heartbeat check.
        self.health.write_text(json.dumps({"codex": {"label": "codex", "updated_at": 1}}))
        agy.run(once=True, fetch=lambda: USAGE_PAYLOAD, cursor_path=self.cursor, clock=lambda: NOW)
        blocks = self.health_blocks()
        self.assertEqual(blocks["codex"], {"label": "codex", "updated_at": 1})
        self.assertIn(agy.PROBE_LABEL, blocks)

    def test_a_failing_poll_returns_one_and_records_the_outage(self):
        def refuse():
            raise agy.unavailable("agy is not on PATH")

        code = agy.run(once=True, fetch=refuse, cursor_path=self.cursor, clock=lambda: NOW)
        self.assertEqual(code, 1)
        block = self.health_blocks()[agy.PROBE_LABEL]
        self.assertEqual(block["consecutive_failures"], 1)
        self.assertEqual(block["blind_since"], NOW)
        self.assertIn(agy.UNAVAILABLE, block["last_error"])

    def test_ten_consecutive_failures_exit_two(self):
        # Same contract as the Codex probe: the loop must die so systemd's
        # OnFailure can alert, rather than spin silently for days.
        ticks = iter(range(NOW, NOW + 10_000, agy.POLL_SECONDS))

        def refuse():
            raise agy.unavailable("still not signed in")

        code = agy.run(
            fetch=refuse,
            cursor_path=self.cursor,
            sleep=lambda _s: None,
            clock=lambda: next(ticks),
        )
        self.assertEqual(code, 2)
        block = self.health_blocks()[agy.PROBE_LABEL]
        self.assertEqual(block["consecutive_failures"], agy.MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(block["blind_since"], NOW, "the outage started at the first failure")
        self.assertTrue(any("PROBE EXITING" in line for line in self.logs))

    def test_a_restart_carries_the_outage_forward_then_clears_it(self):
        """systemd restarts the probe seconds after it exits.

        `resume_health` is what stops the owner alert from reporting that the
        outage began at the restart. It only trusts a block younger than
        HEALTH_CARRY_SECONDS, so the wall clock is pinned here to keep the
        stored block fresh; the probe's own timeline stays on the injected clock.
        """

        def refuse():
            raise agy.unavailable("still not signed in")

        with mock.patch("time.time", lambda: NOW + 60):
            self.assertEqual(
                agy.run(once=True, fetch=refuse, cursor_path=self.cursor, clock=lambda: NOW), 1
            )
            self.assertEqual(self.health_blocks()[agy.PROBE_LABEL]["blind_since"], NOW)
            self.assertEqual(
                agy.run(
                    once=True,
                    fetch=lambda: USAGE_PAYLOAD,
                    cursor_path=self.cursor,
                    clock=lambda: NOW + 60,
                ),
                0,
            )
        recovered = self.health_blocks()[agy.PROBE_LABEL]
        self.assertIsNone(recovered["blind_since"], "the first good poll ends the outage")
        self.assertEqual(recovered["last_ok_at"], NOW + 60)

    def test_the_cursor_carries_the_window_ledger_across_restarts(self):
        weekly = {"buckets": [bucket(resetTime=NOW + 6 * 86400)]}
        agy.run(once=True, fetch=lambda: weekly, cursor_path=self.cursor, clock=lambda: NOW)
        self.assertEqual(
            json.loads(self.cursor.read_text())["windows"], {"gemini-3-pro:REQUESTS": 10080}
        )
        # A second process, sampling the same bucket with ten minutes left, must
        # keep the weekly slot rather than start a fresh 5-hour one.
        late = {"buckets": [bucket(resetTime=NOW + 600)]}
        agy.run(once=True, fetch=lambda: late, cursor_path=self.cursor, clock=lambda: NOW + 60)
        cursor = json.loads(self.cursor.read_text())
        self.assertEqual(cursor["windows"], {"gemini-3-pro:REQUESTS": 10080})
        self.assertEqual(list(cursor["slots"]), ["gemini-3-pro:REQUESTS/10080"])

    def test_a_corrupt_cursor_is_ignored_rather_than_fatal(self):
        self.cursor.write_text("{ truncated")
        code = agy.run(
            once=True, fetch=lambda: USAGE_PAYLOAD, cursor_path=self.cursor, clock=lambda: NOW
        )
        self.assertEqual(code, 0)

    def test_a_clear_detected_across_three_restarts_is_logged(self):
        # Three separate `--once` runs sharing one cursor file: the detector's
        # pending-clear state has to survive the JSON round trip, or a probe
        # that restarts between two samples can never confirm anything.
        anchor = NOW + 4 * 3600
        for offset, fraction in ((0, 0.4), (60, 1.0), (120, 1.0)):
            agy.run(
                once=True,
                fetch=lambda f=fraction: payload(
                    bucket(remainingFraction=f, resetTime=anchor)
                ),
                cursor_path=self.cursor,
                clock=lambda o=offset: NOW + o,
            )
        self.assertTrue(
            any("google clear" in line and "global_candidate" in line for line in self.logs),
            self.logs,
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────


class MainTests(ProbeTestCase):
    def test_fixture_mode_runs_without_agy(self):
        self.allow_cadence()
        recorded = self.tmp / "usage.json"
        recorded.write_text(json.dumps(USAGE_PAYLOAD), encoding="utf-8")
        code = agy.main(["--once", "--fixture", str(recorded), "--cursor", str(self.cursor)])
        self.assertEqual(code, 0)
        self.assertIn(agy.PROBE_LABEL, self.health_blocks())

    def test_the_cadence_refusal_is_reported_as_one_log_line_and_exit_one(self):
        code = agy.main(["--once", "--cursor", str(self.cursor)])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("AI_RESETS_POLL_SECONDS", self.logs[0])

    def test_a_live_run_without_agy_exits_one_with_the_actionable_line(self):
        # The state of this host today. Nothing here installs anything.
        self.allow_cadence()
        with mock.patch.object(agy.shutil, "which", lambda _name: None):
            code = agy.main(["--once", "--cursor", str(self.cursor)])
        self.assertEqual(code, 1)
        self.assertTrue(any(agy.UNAVAILABLE in line for line in self.logs), self.logs)


if __name__ == "__main__":
    unittest.main()
