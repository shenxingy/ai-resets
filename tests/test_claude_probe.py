"""Tests for the Claude Code ground-truth probe.

Everything here runs against a RECORDED payload. The live endpoint throttles
per token at roughly 25-30 requests per 15 minutes with a persistent 429, and
a probe polling it every 20s once locked both of the owner's accounts out of
their own /usage page — so this suite never makes a network call, and the
fixture below is the shape a real read returned on 2026-09-06.

The one edit to that recording: `scope.model.display_name` carried an
unreleased model codename, replaced here with a public model name. The probe
does not publish that field either, for the same reason.
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import claude_probe as cp
from scripts import groundtruth as gt
from scripts import quota_probe as qp

WEEK_MINUTES = 10080
SESSION_MINUTES = 300
DAY = 86400

# The probe reads each profile's .claude.json and history.jsonl from the home
# directory by default. On the machine this runs on, those are the owner's
# real files, so every test in this module starts with all four pointed into
# an empty directory: a test that wants an identity or a history writes one.
_PROFILE_ENV = None
_PROFILE_DIR = None


def setUpModule():
    global _PROFILE_ENV, _PROFILE_DIR
    _PROFILE_DIR = tempfile.TemporaryDirectory()
    root = Path(_PROFILE_DIR.name)
    _PROFILE_ENV = mock.patch.dict(os.environ, {
        f"AI_RESETS_CLAUDE_{kind}_{label}": str(root / f"absent-{kind.lower()}-{label}")
        for kind in ("CONFIG", "HISTORY") for label in ("A", "B")
    })
    _PROFILE_ENV.start()


def tearDownModule():
    _PROFILE_ENV.stop()
    _PROFILE_DIR.cleanup()

# A 108-character access token, the length the credential file actually holds.
FAKE_TOKEN = "sk-ant-oat01-" + "A9z" * 31 + "Bq"


def iso(epoch: int, micro: int = 179608) -> str:
    """The recorded `resets_at` spelling: ISO-8601, microseconds, +00:00."""
    stamp = datetime.fromtimestamp(epoch, timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{micro:06d}+00:00"


def usage_payload(
    *,
    session=22,
    weekly=48,
    scoped=55,
    session_resets=1788770400,
    weekly_resets=1788789600,
    scoped_model="Opus",
):
    """The /api/oauth/usage payload, in the shape recorded on 2026-09-06."""
    return {
        "five_hour": {
            "utilization": float(session),
            "resets_at": iso(session_resets),
            "limit_dollars": None,
            "locked_reason": None,
        },
        "seven_day": {
            "utilization": float(weekly),
            "resets_at": iso(weekly_resets),
            "limit_dollars": None,
            "locked_reason": None,
        },
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": False, "utilization": None},
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": session,
                "severity": "normal",
                "resets_at": iso(session_resets, 179590),
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": weekly,
                "severity": "normal",
                "resets_at": iso(weekly_resets),
                "scope": None,
                "is_active": False,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": scoped,
                "severity": "normal",
                "resets_at": iso(weekly_resets, 179817),
                "scope": {
                    "model": {"id": None, "display_name": scoped_model},
                    "surface": None,
                },
                "is_active": True,
            },
        ],
        "spend": {"percent": 0, "enabled": False},
        "member_dashboard_available": False,
    }


def credential_file(path: Path, *, expires_at_ms: int, token: str = FAKE_TOKEN) -> Path:
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "r" * 108,
                    "expiresAt": expires_at_ms,
                    "refreshTokenExpiresAt": expires_at_ms + 10**9,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


class StateDirTest(unittest.TestCase):
    """Every file the probe writes, redirected into a temp directory.

    `qp.HEALTH_FILE` is patched as well as the probe's own: the health file is
    shared with the Codex probe and its merge is deliberately implemented once,
    in quota_probe, so a test that patched only one of the two names would
    write into /var/lib/ai-resets.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patches = {
            "SAMPLES_FILE": self.state / "claude_samples.jsonl",
            "EVENTS_FILE": self.state / "claude_events.jsonl",
            "CURSOR_FILE": self.state / "claude_cursor.json",
            "HEALTH_FILE": self.state / "probe_health.json",
        }
        for name, value in patches.items():
            patcher = mock.patch.object(cp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        health = mock.patch.object(qp, "HEALTH_FILE", patches["HEALTH_FILE"])
        health.start()
        self.addCleanup(health.stop)

    def state_files(self):
        return [p for p in self.state.rglob("*") if p.is_file()]


# ─── Secrets hygiene ─────────────────────────────────────────────────────────


class RedactionTests(unittest.TestCase):
    def test_a_token_never_survives_redaction(self):
        message = f"usage endpoint rejected Bearer {FAKE_TOKEN}"
        self.assertNotIn(FAKE_TOKEN, cp.redact(message))
        self.assertIn("<redacted>", cp.redact(message))

    def test_short_words_are_left_alone(self):
        self.assertEqual(cp.redact("HTTP 429 throttled"), "HTTP 429 throttled")

    def test_log_redacts_before_printing(self):
        with mock.patch.object(qp, "log") as logged:
            cp.log(f"token {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, logged.call_args[0][0])


# ─── Credentials ─────────────────────────────────────────────────────────────


class CredentialTests(StateDirTest):
    def test_reads_the_token_and_converts_millisecond_expiry(self):
        path = credential_file(self.state / "creds.json", expires_at_ms=1788766910046)
        token, expires_at, subscription = cp.read_credentials(path)
        self.assertEqual(token, FAKE_TOKEN)
        # Milliseconds in the file; seconds everywhere in this process.
        self.assertEqual(expires_at, 1788766910)
        self.assertEqual(subscription, "max")

    def test_a_second_valued_expiry_is_left_alone(self):
        path = credential_file(self.state / "creds.json", expires_at_ms=1788766910)
        _, expires_at, _ = cp.read_credentials(path)
        self.assertEqual(expires_at, 1788766910)

    def test_missing_file_is_a_credential_error(self):
        with self.assertRaises(cp.CredentialError):
            cp.read_credentials(self.state / "absent.json")

    def test_unparseable_file_is_a_credential_error(self):
        path = self.state / "creds.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(cp.CredentialError):
            cp.read_credentials(path)

    def test_missing_oauth_block_is_a_credential_error(self):
        path = self.state / "creds.json"
        path.write_text(json.dumps({"other": {}}), encoding="utf-8")
        with self.assertRaises(cp.CredentialError):
            cp.read_credentials(path)

    def test_missing_token_is_a_credential_error(self):
        path = self.state / "creds.json"
        path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 1}}), encoding="utf-8")
        with self.assertRaises(cp.CredentialError):
            cp.read_credentials(path)


# ─── Transport ───────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError(
        cp.USAGE_URL, code, "error", headers or {}, io.BytesIO(body)
    )


class TransportTests(unittest.TestCase):
    def test_sends_the_measured_headers_and_returns_the_payload(self):
        payload = usage_payload()
        with mock.patch("urllib.request.urlopen") as opener:
            opener.return_value = FakeResponse(json.dumps(payload).encode())
            result = cp.fetch_usage(FAKE_TOKEN)
        request = opener.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")
        self.assertEqual(request.get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(result["limits"][1]["kind"], "weekly_all")

    def test_401_is_an_auth_error_and_never_a_refresh(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401)):
            with self.assertRaises(cp.AuthError):
                cp.fetch_usage(FAKE_TOKEN)

    def test_403_is_an_auth_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(403)):
            with self.assertRaises(cp.AuthError):
                cp.fetch_usage(FAKE_TOKEN)

    def test_429_with_retry_after_carries_the_servers_number(self):
        error = http_error(429, headers={"Retry-After": "42"})
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(cp.ThrottledError) as caught:
                cp.fetch_usage(FAKE_TOKEN)
        self.assertEqual(caught.exception.retry_after, 42)

    def test_429_without_retry_after_leaves_the_wait_to_us(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(429)):
            with self.assertRaises(cp.ThrottledError) as caught:
                cp.fetch_usage(FAKE_TOKEN)
        self.assertIsNone(caught.exception.retry_after)

    def test_retry_after_may_be_an_http_date(self):
        headers = {"Retry-After": "Sun, 06 Sep 2026 12:00:30 GMT"}
        seconds = cp.retry_after_seconds(headers, int(datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(seconds, 30)

    def test_retry_after_is_clamped_to_the_backoff_cap(self):
        self.assertEqual(
            cp.retry_after_seconds({"Retry-After": "86400"}, 0), cp.THROTTLE_MAX_SECONDS
        )

    def test_other_http_codes_are_ordinary_probe_failures(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(500)):
            with self.assertRaises(qp.ProbeError) as caught:
                cp.fetch_usage(FAKE_TOKEN)
        self.assertNotIsInstance(caught.exception, cp.ThrottledError)

    def test_an_error_body_that_echoes_the_token_is_redacted(self):
        body = json.dumps(
            {"error": {"type": "authentication_error", "message": f"bad {FAKE_TOKEN}"}}
        ).encode()
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401, body)):
            with self.assertRaises(cp.AuthError) as caught:
                cp.fetch_usage(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_a_body_that_is_not_json_is_a_probe_failure(self):
        with mock.patch("urllib.request.urlopen") as opener:
            opener.return_value = FakeResponse(b"<html>gateway</html>")
            with self.assertRaises(qp.ProbeError):
                cp.fetch_usage(FAKE_TOKEN)

    def test_a_payload_that_is_not_an_object_is_a_probe_failure(self):
        with mock.patch("urllib.request.urlopen") as opener:
            opener.return_value = FakeResponse(b"[1,2,3]")
            with self.assertRaises(qp.ProbeError):
                cp.fetch_usage(FAKE_TOKEN)

    def test_a_network_failure_is_a_probe_failure(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("no route")
        ):
            with self.assertRaises(qp.ProbeError):
                cp.fetch_usage(FAKE_TOKEN)


# ─── Normalisation ───────────────────────────────────────────────────────────


class NormaliseTests(unittest.TestCase):
    def test_maps_every_kind_to_a_slot_id_and_window(self):
        rows = cp.normalise_usage(usage_payload(), "a", 1788750000, plan_type="max")
        by_id = {row["limit_id"]: row for row in rows}
        self.assertEqual(
            sorted(by_id),
            ["claude:a:session", "claude:a:weekly_all", "claude:a:weekly_scoped:opus"],
        )
        self.assertEqual(by_id["claude:a:session"]["window_minutes"], SESSION_MINUTES)
        self.assertEqual(by_id["claude:a:weekly_all"]["window_minutes"], WEEK_MINUTES)
        self.assertEqual(
            by_id["claude:a:weekly_scoped:opus"]["window_minutes"], WEEK_MINUTES
        )
        self.assertEqual(by_id["claude:a:weekly_all"]["used_percent"], 48.0)
        self.assertEqual(by_id["claude:a:weekly_all"]["resets_at"], 1788789600)
        self.assertEqual(by_id["claude:a:weekly_all"]["plan_type"], "max")

    def test_slot_keys_carry_the_account_label(self):
        rows = cp.normalise_usage(usage_payload(), "b", 1788750000)
        keys = {qp.slot_key(row) for row in rows}
        self.assertIn("claude:b:weekly_all/10080", keys)
        for key in keys:
            self.assertEqual(cp.account_of_slot(key), "b")

    def test_there_is_never_a_credit_bank(self):
        rows = cp.normalise_usage(usage_payload(), "a", 1788750000)
        self.assertTrue(all(row["credits_available"] is None for row in rows))

    def test_the_model_display_name_is_kept_for_the_owner_only(self):
        rows = cp.normalise_usage(usage_payload(scoped_model="Claude Opus 4.6"), "a", 1)
        scoped = [row for row in rows if row["limit_kind"] == "weekly_scoped"][0]
        self.assertEqual(scoped["model"], "claude-opus-4-6")
        self.assertEqual(scoped["model_name"], "Claude Opus 4.6")

    def test_an_unreadable_percent_drops_only_that_window(self):
        payload = usage_payload()
        payload["limits"][0]["percent"] = None
        rows = cp.normalise_usage(payload, "a", 1)
        self.assertEqual(len(rows), 2)

    def test_a_new_weekly_kind_still_maps_by_its_group(self):
        payload = usage_payload()
        payload["limits"][1]["kind"] = "weekly_something_new"
        rows = cp.normalise_usage(payload, "a", 1)
        new = [row for row in rows if row["limit_kind"] == "weekly_something_new"][0]
        self.assertEqual(new["window_minutes"], WEEK_MINUTES)

    def test_an_unrecognisable_window_is_dropped_not_guessed(self):
        payload = usage_payload()
        payload["limits"][0]["group"] = "monthly"
        payload["limits"][0]["kind"] = "monthly_all"
        rows = cp.normalise_usage(payload, "a", 1)
        self.assertEqual(len(rows), 2)

    def test_a_payload_without_limits_is_a_failed_poll_not_an_empty_one(self):
        payload = usage_payload()
        del payload["limits"]
        with self.assertRaises(qp.ProbeError):
            cp.normalise_usage(payload, "a", 1)

    def test_limits_of_the_wrong_type_is_a_failed_poll(self):
        payload = usage_payload()
        payload["limits"] = {"kind": "weekly_all"}
        with self.assertRaises(qp.ProbeError):
            cp.normalise_usage(payload, "a", 1)

    def test_limits_full_of_junk_is_a_failed_poll(self):
        payload = usage_payload()
        payload["limits"] = [None, 7, {"kind": "session"}]
        with self.assertRaises(qp.ProbeError):
            # The only entry with a kind carries no percent, so nothing is
            # readable: reporting that as "we looked and saw nothing" is the
            # error this raise exists to prevent.
            cp.normalise_usage(payload, "a", 1)

    def test_a_missing_reset_time_is_none_not_an_invention(self):
        payload = usage_payload()
        payload["limits"][1]["resets_at"] = None
        rows = cp.normalise_usage(payload, "a", 1)
        weekly = [row for row in rows if row["limit_kind"] == "weekly_all"][0]
        self.assertIsNone(weekly["resets_at"])

    def test_a_zulu_timestamp_parses_too(self):
        self.assertEqual(cp.parse_moment("2026-09-07T10:00:00Z"), 1788775200)

    def test_an_unparseable_timestamp_is_none(self):
        self.assertIsNone(cp.parse_moment("next tuesday"))


# ─── The poll interval is a safety floor, not a preference ───────────────────


class PollIntervalTests(unittest.TestCase):
    def test_the_floor_cannot_be_lowered_by_configuration(self):
        # The measured harm: a probe polling this endpoint every 20s locked
        # both of the owner's accounts out of their own /usage page.
        self.assertEqual(cp.poll_interval("20"), cp.MIN_POLL_SECONDS)
        self.assertEqual(cp.poll_interval("0"), cp.MIN_POLL_SECONDS)
        self.assertEqual(cp.poll_interval("-1"), cp.MIN_POLL_SECONDS)

    def test_a_longer_interval_is_honoured(self):
        self.assertEqual(cp.poll_interval("900"), 900)

    def test_a_typo_falls_back_to_the_floor_instead_of_crashing_at_import(self):
        self.assertEqual(cp.poll_interval("five minutes"), cp.MIN_POLL_SECONDS)
        self.assertEqual(cp.poll_interval(None), cp.MIN_POLL_SECONDS)

    def test_the_natural_expiry_grace_matches_the_interval(self):
        self.assertEqual(cp.NATURAL_GRACE_SECONDS, 2 * cp.POLL_SECONDS + 60)
        self.assertEqual(cp.NATURAL_GRACE_SECONDS, 660)


# ─── Throttling ──────────────────────────────────────────────────────────────


class ThrottleTests(StateDirTest):
    def probe(self):
        path = credential_file(
            self.state / "creds-a.json", expires_at_ms=(int(2e9)) * 1000
        )
        return cp.AccountProbe("a", path)

    def test_the_backoff_doubles_from_ten_minutes_to_a_one_hour_cap(self):
        probe = self.probe()
        waits = []
        for _ in range(6):
            probe.throttled_until = None
            probe._throttle(0, cp.ThrottledError("429"))
            waits.append(probe.throttled_until)
        self.assertEqual(waits, [600, 1200, 2400, 3600, 3600, 3600])

    def test_retry_after_is_honoured_over_our_own_ladder(self):
        probe = self.probe()
        probe._throttle(1000, cp.ThrottledError("429", retry_after=90))
        self.assertEqual(probe.throttled_until, 1090)
        # The ladder still advances, so a server that keeps sending small
        # Retry-Afters cannot hold us at ten minutes forever.
        self.assertEqual(probe.backoff, 1200)

    def test_a_throttled_probe_sends_no_request_at_all(self):
        probe = self.probe()
        probe.throttled_until = 5000
        with mock.patch.object(cp, "fetch_usage") as fetch:
            self.assertIsNone(probe.poll(4000))
        fetch.assert_not_called()
        self.assertFalse(probe.attempted)

    def test_a_throttled_probe_is_never_reported_as_a_clean_look(self):
        probe = self.probe()
        probe._throttle(1000, cp.ThrottledError("429", retry_after=600))
        detector = qp.Detector()
        block = probe.health(detector, 1001)
        self.assertEqual(block["throttled_until"], 1600)
        # quota_probe's generic reader knows nothing about throttling, so the
        # failure counter has to make it read blind there as well.
        self.assertEqual(qp.probe_status(block, 1001), qp.PROBE_BLIND)
        self.assertEqual(gt.probe_status({"claude:a": block}, "claude:a", 1001)["status"], gt.STATUS_THROTTLED)

    def test_the_backoff_survives_a_restart(self):
        # systemd restarts this unit on any non-zero exit. A backoff that lived
        # only in memory would let a crash loop walk straight through a 429 the
        # endpoint is still enforcing.
        qp.write_health(
            {
                "label": "claude:a",
                "updated_at": 1000,
                "last_ok_at": 900,
                "last_sample_t": 900,
                "consecutive_failures": 1,
                "blind_since": None,
                "throttled_until": 2200,
                "token_stale": False,
                "detectable_now": {},
                "last_error": "429",
            },
            "claude:a",
        )
        probe = self.probe()
        probe.resume(1000)
        self.assertEqual(probe.throttled_until, 2200)
        self.assertEqual(probe.backoff, 1200)
        with mock.patch.object(cp, "fetch_usage") as fetch:
            self.assertIsNone(probe.poll(1100))
        fetch.assert_not_called()

    def test_an_implausible_stored_throttle_is_ignored(self):
        qp.write_health(
            {"label": "claude:a", "updated_at": 1000, "throttled_until": 1000 + 10 * 3600},
            "claude:a",
        )
        probe = self.probe()
        probe.resume(1000)
        self.assertIsNone(probe.throttled_until)

    def test_a_stale_credential_costs_no_request(self):
        path = credential_file(
            self.state / "creds-a.json", expires_at_ms=1788700000 * 1000
        )
        probe = cp.AccountProbe("a", path)
        with mock.patch.object(cp, "fetch_usage") as fetch:
            self.assertIsNone(probe.poll(1788800000))
        fetch.assert_not_called()
        self.assertTrue(probe.token_stale)
        block = probe.health(qp.Detector(), 1788800000)
        self.assertTrue(block["token_stale"])
        self.assertEqual(
            gt.probe_status({"claude:a": block}, "claude:a", 1788800000)["status"],
            gt.STATUS_TOKEN_STALE,
        )

    def test_a_401_never_triggers_a_refresh_and_marks_the_token_stale(self):
        probe = self.probe()
        with mock.patch.object(cp, "fetch_usage", side_effect=cp.AuthError("401")):
            self.assertIsNone(probe.poll(2000))
        self.assertTrue(probe.token_stale)
        self.assertEqual(probe.consecutive_failures, 1)

    def test_a_network_failure_starts_a_blind_period(self):
        probe = self.probe()
        with mock.patch.object(cp, "fetch_usage", side_effect=qp.ProbeError("no route")):
            self.assertIsNone(probe.poll(2000))
            self.assertIsNone(probe.poll(2300))
        self.assertEqual(probe.blind_since, 2000)
        self.assertEqual(probe.consecutive_failures, 2)

    def test_a_success_clears_everything(self):
        probe = self.probe()
        probe.blind_since = 1
        probe.consecutive_failures = 4
        probe.token_stale = True
        probe.backoff = 2400
        with mock.patch.object(cp, "fetch_usage", return_value=usage_payload()):
            rows = probe.poll(3000)
        self.assertEqual(len(rows), 3)
        self.assertEqual(probe.consecutive_failures, 0)
        self.assertIsNone(probe.blind_since)
        self.assertFalse(probe.token_stale)
        self.assertEqual(probe.backoff, cp.THROTTLE_BASE_SECONDS)
        self.assertEqual(probe.last_ok_at, 3000)


# ─── Detection against the fixed-anchor contract ─────────────────────────────


T0 = 1788700000


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.detector = qp.Detector()
        self.state = {}

    def feed(self, payload, label, t):
        rows = cp.normalise_usage(payload, label, t)
        return cp.observe_rows(self.detector, rows, self.state)

    def weekly_events(self, events):
        return [e for e in events if e.get("limit_kind") == "weekly_all"]

    def test_a_vendor_reset_keeps_the_anchor_and_zeroes_the_counter(self):
        # The inferred Anthropic shape (n=1): the Sep 7 anchor survived both
        # missed resets, so a forced clear does NOT re-anchor.
        anchor = T0 + 3 * DAY
        self.feed(usage_payload(weekly=48, weekly_resets=anchor), "a", T0)
        self.assertEqual(self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 300), [])
        events = self.weekly_events(
            self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 600)
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["classification"], qp.CLASS_GLOBAL)
        self.assertTrue(event["confirmed"])
        self.assertFalse(event["reanchored"])
        self.assertEqual(event["early_by_seconds"], anchor - (T0 + 300))
        self.assertEqual(event["account"], "a")
        self.assertEqual(event["vendor"], "anthropic")

    def test_a_clear_at_its_scheduled_time_is_a_natural_expiry(self):
        anchor = T0 + 120
        self.feed(usage_payload(weekly=48, weekly_resets=anchor), "a", T0)
        self.feed(usage_payload(weekly=0, weekly_resets=anchor + 7 * DAY), "a", T0 + 300)
        events = self.weekly_events(
            self.feed(usage_payload(weekly=0, weekly_resets=anchor + 7 * DAY), "a", T0 + 600)
        )
        self.assertEqual(events[0]["classification"], qp.CLASS_NATURAL)

    def test_the_grace_covers_two_polls_at_this_cadence(self):
        # 400s early is inside the 660s grace at 300s polls and outside the
        # Codex module's own 180s grace. Reading a weekly expiry seen one poll
        # late as a vendor reset is the over-claim this constant prevents.
        anchor = T0 + 700
        self.feed(usage_payload(weekly=48, weekly_resets=anchor), "a", T0)
        self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 300)
        events = self.weekly_events(
            self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 600)
        )
        self.assertEqual(events[0]["early_by_seconds"], 400)
        self.assertEqual(events[0]["classification"], qp.CLASS_NATURAL)

        # The same clear under the Codex constants, to show the difference is
        # the contract and not the data.
        codex = qp.classify(
            {"t": T0, "used_percent": 48.0, "credits_available": None},
            {"t": T0 + 300, "used_percent": 0.0, "credits_available": None},
            anchor,
        )
        self.assertEqual(codex["classification"], qp.CLASS_GLOBAL)

    def test_importing_this_module_leaves_the_codex_contract_alone(self):
        # `unittest discover` runs both probes' suites in ONE process. Setting
        # the Anthropic cadence at import time instead of around the call
        # would silently rewrite the Codex detector's own thresholds for every
        # test that runs after this module is imported.
        expected = int(os.environ.get("AI_RESETS_POLL_SECONDS", "60"))
        self.assertEqual(qp.POLL_SECONDS, expected)
        self.assertEqual(qp.NATURAL_GRACE_SECONDS, 2 * expected + 60)

    def test_the_codex_constants_are_put_back_even_on_an_exception(self):
        before = (qp.POLL_SECONDS, qp.NATURAL_GRACE_SECONDS, qp.CREDIT_BANK, qp.LOCAL_TRACE)
        with self.assertRaises(RuntimeError):
            with cp.anthropic_contract():
                self.assertEqual(qp.NATURAL_GRACE_SECONDS, 660)
                # No ledger to read, and a local trace that must be read.
                self.assertEqual((qp.CREDIT_BANK, qp.LOCAL_TRACE), (False, True))
                raise RuntimeError("boom")
        self.assertEqual(
            (qp.POLL_SECONDS, qp.NATURAL_GRACE_SECONDS, qp.CREDIT_BANK, qp.LOCAL_TRACE), before
        )
        self.assertEqual((qp.CREDIT_BANK, qp.LOCAL_TRACE), (True, False))

    def test_a_single_idle_read_on_an_unchanged_anchor_is_dropped(self):
        # On a fixed-anchor contract one 0% sample followed by usage proves
        # nothing, and an unretractable clear from it is exactly the shape
        # that cost the Codex probe a real event on 2026-09-02.
        anchor = T0 + 3 * DAY
        self.feed(usage_payload(weekly=48, weekly_resets=anchor), "a", T0)
        self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 300)
        events = self.feed(usage_payload(weekly=20, weekly_resets=anchor), "a", T0 + 600)
        self.assertEqual(events, [])

    def test_usage_snapping_back_retracts_a_confirmed_clear(self):
        anchor = T0 + 3 * DAY
        self.feed(usage_payload(weekly=48, weekly_resets=anchor), "a", T0)
        self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 300)
        self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 600)
        events = self.weekly_events(
            self.feed(usage_payload(weekly=47, weekly_resets=anchor), "a", T0 + 900)
        )
        self.assertEqual(events[0]["kind"], qp.EVENT_RETRACTION)

    def test_a_five_hour_clear_can_never_be_vendor_evidence(self):
        anchor = T0 + 4 * 3600
        self.feed(usage_payload(session=22, session_resets=anchor), "a", T0)
        self.feed(usage_payload(session=0, session_resets=anchor), "a", T0 + 300)
        events = [
            e
            for e in self.feed(usage_payload(session=0, session_resets=anchor), "a", T0 + 600)
            if e.get("limit_kind") == "session"
        ]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["classification"], cp.CLASS_SESSION_ONLY)
        # The detector's own reading is kept as the audit trail, not thrown away.
        self.assertEqual(event["gated_classification"], qp.CLASS_GLOBAL)
        self.assertIn("/limit-reset", event["reason"])
        self.assertFalse(cp.is_public(event, set()))
        self.assertIsNone(cp.verdict_of(event))
        self.assertEqual(event["checks"]["local_trace"], "n_a")

    def test_session_only_appears_in_no_verdict_map_in_the_repository(self):
        # Four independent readers turn a classification into something a
        # person sees. A 5-hour clear must be invisible to every one of them.
        self.assertNotIn(cp.CLASS_SESSION_ONLY, qp.VERDICT_OF_CLASS)
        self.assertNotIn(cp.CLASS_SESSION_ONLY, gt._INTERNAL_TO_VERDICT)
        self.assertNotIn(cp.CLASS_SESSION_ONLY, cp.PUBLIC_CLASSIFICATIONS)
        self.assertNotIn(cp.CLASS_SESSION_ONLY, qp.PUBLIC_CLASSIFICATIONS)

    def test_two_accounts_clearing_together_are_recorded_as_agreeing(self):
        anchor = T0 + 3 * DAY
        for label in ("a", "b"):
            self.feed(usage_payload(weekly=48, weekly_resets=anchor), label, T0)
        for label in ("a", "b"):
            self.feed(usage_payload(weekly=0, weekly_resets=anchor), label, T0 + 300)
        first = self.weekly_events(
            self.feed(usage_payload(weekly=0, weekly_resets=anchor), "a", T0 + 600)
        )
        second = self.weekly_events(
            self.feed(usage_payload(weekly=0, weekly_resets=anchor), "b", T0 + 600)
        )
        self.assertEqual(first[0]["concordance"], cp.CONCORDANCE_UNKNOWN)
        self.assertEqual(second[0]["concordance"], cp.CONCORDANCE_AGREEING)
        self.assertFalse(cp.is_public(second[0], set()))
        self.assertEqual(cp.verdict_of(second[0]), qp.VERDICT_UNRESOLVED)

    def test_one_account_clearing_while_the_other_stays_busy_is_a_disagreement(self):
        anchor = T0 + 3 * DAY
        busy = usage_payload(weekly=60, weekly_resets=anchor)
        for offset, a_weekly in ((0, 48), (300, 0), (600, 0)):
            self.feed(busy, "b", T0 + offset)
            events = self.weekly_events(
                self.feed(
                    usage_payload(weekly=a_weekly, weekly_resets=anchor), "a", T0 + offset
                )
            )
        self.assertEqual(events[0]["concordance"], cp.CONCORDANCE_DISAGREE)
        # Not evidence: it stays out of the public export.
        self.assertFalse(cp.is_public(events[0], set()))

    def test_an_idle_other_account_is_unknown_rather_than_a_disagreement(self):
        anchor = T0 + 3 * DAY
        idle = usage_payload(weekly=2, scoped=1, weekly_resets=anchor)
        for offset, a_weekly in ((0, 48), (300, 0), (600, 0)):
            self.feed(idle, "b", T0 + offset)
            events = self.weekly_events(
                self.feed(
                    usage_payload(weekly=a_weekly, weekly_resets=anchor), "a", T0 + offset
                )
            )
        self.assertEqual(events[0]["concordance"], cp.CONCORDANCE_UNKNOWN)


# ─── Attribution: identity, the local trace, account changes ─────────────────

FP = "aaaaaaaaaaaa"
OTHER_FP = "bbbbbbbbbbbb"


def profile_file(path: Path, *, org="org-raw-0001", account="acct-raw-0001") -> Path:
    """A profile .claude.json in the recorded shape, with placeholder ids."""
    path.write_text(json.dumps({
        "numStartups": 12,
        "oauthAccount": {
            "accountUuid": account,
            "organizationUuid": org,
            "emailAddress": "owner@example.invalid",
            "organizationName": "Example Org Name",
            "organizationType": "claude_max",
        },
    }), encoding="utf-8")
    return path


def history_file(path: Path, entries) -> Path:
    """history.jsonl lines: (display, epoch seconds) -> milliseconds, as recorded."""
    path.write_text("".join(
        json.dumps({"display": display, "pastedContents": {}, "timestamp": moment * 1000,
                    "project": "/work", "sessionId": "s"}) + "\n"
        for display, moment in entries
    ), encoding="utf-8")
    return path


class IdentityTests(StateDirTest):
    """docs/reset-attribution.md: Claude `account_fp` from `.claude.json`."""

    def test_the_fingerprint_is_the_org_and_the_account_hashed_together(self):
        path = profile_file(self.state / "claude.json")
        self.assertEqual(
            cp.read_account_fingerprint(path),
            qp.account_fingerprint("org-raw-0001/acct-raw-0001"),
        )
        # Moving the same user to another org is a different account here:
        # account a's 2026-10-11 switch was exactly that.
        moved = profile_file(self.state / "moved.json", org="org-raw-0002")
        self.assertNotEqual(cp.read_account_fingerprint(moved), cp.read_account_fingerprint(path))

    def test_an_unreadable_profile_is_unread_not_an_identity(self):
        cases = {
            "missing": None,
            "not json": "{half",
            "no account block": json.dumps({"numStartups": 1}),
            "block of the wrong type": json.dumps({"oauthAccount": "x"}),
            "no org": json.dumps({"oauthAccount": {"accountUuid": "a"}}),
            "no account": json.dumps({"oauthAccount": {"organizationUuid": "o"}}),
            "empty ids": json.dumps({"oauthAccount": {"accountUuid": "", "organizationUuid": ""}}),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                path = self.state / f"{name}.json"
                if text is not None:
                    path.write_text(text, encoding="utf-8")
                self.assertIsNone(cp.read_account_fingerprint(path))

    def test_the_default_paths_are_where_each_profile_keeps_them(self):
        names = [f"AI_RESETS_CLAUDE_{kind}_{label}" for kind in ("CONFIG", "HISTORY")
                 for label in ("A", "B")]
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in names:
                os.environ.pop(name, None)
            configs, histories = cp.profile_config_paths(), cp.history_paths()
        home = Path.home()
        # The primary profile keeps its config at $HOME, not inside ~/.claude.
        self.assertEqual(configs["a"], home / ".claude.json")
        self.assertEqual(configs["b"], home / ".claude-profiles" / "main1" / ".claude.json")
        self.assertEqual(histories["a"], home / ".claude" / "history.jsonl")
        self.assertEqual(histories["b"], home / ".claude-profiles" / "main1" / "history.jsonl")
        with mock.patch.dict(os.environ, {"AI_RESETS_CLAUDE_CONFIG_B": "/elsewhere/b.json"}):
            self.assertEqual(cp.profile_config_paths()["b"], Path("/elsewhere/b.json"))

    def test_every_poll_reads_the_profile_and_no_raw_id_is_ever_written(self):
        far_future_ms = 2_000_000_000 * 1000
        creds = credential_file(self.state / "creds-a.json", expires_at_ms=far_future_ms)
        config = profile_file(self.state / "claude-a.json")
        probe = cp.AccountProbe("a", creds, config)
        detector, claude_state = qp.Detector(), {}
        events = []
        for i, org in enumerate(("org-raw-0001", "org-raw-0001", "org-raw-0002")):
            profile_file(config, org=org)
            with mock.patch.object(cp, "fetch_usage", return_value=usage_payload()):
                cp.poll_all([probe], detector, claude_state, now=T0 + 300 * i)
        rows = [json.loads(line) for line in cp.SAMPLES_FILE.read_text().splitlines()]
        self.assertEqual(
            {row["account_fp"] for row in rows},
            {qp.account_fingerprint("org-raw-0001/acct-raw-0001"),
             qp.account_fingerprint("org-raw-0002/acct-raw-0001")},
        )
        events = [json.loads(line) for line in cp.EVENTS_FILE.read_text().splitlines()]
        self.assertTrue(events)
        self.assertTrue(all(e["kind"] == qp.EVENT_ACCOUNT_CHANGE for e in events))
        cp.export_observations(self.state / "anthropic.json", now=T0 + 600)
        for path in self.state_files():
            if path.name.startswith(("creds-", "claude-")):
                continue  # the inputs, which this probe did not write
            text = path.read_text(encoding="utf-8")
            for raw in ("org-raw-0001", "org-raw-0002", "acct-raw-0001",
                        "owner@example.invalid", "Example Org Name"):
                self.assertNotIn(raw, text, path.name)

    def test_the_default_probe_reads_the_configured_profile(self):
        with mock.patch.dict(os.environ, {"AI_RESETS_CLAUDE_CONFIG_A": "/x/a.json"}):
            probe = cp.AccountProbe("a", self.state / "creds-a.json")
        self.assertEqual(probe.config_path, Path("/x/a.json"))


class LocalTraceTests(StateDirTest):
    """The Claude Code reset menu, read from the profile's history at clear time."""

    ANCHOR = T0 + 3 * DAY

    def setUp(self):
        super().setUp()
        self.detector, self.claude_state = qp.Detector(), {}
        self.history = {label: self.state / f"history-{label}.jsonl" for label in ("a", "b")}
        patcher = mock.patch.dict(os.environ, {
            f"AI_RESETS_CLAUDE_HISTORY_{label.upper()}": str(path)
            for label, path in self.history.items()
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def feed(self, weekly, t, label="a", fp=FP, plan="max", session=22):
        rows = cp.normalise_usage(
            usage_payload(weekly=weekly, session=session, weekly_resets=self.ANCHOR),
            label, t, plan_type=plan, account_fp=fp,
        )
        return cp.observe_rows(self.detector, rows, self.claude_state)

    def early_clear(self, label="a", **edit):
        self.feed(48, T0, label, **edit)
        self.feed(0, T0 + 300, label, **edit)
        events = self.feed(0, T0 + 600, label, **edit)
        return [e for e in events if e.get("limit_kind") == "weekly_all"]

    def test_every_check_passing_is_unattributed_and_still_private(self):
        history_file(self.history["a"], [("/model", T0 - 100)])
        event = self.early_clear()[0]
        self.assertEqual(event["classification"], qp.CLASS_UNATTRIBUTED)
        self.assertEqual(event["checks"], {
            "identity": "same", "plan": "same", "credit": "no_bank",
            "bracket": "tight", "local_trace": "none",
        })
        self.assertEqual(event["window_family"], "weekly_all")
        self.assertFalse(cp.is_public(event, set()))
        self.assertEqual(cp.verdict_of(event), qp.VERDICT_UNATTRIBUTED)
        # Still compared with the other account, like any early clear.
        self.assertEqual(event["concordance"], cp.CONCORDANCE_UNKNOWN)

    def test_the_reset_menu_within_the_hour_is_the_owners_own_reset(self):
        # 2026-09-30, account b: /rate-limit-options 30 minutes before.
        for command in ("/rate-limit-options", "/limit-reset weekly"):
            with self.subTest(command=command):
                self.detector, self.claude_state = qp.Detector(), {}
                history_file(self.history["b"], [(command, T0 + 300 - 1800)])
                event = self.early_clear("b")[0]
                self.assertEqual(event["classification"], qp.CLASS_SELF)
                self.assertEqual(event["checks"]["local_trace"], "found")
                self.assertIn(command.split()[0], event["reason"])
                self.assertNotIn("concordance", event)

    def test_the_menu_is_found_even_when_another_check_could_not_run(self):
        # Contract step 4 precedes step 5: a positive own-cause needs nothing else.
        history_file(self.history["a"], [("/rate-limit-options", T0 + 100)])
        event = self.early_clear(fp=None)[0]
        self.assertEqual(event["classification"], qp.CLASS_SELF)

    def test_the_lookback_is_one_hour_before_the_bracket(self):
        lo, hi = T0, T0 + 300
        for moment, expected in (
            (lo - 3600, qp.CLASS_SELF),
            (lo - 3601, qp.CLASS_UNATTRIBUTED),
            (hi, qp.CLASS_SELF),
            (hi + 1, qp.CLASS_UNATTRIBUTED),
        ):
            with self.subTest(offset=moment - lo):
                self.detector, self.claude_state = qp.Detector(), {}
                history_file(self.history["a"], [("/rate-limit-options", moment)])
                self.assertEqual(self.early_clear()[0]["classification"], expected)

    def test_a_history_that_cannot_be_read_keeps_the_clear_unresolved(self):
        event = self.early_clear()[0]  # no file written
        self.assertEqual(event["classification"], qp.CLASS_GLOBAL)
        self.assertEqual(event["checks"]["local_trace"], "unread")
        self.assertIn("history could not be opened", event["reason"])
        self.assertEqual(cp.verdict_of(event), qp.VERDICT_UNRESOLVED)

    def test_a_reset_line_that_cannot_be_placed_is_unread_not_none(self):
        cases = {
            "torn line": '{"display": "/rate-limit-options", "timest\n',
            "no time": json.dumps({"display": "/rate-limit-options"}) + "\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.detector, self.claude_state = qp.Detector(), {}
                self.history["a"].write_text(text, encoding="utf-8")
                event = self.early_clear()[0]
                self.assertEqual(event["checks"]["local_trace"], "unread")
                self.assertEqual(event["classification"], qp.CLASS_GLOBAL)

    def test_a_command_named_mid_prompt_is_not_the_menu(self):
        history_file(self.history["a"], [("what does /rate-limit-options do?", T0 + 100)])
        self.assertEqual(self.early_clear()[0]["classification"], qp.CLASS_UNATTRIBUTED)

    def test_the_other_profiles_history_is_not_this_accounts(self):
        history_file(self.history["a"], [])
        history_file(self.history["b"], [("/rate-limit-options", T0 + 100)])
        self.assertEqual(self.early_clear("a")[0]["classification"], qp.CLASS_UNATTRIBUTED)

    def test_the_history_is_read_at_clear_time_only(self):
        history_file(self.history["a"], [])
        with mock.patch.object(cp, "reset_menu_trace", wraps=cp.reset_menu_trace) as read:
            for i in range(4):
                self.feed(48, T0 - 300 * (4 - i))
            self.assertEqual(read.call_count, 0)
            self.early_clear()
            # A natural expiry and a 5-hour clear never read it either.
            self.feed(0, T0 + 900, session=0)
            self.feed(0, T0 + 1200, session=0)
        self.assertEqual(read.call_count, 1)

    def test_a_check_that_could_not_run_is_named_alongside_the_trace(self):
        history_file(self.history["a"], [])
        event = self.early_clear(fp=None)[0]
        self.assertEqual(event["classification"], qp.CLASS_GLOBAL)
        self.assertIn("account fingerprint", event["reason"])
        self.assertIn("Personal limit resets can also restore weekly limits", event["reason"])

    def test_two_unattributed_accounts_are_recorded_as_agreeing(self):
        history_file(self.history["a"], [])
        history_file(self.history["b"], [])
        for label in ("a", "b"):
            self.feed(48, T0, label, fp=FP if label == "a" else OTHER_FP)
        for label in ("a", "b"):
            self.feed(0, T0 + 300, label, fp=FP if label == "a" else OTHER_FP)
        first = [e for e in self.feed(0, T0 + 600, "a") if e.get("limit_kind") == "weekly_all"]
        second = [e for e in self.feed(0, T0 + 600, "b", fp=OTHER_FP)
                  if e.get("limit_kind") == "weekly_all"]
        self.assertEqual(first[0]["classification"], qp.CLASS_UNATTRIBUTED)
        self.assertEqual(second[0]["concordance"], cp.CONCORDANCE_AGREEING)
        # One record never upgrades itself: the two-account rule runs over the
        # whole log in scripts/attribution.py (tests/test_attribution.py).
        self.assertFalse(cp.is_public(second[0], set()))

    def test_the_unattributed_sentence_says_what_was_checked_and_not_whose_it_was(self):
        history_file(self.history["a"], [])
        row = cp.observation(self.early_clear()[0], set())
        said = f"{row['headline']} {row['evidence']}"
        self.assertIn("with no cause on our side that this probe can see", row["headline"])
        self.assertIn("no Claude Code reset menu in the hour before", row["evidence"])
        self.assertIn("cannot say whose reset this was", row["evidence"])
        self.assertNotIn("vendor", said.lower())
        self.assertFalse(row["public"])


class ClaudeAccountChangeTests(StateDirTest):
    """2026-10-11 21:03 UTC: account a went from a Max org to a Team org."""

    ANCHOR = T0 + 3 * DAY

    def setUp(self):
        super().setUp()
        self.detector, self.claude_state = qp.Detector(), {}

    def feed(self, weekly, t, fp, plan, label="a"):
        rows = cp.normalise_usage(
            usage_payload(weekly=weekly, weekly_resets=self.ANCHOR), label, t,
            plan_type=plan, account_fp=fp,
        )
        return cp.observe_rows(self.detector, rows, self.claude_state)

    def test_the_org_switch_is_an_account_change_on_every_slot_not_a_clear(self):
        self.feed(100, T0, FP, "max")
        events = self.feed(1, T0 + 300, OTHER_FP, "team")
        self.assertTrue(events)
        self.assertEqual({e["kind"] for e in events}, {qp.EVENT_ACCOUNT_CHANGE})
        self.assertEqual({e["limit_kind"] for e in events},
                         {"session", "weekly_all", "weekly_scoped"})
        for event in events:
            # Never re-labelled session_only: it is not a clear on any window.
            self.assertEqual(event["classification"], qp.CLASS_ACCOUNT_CHANGE)
            self.assertEqual((event["plan_before"], event["plan_after"]), ("max", "team"))
            self.assertEqual(event["account"], "a")
        self.assertEqual(self.feed(1, T0 + 600, OTHER_FP, "team"), [])

    def test_a_plan_change_alone_is_enough_for_rows_written_before_fingerprints(self):
        self.feed(100, T0, None, "max")
        events = self.feed(1, T0 + 300, None, "team")
        self.assertEqual({e["kind"] for e in events}, {qp.EVENT_ACCOUNT_CHANGE})

    def test_one_switch_is_one_private_observation(self):
        self.feed(100, T0, FP, "max")
        events = self.feed(1, T0 + 300, OTHER_FP, "team")
        qp.append_jsonl(cp.EVENTS_FILE, events)
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0 + 600)
        rows = json.loads(out.read_text())["observations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], qp.VERDICT_ACCOUNT_CHANGE)
        self.assertFalse(rows[0]["public"])
        # Bracketed by the two reads it happened between, like a clear.
        self.assertEqual(rows[0]["observed_before"], qp.iso_utc(T0))
        self.assertEqual(rows[0]["observed_after"], qp.iso_utc(T0 + 300))
        self.assertIn("A different account on a different plan (max to team)", rows[0]["evidence"])
        self.assertNotIn(FP, rows[0]["evidence"])
        self.assertNotIn(OTHER_FP, rows[0]["evidence"])

    def test_the_change_is_logged_for_the_owner(self):
        self.feed(100, T0, FP, "max")
        with mock.patch.object(qp, "log") as logged:
            cp.log_events(self.feed(1, T0 + 300, OTHER_FP, "team"))
        self.assertIn("ACCOUNT CHANGED claude:a", logged.call_args_list[0][0][0])


class AccountsPhraseTests(unittest.TestCase):
    """The one public description of the population, derived not written."""

    def test_the_phrase_follows_the_plans_each_account_last_read(self):
        cases = {
            (("a", "team"), ("b", "max")): "Two accounts (Max and Team) on one operator machine",
            (("a", "max"), ("b", "max")): "Two Max accounts on one operator machine",
            (("a", "max"),): "Two accounts on one operator machine",
            (("a", "max"), ("b", None)): "Two accounts on one operator machine",
            (): "Two accounts on one operator machine",
            (("a", "ultra"), ("b", "ultra")): "Two Ultra accounts on one operator machine",
            (("a", "max"), ("b", "Owner Name <x@y>")): "Two accounts on one operator machine",
        }
        for plans, phrase in cases.items():
            with self.subTest(plans=plans):
                self.assertEqual(cp.accounts_phrase(dict(plans)), phrase)

    def test_each_account_s_plan_comes_from_its_newest_row(self):
        # Account a's per-model slot stopped reporting when it moved to Team;
        # its last row there still says max.
        detector = qp.Detector({"slots": {
            "claude:a:weekly_scoped:opus/10080": {"last": {"t": 100, "plan_type": "max"}},
            "claude:a:weekly_all/10080": {"last": {"t": 200, "plan_type": "team"}},
            "claude:b:weekly_all/10080": {"last": {"t": 150, "plan_type": "max"}},
        }})
        self.assertEqual(cp.plans_by_account(detector), {"a": "team", "b": "max"})

    def test_the_coverage_sentence_carries_the_derived_phrase(self):
        peaks = {"claude:a:weekly_all/10080": 40.0, "claude:b:weekly_all/10080": 30.0}
        sentence = cp.coverage_sentence(peaks, T0, {}, {"a": "team", "b": "max"})
        self.assertTrue(sentence.startswith("Two accounts (Max and Team) on one operator machine"))
        self.assertNotIn("20x", sentence)


# ─── Health, and the file it shares with the Codex probe ─────────────────────


CODEX_BLOCK = {
    "label": "codex",
    "updated_at": 1788700000,
    "last_ok_at": 1788699990,
    "last_sample_t": 1788699990,
    "consecutive_failures": 0,
    "blind_since": None,
    "throttled_until": None,
    "token_stale": False,
    "detectable_now": {"codex/10080": True},
    "last_error": None,
}

HEALTH_KEYS = {
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
}


class HealthTests(StateDirTest):
    def test_the_block_carries_exactly_the_shared_contract_keys(self):
        block = cp.health_block(
            qp.Detector(),
            "a",
            1788700000,
            last_ok_at=None,
            consecutive_failures=0,
            blind_since=None,
            throttled_until=None,
            token_stale=False,
            last_error=None,
        )
        self.assertEqual(set(block), HEALTH_KEYS)
        self.assertEqual(block["label"], "claude:a")

    def test_writing_a_claude_block_never_clobbers_the_codex_one(self):
        # Clobbering it would blind the Codex alerting, which is the one
        # failure this probe must not cause on its way in.
        qp.write_health(CODEX_BLOCK, "codex")
        detector = qp.Detector()
        for label in ("a", "b"):
            qp.write_health(
                cp.health_block(
                    detector,
                    label,
                    1788700100,
                    last_ok_at=1788700100,
                    consecutive_failures=0,
                    blind_since=None,
                    throttled_until=None,
                    token_stale=False,
                    last_error=None,
                ),
                cp.HEALTH_LABELS[label],
            )
        health = json.loads(cp.HEALTH_FILE.read_text())
        self.assertEqual(sorted(health), ["claude:a", "claude:b", "codex"])
        self.assertEqual(health["codex"], CODEX_BLOCK)

    def test_detectable_and_last_sample_are_per_account(self):
        detector = qp.Detector()
        state = {}
        cp.observe_rows(
            detector, cp.normalise_usage(usage_payload(weekly=48), "a", 500), state
        )
        cp.observe_rows(
            detector, cp.normalise_usage(usage_payload(weekly=3), "b", 600), state
        )
        block_a = cp.health_block(
            detector, "a", 700, last_ok_at=500, consecutive_failures=0,
            blind_since=None, throttled_until=None, token_stale=False, last_error=None,
        )
        block_b = cp.health_block(
            detector, "b", 700, last_ok_at=600, consecutive_failures=0,
            blind_since=None, throttled_until=None, token_stale=False, last_error=None,
        )
        self.assertEqual(block_a["last_sample_t"], 500)
        self.assertEqual(block_b["last_sample_t"], 600)
        self.assertTrue(all(k.startswith("claude:a:") for k in block_a["detectable_now"]))
        self.assertTrue(block_a["detectable_now"]["claude:a:weekly_all/10080"])
        # Account b's weekly window is too empty for a clear to show, which is
        # the difference between "not seen" and "could not have been seen".
        self.assertFalse(block_b["detectable_now"]["claude:b:weekly_all/10080"])

    def test_an_unwritable_health_file_does_not_kill_the_loop(self):
        with mock.patch.object(
            qp.Path, "mkdir", side_effect=OSError("read-only file system")
        ):
            qp.write_health(CODEX_BLOCK, "claude:a")  # must not raise

    def test_the_error_line_is_redacted_before_it_reaches_the_file(self):
        block = cp.health_block(
            qp.Detector(), "a", 1, last_ok_at=None, consecutive_failures=1,
            blind_since=1, throttled_until=None, token_stale=False,
            last_error=f"rejected Bearer {FAKE_TOKEN}",
        )
        self.assertNotIn(FAKE_TOKEN, block["last_error"])


# ─── One full cycle, end to end ──────────────────────────────────────────────


class CycleTests(StateDirTest):
    def setUp(self):
        super().setUp()
        far_future_ms = 2_000_000_000 * 1000
        self.paths = {
            label: credential_file(
                self.state / f"creds-{label}.json", expires_at_ms=far_future_ms
            )
            for label in ("a", "b")
        }
        self.probes = [cp.AccountProbe(label, path) for label, path in sorted(self.paths.items())]
        self.detector = qp.Detector()
        self.claude_state = {}

    def cycle(self, t, payload_a, payload_b):
        with mock.patch.object(cp, "fetch_usage", side_effect=[payload_a, payload_b]):
            return cp.poll_all(self.probes, self.detector, self.claude_state, now=t)

    def clear_run(self, anchor=None):
        """Three polls in which both accounts' weekly windows clear together."""
        anchor = anchor or (T0 + 3 * DAY)
        busy = usage_payload(weekly=48, weekly_resets=anchor)
        clear = usage_payload(weekly=0, weekly_resets=anchor)
        self.cycle(T0, busy, busy)
        self.cycle(T0 + 300, clear, clear)
        return self.cycle(T0 + 600, clear, clear)

    def test_a_healthy_cycle_writes_samples_health_and_a_cursor(self):
        summary = self.cycle(T0, usage_payload(), usage_payload())
        self.assertEqual(summary["succeeded"], 2)
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["written"], 6)  # three windows on each account
        self.assertTrue(cp.SAMPLES_FILE.is_file())
        self.assertTrue(cp.CURSOR_FILE.is_file())
        health = json.loads(cp.HEALTH_FILE.read_text())
        self.assertEqual(sorted(health), ["claude:a", "claude:b"])
        self.assertEqual(health["claude:a"]["last_ok_at"], T0)
        self.assertEqual(health["claude:a"]["consecutive_failures"], 0)

    def test_an_unchanged_window_is_not_written_twice(self):
        self.cycle(T0, usage_payload(), usage_payload())
        self.cycle(T0 + 300, usage_payload(), usage_payload())
        rows = [json.loads(line) for line in cp.SAMPLES_FILE.read_text().splitlines()]
        self.assertEqual(len(rows), 6)

    def test_one_account_failing_does_not_lose_the_other(self):
        with mock.patch.object(
            cp, "fetch_usage", side_effect=[usage_payload(), qp.ProbeError("no route")]
        ):
            summary = cp.poll_all(self.probes, self.detector, self.claude_state, now=T0)
        self.assertEqual(summary["succeeded"], 1)
        health = json.loads(cp.HEALTH_FILE.read_text())
        self.assertEqual(health["claude:a"]["consecutive_failures"], 0)
        self.assertEqual(health["claude:b"]["consecutive_failures"], 1)
        self.assertEqual(health["claude:b"]["blind_since"], T0)

    def test_the_state_directory_never_contains_a_token(self):
        # The single hardest requirement in this lane: the token is read on
        # every poll and must exist nowhere but the request header.
        self.clear_run()
        cp.export_observations(self.state / "anthropic.json", now=T0 + 600)
        for path in self.state_files():
            if path.name.startswith("creds-"):
                continue  # the credential file itself, which we did not write
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("accessToken", text, path.name)
            self.assertNotIn(FAKE_TOKEN, text, path.name)

    def test_early_clears_reach_the_private_export_with_an_unverified_cause(self):
        self.clear_run()
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0 + 600)
        payload = json.loads(out.read_text())
        self.assertEqual(payload["vendor"], "anthropic")
        self.assertEqual(payload["probe"]["status"], qp.PROBE_OK)
        public = [row for row in payload["observations"] if row["public"]]
        self.assertEqual(public, [])
        self.assertTrue(payload["observations"])
        for row in payload["observations"]:
            self.assertEqual(row["verdict"], qp.VERDICT_UNRESOLVED)
            self.assertEqual(row["window"], "weekly")
            # Everything build.py copies onto the site has to be present.
            for field in (
                "observed_at", "window", "verdict", "headline", "evidence",
                "used_before", "used_after", "early_by_hours",
                "observed_before", "observed_after",
            ):
                self.assertIn(field, row)

    def test_the_published_sentences_stay_inside_what_we_can_defend(self):
        self.clear_run()
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0 + 600)
        payload = json.loads(out.read_text())
        text = " ".join(
            f"{row['headline']} {row['evidence']}" for row in payload["observations"]
        )
        text += " " + payload["probe"]["coverage"]
        for forbidden in (
            "no reset happened",
            "the vendor reset everyone",
            "everyone",
            "@",
            "organization",
            "organisation",
        ):
            self.assertNotIn(forbidden, text.lower(), forbidden)
        self.assertIn("the cause is unverified", text)
        self.assertNotIn("nothing this account did explains it", text)
        self.assertIn("Personal limit resets can restore weekly limits too", text)
        # Both fixture credentials read subscriptionType "max".
        self.assertIn("Two Max accounts on one operator machine", text)
        self.assertNotIn("20x", text)

    def test_a_five_hour_clear_never_reaches_the_export(self):
        anchor = T0 + 4 * 3600
        busy = usage_payload(session=22, session_resets=anchor)
        idle = usage_payload(session=0, session_resets=anchor)
        self.cycle(T0, busy, busy)
        self.cycle(T0 + 300, idle, idle)
        self.cycle(T0 + 600, idle, idle)
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0 + 600)
        payload = json.loads(out.read_text())
        self.assertTrue(payload["observations"])
        self.assertEqual([row for row in payload["observations"] if row["public"]], [])
        for row in payload["observations"]:
            self.assertEqual(row["classification"], cp.CLASS_SESSION_ONLY)
            self.assertIsNone(row["verdict"])

    def test_a_blind_account_makes_the_whole_probe_blind(self):
        self.clear_run()
        health = json.loads(cp.HEALTH_FILE.read_text())
        health["claude:b"]["blind_since"] = T0
        cp.HEALTH_FILE.write_text(json.dumps(health))
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0 + 600)
        payload = json.loads(out.read_text())
        # A blind half cannot be rendered as a clean look.
        self.assertEqual(payload["probe"]["status"], qp.PROBE_BLIND)

    def test_a_never_run_probe_exports_as_absent_rather_than_quiet(self):
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0)
        payload = json.loads(out.read_text())
        self.assertEqual(payload["probe"]["status"], qp.PROBE_ABSENT)
        self.assertEqual(payload["observations"], [])
        self.assertIn("No weekly samples are on record", payload["probe"]["coverage"])

    def test_the_coverage_sentence_names_the_account_that_is_too_quiet(self):
        anchor = T0 + 3 * DAY
        self.cycle(T0, usage_payload(weekly=48, weekly_resets=anchor), usage_payload(weekly=3, scoped=2, weekly_resets=anchor))
        out = self.state / "anthropic.json"
        cp.export_observations(out, now=T0)
        coverage = json.loads(out.read_text())["probe"]["coverage"]
        self.assertIn("Account b's weekly window has never read above 3%", coverage)
        self.assertIn("/limit-reset", coverage)

    def test_the_export_survives_a_half_written_final_line(self):
        self.clear_run()
        with cp.EVENTS_FILE.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "clear", "slot": "clau')
        out = self.state / "anthropic.json"
        self.assertEqual(cp.export_observations(out, now=T0 + 600), 0)
        self.assertTrue(json.loads(out.read_text())["observations"])

    def test_groundtruth_reads_this_probes_events_and_labels(self):
        # The wiring, not the theory: groundtruth.py declares where this
        # probe's files live, and a rename on either side must fail here
        # rather than silently make the email say "we do not run a probe".
        spec = gt.PROBE_SPECS["anthropic"]
        self.assertEqual(spec.events_filename, cp.EVENTS_FILE.name)
        self.assertEqual(spec.labels, tuple(cp.HEALTH_LABELS[l] for l in cp.LABELS))
        self.clear_run()
        self.assertIn("anthropic", gt.probed_vendors(self.state, now=T0 + 600))
        clears = gt.clear_observations(self.state, vendor="anthropic")
        self.assertTrue(clears)
        self.assertEqual(clears[0]["verdict"], gt.VERDICT_UNRESOLVED)

    def test_groundtruth_never_reads_a_five_hour_clear_as_a_vendor_reset(self):
        anchor = T0 + 4 * 3600
        self.cycle(T0, usage_payload(session=22, session_resets=anchor), usage_payload())
        self.cycle(T0 + 300, usage_payload(session=0, session_resets=anchor), usage_payload())
        self.cycle(T0 + 600, usage_payload(session=0, session_resets=anchor), usage_payload())
        found = gt.observation_near(T0 + 400, self.state, vendor="anthropic")
        self.assertIsNone(found)


# ─── The loop, and what it hands to systemd ──────────────────────────────────


class RunLoopTests(StateDirTest):
    def setUp(self):
        super().setUp()
        far_future_ms = 2_000_000_000 * 1000
        for label in ("a", "b"):
            credential_file(
                self.state / f"creds-{label}.json", expires_at_ms=far_future_ms
            )
        for label in ("a", "b"):
            patcher = mock.patch.dict(
                os.environ,
                {f"AI_RESETS_CLAUDE_CREDS_{label.upper()}": str(self.state / f"creds-{label}.json")},
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_once_exits_zero_on_a_reading_and_non_zero_on_none(self):
        with mock.patch.object(cp, "fetch_usage", return_value=usage_payload()):
            self.assertEqual(cp.run(once=True), 0)
        with mock.patch.object(cp, "fetch_usage", side_effect=qp.ProbeError("no route")):
            # A smoke test must not be able to pass on a probe that never
            # actually read anything.
            self.assertEqual(cp.run(once=True), 1)

    def test_a_sustained_outage_exits_two_for_systemd(self):
        with mock.patch.object(cp, "fetch_usage", side_effect=qp.ProbeError("no route")):
            with mock.patch.object(cp.time, "sleep"):
                with self.assertRaises(SystemExit) as caught:
                    cp.run(once=False)
        self.assertEqual(caught.exception.code, 2)
        health = json.loads(cp.HEALTH_FILE.read_text())
        self.assertEqual(health["claude:a"]["consecutive_failures"], cp.BLIND_EXIT_FAILURES)
        self.assertIsNotNone(health["claude:a"]["blind_since"])

    def test_a_throttled_pair_waits_instead_of_restart_looping(self):
        # Neither a backoff nor a stale credential is fixed by a restart, so
        # they must not accumulate toward the exit that asks for one.
        error = cp.ThrottledError("429", retry_after=600)
        with mock.patch.object(cp, "fetch_usage", side_effect=error):
            with mock.patch.object(cp.time, "sleep") as slept:
                slept.side_effect = [None] * 30 + [StopIteration]
                with self.assertRaises((StopIteration, RuntimeError)):
                    cp.run(once=False)
        health = json.loads(cp.HEALTH_FILE.read_text())
        self.assertIsNotNone(health["claude:a"]["throttled_until"])

    def test_export_runs_from_disk_with_no_network_at_all(self):
        out = self.state / "anthropic.json"
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(cp.main(["--export", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text())["vendor"], "anthropic")
