import base64
import contextlib
import os
import hashlib
import hmac
import html
import io
import json
import re
import sqlite3
import tempfile
import sys
import threading
import time
import unittest
import unittest.mock
import urllib.parse
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.subscriptions import (
    CLAIM_NONE,
    CLAIM_PENDING,
    CLAIM_SETTLED,
    FLOOD_CAP,
    ORIGIN_ANNOUNCEMENT,
    ORIGIN_PROBE,
    STAGE_CONFIRMED,
    STAGE_NEW,
    STAGE_RETRACTED,
    UNIT_ALERT_REPEAT_SECONDS,
    Config,
    Store,
    SubscriptionApp,
    event_origin,
    flatten_events,
    incident_key,
    mail_stage,
    make_handler,
    normalize_email,
    normalize_stages,
    plan_adoptions,
    plan_discovery,
    run_notifier,
    run_unit_alert,
    safe_http_url,
)
from scripts.subscriptions import main as subscriptions_main


# The evening the delivery guards were written. Fixing the clock keeps the
# 48-hour age cutoff deterministic instead of making every fixture expire.
NOW = 1788573600


def iso(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(key, vendor, *, offset=600, **extra):
    payload = {
        "event_key": key,
        "vendor": vendor,
        "id": key.split(":", 1)[1],
        "announced_at": iso(NOW - offset),
        "text": "We have reset usage limits.",
        "url": "https://example.test/post",
        "kind": "reset",
    }
    payload.update(extra)
    return payload


class FakeMailer:
    def __init__(self):
        self.messages = []
        # How many of the next sends raise, for the retry path. Resend's 429
        # and a network error both arrive as RuntimeError.
        self.fail_next = 0

    def send(self, **message):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("Resend HTTP 429: rate_limit_exceeded")
        self.messages.append(message)
        return f"message-{len(self.messages)}"


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.config = Config(
            api_key="test-key",
            secret="s" * 48,
            db_path=root / "subscriptions.db",
            public_url="https://resets.example.test",
            from_email="AI Reset Watch <updates@example.test>",
            data_file=root / "data.json",
            webhook_secret="whsec_" + base64.b64encode(b"w" * 32).decode(),
        )
        self.store = Store(self.config.db_path)
        self.store.init()
        self.mailer = FakeMailer()
        self.app = SubscriptionApp(self.config, self.store, self.mailer)

    def subscriber_status(self, email):
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT status FROM subscribers WHERE email = ?", (email,)
            ).fetchone()
            return row["status"] if row else None

    def confirmation_token(self, message_index=0):
        body = self.mailer.messages[message_index]["html_body"]
        href = html.unescape(re.search(r'href="([^"]+)"', body).group(1))
        return urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)["token"][0]

    def webhook_headers(self, body, message_id="msg_test"):
        timestamp = str(int(time.time()))
        key = base64.b64decode(self.config.webhook_secret.removeprefix("whsec_"))
        signed = f"{message_id}.{timestamp}.".encode() + body
        signature = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()
        ).decode()
        return {
            "svix-id": message_id,
            "svix-timestamp": timestamp,
            "svix-signature": f"v1,{signature}",
        }

    def test_normalize_email(self):
        self.assertEqual(normalize_email(" Person@Example.COM "), "person@example.com")
        for invalid in ("", "not-an-email", "a@localhost", "a b@example.com"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_email(invalid)

    def test_double_opt_in_and_resubscribe(self):
        email = "person@example.com"
        self.app.subscribe(email, "192.0.2.1")
        self.assertEqual(self.subscriber_status(email), "pending")
        self.assertEqual(len(self.mailer.messages), 1)

        # Pending requests inside the cooldown do not send another message.
        self.app.subscribe(email, "192.0.2.1")
        self.assertEqual(len(self.mailer.messages), 1)

        confirm_token = self.confirmation_token()
        self.app.confirm(confirm_token)
        self.assertEqual(self.subscriber_status(email), "active")

        # Do not reveal an existing subscription or send a redundant email.
        self.app.subscribe(email, "192.0.2.1")
        self.assertEqual(len(self.mailer.messages), 1)

        unsubscribe_token = self.app.make_token(email, "unsubscribe", 3600)
        self.app.unsubscribe(unsubscribe_token)
        self.assertEqual(self.subscriber_status(email), "unsubscribed")

        self.app.subscribe(email, "192.0.2.1", ["google"])
        self.assertEqual(len(self.mailer.messages), 2)
        self.app.confirm(self.confirmation_token(1))
        self.assertEqual(self.subscriber_status(email), "active")
        self.assertEqual(self.store.subscriber_topics(email), ("google",))

    def test_token_is_bound_to_purpose_and_signature(self):
        token = self.app.make_token("person@example.com", "confirm", 3600)
        self.assertEqual(
            self.app.parse_token(token, "confirm"),
            "person@example.com",
        )
        with self.assertRaises(ValueError):
            self.app.parse_token(token, "unsubscribe")
        with self.assertRaises(ValueError):
            self.app.parse_token(token + "x", "confirm")

    def test_first_feed_is_baseline_and_new_event_is_queued(self):
        email = "person@example.com"
        self.store.request_subscription(email)
        self.store.activate(email)
        original = [
            {
                "event_key": "openai:one",
                "vendor": "openai",
                "id": "one",
                "announced_at": iso(NOW - 3600),
                "text": "Original event",
                "url": "https://example.test/one",
            }
        ]
        plan = self.store.discover_events(original, now=NOW)
        self.assertTrue(plan.baseline)
        self.assertEqual(plan.new_count, 1)
        self.assertEqual(self.store.pending_deliveries(), [])

        updated = original + [
            {
                "event_key": "anthropic:two",
                "vendor": "anthropic",
                "id": "two",
                "announced_at": iso(NOW - 600),
                "text": "New event",
                "url": "https://example.test/two",
            }
        ]
        plan = self.store.discover_events(updated, now=NOW)
        self.assertFalse(plan.baseline)
        self.assertEqual(plan.new_count, 1)
        pending = self.store.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["incident_key"], "anthropic:two")
        self.assertEqual(pending[0]["email"], email)

    def test_deliveries_respect_selected_provider_topics(self):
        email = "person@example.com"
        self.store.request_subscription(email, ("openai",))
        self.store.activate(email)
        self.store.discover_events(
            [event("openai:baseline", "openai")], now=NOW
        )
        self.store.discover_events(
            [event("anthropic:new", "anthropic")], now=NOW
        )
        self.assertEqual(self.store.pending_deliveries(), [])
        self.store.discover_events([event("openai:new", "openai")], now=NOW)
        pending = self.store.pending_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["incident_key"], "openai:new")

    def test_webhook_suppresses_bounces_and_is_idempotent(self):
        email = "person@example.com"
        self.store.request_subscription(email)
        self.store.activate(email)
        body = json.dumps(
            {"type": "email.bounced", "data": {"to": [email]}}
        ).encode()
        headers = self.webhook_headers(body)
        self.assertTrue(self.app.handle_webhook(headers, body))
        self.assertEqual(self.subscriber_status(email), "unsubscribed")
        self.assertFalse(self.app.handle_webhook(headers, body))
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT suppression_reason FROM subscribers WHERE email = ?",
                (email,),
            ).fetchone()
        self.assertEqual(row["suppression_reason"], "bounce")

    def test_webhook_rejects_invalid_signature(self):
        body = json.dumps({"type": "email.bounced", "data": {"to": []}}).encode()
        headers = self.webhook_headers(body)
        headers["svix-signature"] = "v1,not-valid"
        with self.assertRaises(ValueError):
            self.app.handle_webhook(headers, body)

    def test_unsubscribed_recipient_is_not_returned_for_delivery(self):
        email = "person@example.com"
        self.store.request_subscription(email)
        self.store.activate(email)
        self.store.discover_events([event("openai:one", "openai")], now=NOW)
        self.store.discover_events([event("openai:two", "openai")], now=NOW)
        self.assertEqual(len(self.store.pending_deliveries()), 1)
        self.store.unsubscribe(email)
        self.assertEqual(self.store.pending_deliveries(), [])

    def test_notification_has_one_click_unsubscribe_headers(self):
        event = {
            "vendor": "google",
            "announced_at": "2026-07-28T00:00:00Z",
            "text": "Quota policy changed.",
            "url": "https://example.test/source",
        }
        subject, html_body, text_body, unsubscribe_url, headers = (
            self.app.notification_message("person@example.com", event)
        )
        # No `kind` on this fixture, so it must claim nothing beyond "update".
        self.assertEqual(subject, "Gemini: usage limits update")
        self.assertIn("Google / Gemini", html_body)
        self.assertIn("Quota policy changed.", html_body)
        self.assertIn(unsubscribe_url, text_body)
        self.assertEqual(headers["List-Unsubscribe"], f"<{unsubscribe_url}>")
        self.assertEqual(
            headers["List-Unsubscribe-Post"],
            "List-Unsubscribe=One-Click",
        )

    def test_flatten_events_uses_vendor_scoped_key(self):
        data = {
            "vendors": {
                "openai": {
                    "events": [
                        {
                            "id": "123",
                            "text": "Reset",
                            "announced_at": "2026-07-28T00:00:00Z",
                        }
                    ]
                }
            }
        }
        events = flatten_events(json.loads(json.dumps(data)))
        self.assertEqual(events[0]["event_key"], "openai:123")
        self.assertEqual(events[0]["vendor"], "openai")

    def test_only_http_source_urls_are_allowed(self):
        fallback = "https://resets.example.test"
        self.assertEqual(
            safe_http_url("https://example.test/source", fallback),
            "https://example.test/source",
        )
        self.assertEqual(safe_http_url("javascript:alert(1)", fallback), fallback)

    def test_http_subscription_endpoint(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/subscribe",
            data=json.dumps({"email": "person@example.com", "website": ""}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://resets.example.test",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(response.status, 202)
        self.assertIn("confirm", payload["message"].lower())
        self.assertEqual(len(self.mailer.messages), 1)

    def test_confirmation_get_requires_explicit_post(self):
        email = "person@example.com"
        self.app.subscribe(email, "192.0.2.1", ["openai"])
        token = self.confirmation_token()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = (
            f"http://127.0.0.1:{server.server_port}/api/confirm?"
            + urllib.parse.urlencode({"token": token})
        )
        with urllib.request.urlopen(url) as response:
            page = response.read().decode()
        self.assertIn("Confirm your alerts", page)
        self.assertEqual(self.subscriber_status(email), "pending")
        self.app.confirm(token)
        self.assertEqual(self.subscriber_status(email), "active")

    def test_http_subscription_rejects_cross_origin_request(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/subscribe",
            data=json.dumps({"email": "person@example.com"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(len(self.mailer.messages), 0)

    def test_unsubscribe_get_confirms_and_post_changes_state(self):
        email = "person@example.com"
        self.store.request_subscription(email)
        self.store.activate(email)
        token = self.app.make_token(email, "unsubscribe", 3600)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = (
            f"http://127.0.0.1:{server.server_port}/api/unsubscribe"
            f"?token={token}"
        )

        with urllib.request.urlopen(url) as response:
            page = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("Stop all alerts?", page)
        self.assertEqual(self.subscriber_status(email), "active")

        request = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), b"")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.subscriber_status(email), "unsubscribed")


class DeliveryGuardTests(unittest.TestCase):
    """The guards that stand between a feed refresh and seven inboxes."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.state = root / "state"
        self.state.mkdir()
        self.config = Config(
            api_key="test-key",
            secret="s" * 48,
            db_path=root / "subscriptions.db",
            public_url="https://resets.example.test",
            from_email="AI Reset Watch <updates@example.test>",
            data_file=root / "data.json",
            state_dir=self.state,
            owner_email="owner@example.test",
        )
        self.store = Store(self.config.db_path)
        self.store.init()
        self.mailer = FakeMailer()
        self.app = SubscriptionApp(self.config, self.store, self.mailer)
        for address in ("one@example.com", "two@example.com"):
            self.store.request_subscription(address)
            self.store.activate(address)

    def write_feed(self, events):
        payload = {
            "generated_at": iso(NOW - 60),
            "vendors": {"openai": {"events": events}},
        }
        self.config.data_file.write_text(json.dumps(payload))

    def write_health(self, **overrides):
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
        }
        entry.update(overrides)
        (self.state / "probe_health.json").write_text(json.dumps({"codex": entry}))

    def statuses(self):
        with self.store.connect() as conn:
            return [
                (row["incident_key"], row["status"])
                for row in conn.execute(
                    "SELECT incident_key, status FROM deliveries "
                    "ORDER BY incident_key, email"
                )
            ]

    def baseline(self):
        self.store.discover_events([event("openai:baseline", "openai")], now=NOW)

    # ─── Age cutoff ──────────────────────────────────────────────────────────

    def test_an_event_older_than_the_cutoff_is_recorded_but_never_queued(self):
        # The Anthropic import case: 11 events from April to August would have
        # gone out as breaking news the moment that feed was wired up.
        self.baseline()
        plan = self.store.discover_events(
            [event("openai:old", "openai", offset=72 * 3600)], now=NOW
        )
        self.assertEqual(plan.new_count, 1)
        self.assertEqual(len(plan.historical), 1)
        self.assertEqual(plan.mailable, ())
        self.assertEqual(self.store.pending_deliveries(), [])
        # Recorded, so it can never be rediscovered as new later.
        self.assertIn("openai:old", self.store.known_event_keys())

    def test_an_event_just_inside_the_cutoff_is_still_queued(self):
        self.baseline()
        plan = self.store.discover_events(
            [event("openai:fresh", "openai", offset=47 * 3600)], now=NOW
        )
        self.assertEqual(len(plan.mailable), 1)
        self.assertEqual(len(self.store.pending_deliveries()), 2)

    def test_an_undatable_event_is_recorded_but_never_queued(self):
        self.baseline()
        undated = event("openai:undated", "openai")
        del undated["announced_at"]
        plan = self.store.discover_events([undated], now=NOW)
        self.assertEqual(len(plan.undatable), 1)
        self.assertEqual(plan.historical, ())
        self.assertEqual(self.store.pending_deliveries(), [])

    def test_an_unfused_own_account_observation_is_recorded_but_never_mailed(self):
        # A probe-originated row carries observed_at and not announced_at. It is
        # a measurement of one account, not a vendor action, until the fusion
        # layer raises it to `observed` — so it is recorded and nothing else.
        self.baseline()
        observed = event("openai:probe", "openai")
        del observed["announced_at"]
        observed["observed_at"] = iso(NOW - 120)
        plan = self.store.discover_events([observed], now=NOW)
        self.assertEqual(len(plan.silent), 1)
        self.assertEqual(plan.mailable, ())
        self.assertEqual(self.store.pending_deliveries(), [])
        self.assertIn("openai:probe", self.store.known_event_keys())

    def test_observed_at_is_accepted_as_a_timestamp(self):
        # Once fused, the same row is mailable and its observed_at is the clock
        # the 48-hour cutoff reads: a probe row has no announced_at at all.
        self.baseline()
        observed = event("openai:probe", "openai", status="observed")
        del observed["announced_at"]
        observed["observed_at"] = iso(NOW - 120)
        plan = self.store.discover_events([observed], now=NOW)
        self.assertEqual(len(plan.mailable), 1)
        self.assertEqual(plan.silent, ())

    # ─── Flood cap ───────────────────────────────────────────────────────────

    def test_more_than_the_cap_holds_the_whole_batch_unsent(self):
        self.baseline()
        batch = [event(f"openai:{index}", "openai") for index in range(FLOOD_CAP + 1)]
        plan = self.store.discover_events(batch, now=NOW)
        self.assertTrue(plan.flooded)
        self.assertEqual(self.store.pending_deliveries(), [])
        self.assertEqual(
            len(self.store.held_deliveries()), (FLOOD_CAP + 1) * 2
        )

    def test_a_batch_at_the_cap_still_goes_out(self):
        self.baseline()
        batch = [event(f"openai:{index}", "openai") for index in range(FLOOD_CAP)]
        plan = self.store.discover_events(batch, now=NOW)
        self.assertFalse(plan.flooded)
        self.assertEqual(len(self.store.pending_deliveries()), FLOOD_CAP * 2)

    def test_release_moves_held_deliveries_into_the_queue_once(self):
        self.baseline()
        self.store.discover_events(
            [event(f"openai:{index}", "openai") for index in range(FLOOD_CAP + 1)],
            now=NOW,
        )
        moved = self.store.release_held(now=NOW)
        self.assertEqual(moved, (FLOOD_CAP + 1) * 2)
        self.assertEqual(len(self.store.pending_deliveries()), (FLOOD_CAP + 1) * 2)
        self.assertEqual(self.store.release_held(now=NOW), 0)

    # ─── An incident that improves ───────────────────────────────────────────

    def test_a_row_recorded_while_unmailable_is_mailed_once_it_improves(self):
        """The whole point of the lifecycle: an incident gets better.

        A probe clear arrives as `observed_pending` and may not be mailed. When
        a witness turns up it becomes `observed`, which may. Recording it the
        first time must not settle it forever — that silently removed the
        observed_pending -> observed path, which is the headline P4 capability.
        """
        self.baseline()
        pending = event("openai:probe-1", "openai", status="observed_pending")
        plan = self.store.discover_events([pending], now=NOW)
        self.assertEqual(len(plan.silent), 1)
        self.assertEqual(self.store.pending_deliveries(), [])

        # Same row, better status.
        improved = {**pending, "status": "observed"}
        plan = self.store.discover_events([improved], now=NOW)
        self.assertEqual(len(plan.ripened), 1)
        self.assertEqual(len(plan.mailable), 1)
        self.assertEqual(len(self.store.pending_deliveries()), 2)

    def test_an_improved_row_is_not_mailed_twice(self):
        self.baseline()
        pending = event("openai:probe-2", "openai", status="observed_pending")
        self.store.discover_events([pending], now=NOW)
        improved = {**pending, "status": "observed"}
        self.store.discover_events([improved], now=NOW)
        first = len(self.store.pending_deliveries())
        # Every later refresh sees the same improved row.
        for _ in range(3):
            plan = self.store.discover_events([improved], now=NOW)
            self.assertEqual(plan.ripened, ())
        self.assertEqual(len(self.store.pending_deliveries()), first)

    def test_a_row_that_never_improves_is_never_mailed(self):
        self.baseline()
        stuck = event("openai:probe-3", "openai", status="observed_pending")
        for _ in range(3):
            self.store.discover_events([stuck], now=NOW)
        self.assertEqual(self.store.pending_deliveries(), [])

    # ─── Dry run ─────────────────────────────────────────────────────────────

    def test_dry_run_writes_nothing_and_names_what_would_be_sent(self):
        self.baseline()
        self.write_health()
        (self.state / "quota_cursor.json").write_text(
            json.dumps(
                {
                    "slots": {
                        "codex/10080": {
                            "last": {
                                "t": NOW - 30,
                                "used_percent": 94.0,
                                "credits_available": 1,
                                "resets_at": 1788926992,
                            }
                        }
                    }
                }
            )
        )
        self.write_feed(
            [
                {
                    "id": "banked",
                    "kind": "reset",
                    "upstream_reset_type": "banked",
                    "announced_at": iso(NOW - 600),
                    "text": "we will do the full banked reset today too. Lands end of day.",
                    "url": "https://example.test/banked",
                }
            ]
        )
        before = self.store.known_event_keys()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_notifier(self.config, self.app, dry_run=True, now=NOW)
        output = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertEqual(self.mailer.messages, [])
        self.assertEqual(self.store.known_event_keys(), before)
        self.assertEqual(self.store.pending_deliveries(), [])
        self.assertIn("would send to 2", output)
        self.assertIn("banked reset announced — not yet landed", output)
        self.assertIn("Not landed on our Pro account", output)

    # ─── Owner alerts ────────────────────────────────────────────────────────

    def test_a_blind_probe_alerts_the_owner_once_and_recovers_once(self):
        self.baseline()
        self.write_feed([])
        self.write_health(blind_since=NOW - 3600, consecutive_failures=35)

        run_notifier(self.config, self.app, now=NOW)
        alerts = [m for m in self.mailer.messages if m["to"] == "owner@example.test"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("[ai-resets alert]", alerts[0]["subject"])
        self.assertIn("probe:codex:blind", alerts[0]["subject"])

        # A six-hour outage must not become seventy-two emails.
        run_notifier(self.config, self.app, now=NOW)
        self.assertEqual(
            len([m for m in self.mailer.messages if m["to"] == "owner@example.test"]), 1
        )

        self.write_health()
        run_notifier(self.config, self.app, now=NOW)
        alerts = [m for m in self.mailer.messages if m["to"] == "owner@example.test"]
        self.assertEqual(len(alerts), 2)
        self.assertIn("recovered", alerts[1]["subject"])

    def test_a_stalled_publish_pipeline_alerts_the_owner(self):
        self.baseline()
        self.write_health()
        self.config.data_file.write_text(
            json.dumps({"generated_at": iso(NOW - 4 * 3600), "vendors": {}})
        )
        run_notifier(self.config, self.app, now=NOW)
        subjects = [
            m["subject"] for m in self.mailer.messages if m["to"] == "owner@example.test"
        ]
        self.assertTrue(any("data:stale" in subject for subject in subjects), subjects)

    def test_alerts_are_reported_loudly_when_no_owner_address_is_configured(self):
        self.baseline()
        self.write_feed([])
        self.write_health(blind_since=NOW - 3600)
        app = SubscriptionApp(
            Config(**{**self.config.__dict__, "owner_email": ""}),
            self.store,
            self.mailer,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_notifier(app.config, app, now=NOW)
        self.assertIn("no OWNER_EMAIL set", buffer.getvalue())
        self.assertEqual(self.mailer.messages, [])

    # ─── The message itself ──────────────────────────────────────────────────

    def test_the_email_quotes_the_post_and_states_what_we_saw(self):
        self.write_health()
        (self.state / "quota_cursor.json").write_text(
            json.dumps(
                {
                    "slots": {
                        "codex/10080": {
                            "last": {"t": NOW - 30, "used_percent": 98.0, "credits_available": 1}
                        }
                    }
                }
            )
        )
        subject, html_body, text_body, _, _ = self.app.notification_message(
            "one@example.com",
            {
                "vendor": "openai",
                "kind": "reset",
                "upstream_reset_type": "banked",
                "announced_at": "2026-09-05T00:39:25Z",
                "text": "we will do the full banked reset today too.\n\nHappy Astra day",
                "url": "https://x.com/thsottiaux/status/2096035437299237298",
                "announcer": "thsottiaux",
            },
            now=NOW,
        )
        self.assertEqual(subject, "Codex: banked reset announced — not yet landed")
        # Pacific time with the zone, and the UTC date that differs from it.
        self.assertIn("Sep 4, 2026, 5:39 PM PDT", html_body)
        self.assertIn("00:39 UTC Sep 5", html_body)
        self.assertIn("@thsottiaux", html_body)
        # The vendor's words, verbatim, with the author's paragraph break kept.
        self.assertIn("<blockquote>", html_body)
        self.assertIn("Happy Astra day", html_body)
        self.assertEqual(html_body.count("<p>we will do the full banked reset"), 1)
        # And what our own account showed at the time it was sent.
        self.assertIn("Not landed on our Pro account", html_body)
        self.assertIn("98% used", text_body)
        self.assertIn("Not landed on our Pro account", text_body)

    def test_a_landed_forecast_does_not_contradict_its_own_subject(self):
        # The post forecast a banked reset; our probe has since recorded the
        # credit arriving. A subject saying "not yet landed" over a body saying
        # "landed at 9:20 PM" is worse than either line alone.
        self.write_health()
        (self.state / "quota_cursor.json").write_text(
            json.dumps(
                {"slots": {"codex/10080": {"last": {
                    "t": NOW - 30, "used_percent": 100.0, "credits_available": 2}}}}
            )
        )
        (self.state / "quota_events.jsonl").write_text(
            json.dumps({"kind": "credit_granted", "slot": "codex/10080",
                        "t": NOW - 1800, "credits_before": 1, "credits_after": 2}) + "\n"
        )
        subject, html_body, _text, _u, _h = self.app.notification_message(
            "one@example.com",
            {
                "vendor": "openai",
                "kind": "reset",
                "upstream_reset_type": "banked",
                "announced_at": iso(NOW - 4 * 3600),
                "text": "we will do the full banked reset today too. Lands end of day.",
                "url": "https://x.com/a/status/1",
            },
            now=NOW,
        )
        self.assertEqual(subject, "Codex: the announced banked reset landed")
        self.assertIn("Landed on our Pro account", html_body)
        self.assertNotIn("not yet landed", subject)

    def test_a_forecast_still_pending_keeps_the_not_yet_landed_subject(self):
        self.write_health()
        (self.state / "quota_cursor.json").write_text(
            json.dumps(
                {"slots": {"codex/10080": {"last": {
                    "t": NOW - 30, "used_percent": 100.0, "credits_available": 1}}}}
            )
        )
        subject, _h, _t, _u, _hd = self.app.notification_message(
            "one@example.com",
            {
                "vendor": "openai",
                "kind": "reset",
                "upstream_reset_type": "banked",
                "announced_at": iso(NOW - 600),
                "text": "we will do the full banked reset today too. Lands end of day.",
                "url": "https://x.com/a/status/1",
            },
            now=NOW,
        )
        self.assertEqual(subject, "Codex: banked reset announced — not yet landed")

    def test_an_empty_post_says_so_instead_of_shipping_a_blank_quote(self):
        _, html_body, text_body, _, _ = self.app.notification_message(
            "one@example.com",
            {
                "vendor": "openai",
                "kind": "reset",
                "announced_at": "2026-09-04T20:08:45Z",
                "text": "",
                "url": "https://x.com/a/status/1",
            },
            now=NOW,
        )
        self.assertIn("carried no text", html_body)
        self.assertIn("carried no text", text_body)
        self.assertNotIn("<blockquote><p></p></blockquote>", html_body)

    def test_an_email_with_no_probe_reading_claims_no_observation(self):
        # The exact sentence belongs to scripts/groundtruth.py and is tested
        # there; what this layer must guarantee is that the block is present,
        # that it does not claim we saw anything, and above all that it never
        # renders the one sentence a single account may never say.
        _, html_body, text_body, _, _ = self.app.notification_message(
            "one@example.com",
            {
                "vendor": "anthropic",
                "kind": "reset",
                "announced_at": "2026-09-04T20:08:45Z",
                "text": "We've just reset weekly limits for everyone on a Claude Max plan.",
                "url": "https://x.com/lydiahallie/status/2095967323412930677",
                "announcer": "lydiahallie",
            },
        )
        self.assertIn("What our own account shows", html_body)
        self.assertNotIn("Observed on", html_body)
        for forbidden in ("no reset happened", "did not happen", "everyone"):
            self.assertNotIn(forbidden, text_body.replace("everyone on a Claude Max plan", ""))

    # ─── Schema migration ────────────────────────────────────────────────────

    def test_migration_preserves_existing_deliveries(self):
        legacy = Path(self.tempdir.name) / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            CREATE TABLE subscribers (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'unsubscribed')),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                topics TEXT NOT NULL DEFAULT 'anthropic,openai,google'
            );
            CREATE TABLE known_events (
                event_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                discovered_at INTEGER NOT NULL
            );
            CREATE TABLE deliveries (
                event_key TEXT NOT NULL REFERENCES known_events(event_key),
                email TEXT NOT NULL REFERENCES subscribers(email),
                status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                resend_id TEXT,
                last_error TEXT,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (event_key, email)
            );
            INSERT INTO subscribers VALUES ('kept@example.com','active',1,1,'openai');
            INSERT INTO known_events VALUES ('openai:kept','{}',1);
            INSERT INTO deliveries VALUES ('openai:kept','kept@example.com','sent',1,'resend-1',NULL,1);
            """
        )
        conn.commit()
        conn.close()

        store = Store(legacy)
        store.init()
        with store.connect() as conn:
            rows = list(conn.execute("SELECT * FROM deliveries"))
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='deliveries'"
            ).fetchone()["sql"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "sent")
        self.assertEqual(rows[0]["resend_id"], "resend-1")
        self.assertIn("'held'", schema)
        # Idempotent: a second init must not migrate again.
        self.assertFalse(store.migrate_deliveries_held())


class IncidentKeyTests(unittest.TestCase):
    """One vendor action, one key — including before anyone has posted about it."""

    def test_an_announcement_keeps_the_key_it_has_always_had(self):
        # The 83 rows in the live database were filed under exactly this
        # string. If this changes, every one of them is re-mailable.
        self.assertEqual(
            incident_key({"vendor": "openai", "id": "2096035437299237298"}),
            "openai:2096035437299237298",
        )

    def test_a_probe_row_is_keyed_by_its_clear_bracket_not_its_payload(self):
        observation = {
            "vendor": "openai",
            "origin": ORIGIN_PROBE,
            "slot": "codex/10080",
            "observed_before": iso(NOW - 660),
            "observed_after": iso(NOW - 600),
            "observed_at": iso(NOW - 600),
            "evidence": "The weekly window went 96% to 0%.",
        }
        key = incident_key(observation)
        self.assertEqual(key, f"openai:probe-codex/10080-{NOW - 660}")
        # The export re-renders its own sentences on every run. That must not
        # read as a second reset, which a payload hash would have done.
        rephrased = {**observation, "evidence": "Cleared early; unexplained."}
        self.assertEqual(incident_key(rephrased), key)

    def test_a_fused_row_states_its_own_key_and_is_believed(self):
        self.assertEqual(
            incident_key(
                {"vendor": "openai", "id": "999", "incident_key": "openai:decided"}
            ),
            "openai:decided",
        )

    def test_origin_is_read_from_the_shape_when_it_is_not_declared(self):
        self.assertEqual(
            event_origin({"observed_at": iso(NOW), "slot": "codex/10080"}),
            ORIGIN_PROBE,
        )
        self.assertEqual(
            event_origin({"announced_at": iso(NOW), "id": "1"}), ORIGIN_ANNOUNCEMENT
        )

    def test_the_lifecycle_status_decides_whether_a_row_may_be_mailed(self):
        for status in ("announced", "confirmed", "forecast", "observed"):
            with self.subTest(status=status):
                self.assertEqual(mail_stage({"status": status}), STAGE_NEW)
        for status in (
            "candidate",
            "forecast_lapsed",
            "observed_pending",
            "observed_single",
            "historical",
            "retracted",
            "reverted_on_our_account",
        ):
            with self.subTest(status=status):
                self.assertIsNone(mail_stage({"status": status}))
        # No status is the pre-fusion feed: announcements behave as they always
        # did, an unfused own-account measurement does not become email.
        self.assertEqual(mail_stage({"announced_at": iso(NOW), "id": "1"}), STAGE_NEW)
        self.assertIsNone(mail_stage({"observed_at": iso(NOW), "slot": "s"}))

    def test_adoption_is_planned_from_either_the_merge_field_or_the_store(self):
        stored = {"openai:c1": "openai:probe-codex/10080-100"}
        known = {"openai:probe-codex/10080-100"}
        stated = [{"event_key": "openai:c1", "vendor": "openai", "id": "post",
                   "merged_from": "openai:probe-codex/10080-100"}]
        self.assertEqual(
            plan_adoptions(stated, {}, known),
            (("openai:probe-codex/10080-100", "openai:post"),),
        )
        silent = [{"event_key": "openai:c1", "vendor": "openai",
                   "incident_key": "openai:post"}]
        self.assertEqual(
            plan_adoptions(silent, stored, known),
            (("openai:probe-codex/10080-100", "openai:post"),),
        )
        # A key we have never filed anything under is not a rename.
        self.assertEqual(plan_adoptions(stated, {}, set()), ())

    def test_only_new_is_mandatory_in_the_stages_preference(self):
        self.assertEqual(normalize_stages("new,confirmed"), (STAGE_NEW, STAGE_CONFIRMED))
        self.assertEqual(normalize_stages([]), (STAGE_NEW,))
        self.assertEqual(normalize_stages("nonsense"), (STAGE_NEW,))
        self.assertEqual(normalize_stages(None), (STAGE_NEW,))

    def test_observations_are_flattened_and_secret_ones_are_dropped(self):
        events = flatten_events(
            {
                "vendors": {
                    "openai": {
                        "events": [{"id": "1", "announced_at": iso(NOW)}],
                        "observations": [
                            {
                                "event_id": "c1",
                                "observed_at": iso(NOW),
                                "slot": "codex/10080",
                                "public": True,
                            },
                            # A self-applied credit: what the OWNER did with
                            # their own account, never a vendor action.
                            {"event_id": "c2", "observed_at": iso(NOW), "public": False},
                        ],
                    }
                }
            }
        )
        self.assertEqual(
            [(row["event_key"], event_origin(row)) for row in events],
            [("openai:1", ORIGIN_ANNOUNCEMENT), ("openai:c1", ORIGIN_PROBE)],
        )

    def test_a_retraction_is_read_even_when_the_row_is_no_longer_public(self):
        # The probe marks a withdrawn clear non-public, which is right for the
        # site and wrong here: the people who were sent the reading are owed
        # the withdrawal. It can only ever reach them, so this cannot leak a
        # row that was never public in the first place.
        events = flatten_events(
            {
                "vendors": {
                    "openai": {
                        "observations": [
                            {
                                "event_id": "c1",
                                "observed_at": iso(NOW),
                                "public": False,
                                "status": "retracted",
                            }
                        ]
                    }
                }
            }
        )
        self.assertEqual([row["event_key"] for row in events], ["openai:c1"])
        self.assertIsNone(mail_stage(events[0]))


class StageAwareNotifierTests(unittest.TestCase):
    """One reset, one email — even when the probe sees it before the post."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.state = root / "state"
        self.state.mkdir()
        self.config = Config(
            api_key="test-key",
            secret="s" * 48,
            db_path=root / "subscriptions.db",
            public_url="https://resets.example.test",
            from_email="AI Reset Watch <updates@example.test>",
            data_file=root / "data.json",
            state_dir=self.state,
            owner_email="owner@example.test",
        )
        self.store = Store(self.config.db_path)
        self.store.init()
        self.mailer = FakeMailer()
        self.app = SubscriptionApp(self.config, self.store, self.mailer)
        for address in ("one@example.com", "two@example.com"):
            self.store.request_subscription(address, ("openai",))
            self.store.activate(address)
        self.write_health()
        self.write_cursor(used=96.0, credits=1)
        # The first feed a notifier ever reads is a baseline and delivers
        # nothing, so every test here starts one refresh in.
        self.store.discover_events(
            [
                {
                    "event_key": "openai:seed",
                    "vendor": "openai",
                    "id": "seed",
                    "kind": "reset",
                    "announced_at": iso(NOW - 30 * 86400),
                    "text": "Seeded so the next refresh is not a baseline.",
                }
            ],
            now=NOW,
        )

    # ─── Fixtures ────────────────────────────────────────────────────────────

    def write_health(self):
        (self.state / "probe_health.json").write_text(
            json.dumps(
                {
                    "codex": {
                        "label": "codex",
                        "updated_at": NOW - 30,
                        "last_ok_at": NOW - 30,
                        "last_sample_t": NOW - 30,
                        "consecutive_failures": 0,
                        "blind_since": None,
                        "throttled_until": None,
                        "token_stale": False,
                        "detectable_now": {"codex/10080": True},
                    }
                }
            )
        )

    def write_cursor(self, *, used, credits):
        (self.state / "quota_cursor.json").write_text(
            json.dumps(
                {
                    "slots": {
                        "codex/10080": {
                            "last": {
                                "t": NOW - 30,
                                "used_percent": used,
                                "credits_available": credits,
                            }
                        }
                    }
                }
            )
        )

    def write_probe_log(self, *records):
        (self.state / "quota_events.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )

    def write_feed(self, events=(), observations=()):
        self.config.data_file.write_text(
            json.dumps(
                {
                    "generated_at": iso(NOW - 60),
                    "vendors": {
                        "openai": {
                            "events": list(events),
                            "observations": list(observations),
                        }
                    },
                }
            )
        )

    def notify(self, now=NOW):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_notifier(self.config, self.app, now=now)
        return buffer.getvalue()

    def subscriber_mails(self):
        return [
            message
            for message in self.mailer.messages
            if message["to"] != "owner@example.test"
        ]

    def deliveries(self):
        with self.store.connect() as conn:
            return [
                (row["incident_key"], row["email"], row["stage"], row["status"], row["claim"])
                for row in conn.execute(
                    "SELECT incident_key, email, stage, status, claim FROM deliveries "
                    "ORDER BY incident_key, email, stage"
                )
            ]

    def announcement(self, post_id, text, **extra):
        row = {
            "id": post_id,
            "kind": "reset",
            "announced_at": iso(NOW - 1800),
            "text": text,
            "url": f"https://x.com/a/status/{post_id}",
            "announcer": "thsottiaux",
        }
        row.update(extra)
        return row

    def observation(self, **extra):
        row = {
            "event_id": "clear-1",
            "origin": ORIGIN_PROBE,
            "slot": "codex/10080",
            "kind": "reset",
            "verdict": "vendor_reset",
            "observed_at": iso(NOW - 2100),
            "observed_before": iso(NOW - 2160),
            "observed_after": iso(NOW - 2100),
            "public": True,
            "text": "",
            "status": "observed",
        }
        row.update(extra)
        return row

    def seen_clear(self, at):
        return {
            "kind": "clear",
            "event_id": "clear-1",
            "slot": "codex/10080",
            "detected_at": at,
            "classification": "global_candidate",
            "confirmed": True,
            "used_before": 96.0,
            "used_after": 0.0,
            "early_by_seconds": 7200,
            "credits_before": 1,
            "credits_after": 1,
        }

    # ─── One reset, one email ────────────────────────────────────────────────

    def test_probe_first_then_the_post_is_one_email_not_two(self):
        # Our own account saw the 2026-08-30 reset 2.5 minutes BEFORE
        # @thsottiaux posted it. Under the old event_key that was two keys and
        # two emails for one reset.
        probe_key = f"openai:probe-codex/10080-{NOW - 2160}"
        self.write_feed(observations=[self.observation()])
        self.write_feed(observations=[self.observation()])
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)
        self.assertEqual({key for key, _, _, _, _ in self.deliveries()}, {probe_key})

        # The vendor posts about it and the fusion layer merges the two.
        post = self.announcement("2094856679250919746", "We have reset usage limits.")
        merged = self.observation(
            incident_key="openai:2094856679250919746", merged_from=probe_key
        )
        self.write_feed(events=[post], observations=[merged])
        self.notify()

        self.assertEqual(len(self.subscriber_mails()), 2)
        self.assertEqual(
            {key for key, _, _, _, _ in self.deliveries()},
            {"openai:2094856679250919746"},
        )
        with self.store.connect() as conn:
            merged_from = {
                row["event_key"]: row["merged_from"]
                for row in conn.execute("SELECT event_key, merged_from FROM known_events")
            }
        self.assertEqual(merged_from["openai:clear-1"], probe_key)

    def test_a_probe_first_email_quotes_the_probe_instead_of_an_absent_post(self):
        # There is no post: that is the whole point of a probe-first incident.
        # The site shows the probe's own two sentences for this observation and
        # so must the email — "the source post carried no text" would be
        # telling the reader a post they were never promised is empty.
        self.write_feed(
            observations=[
                self.observation(
                    headline="Our weekly window cleared 2 hours early.",
                    evidence="The counter went 96% to 0% between two polls.",
                )
            ]
        )
        self.notify()
        body = self.subscriber_mails()[0]["html_body"]
        self.assertIn("Our weekly window cleared 2 hours early.", body)
        self.assertIn("The counter went 96% to 0% between two polls.", body)
        self.assertNotIn("carried no text", body)

    def test_a_status_upgrade_can_still_reach_the_queue(self):
        # The old dedupe was "have I seen this key?", so a row that arrived as
        # a candidate and was later verified could never be mailed at all.
        self.write_feed(events=[self.announcement("1", "Something.", status="candidate")])
        self.write_feed(
            events=[self.announcement("1", "Something.", status="candidate"),
                    self.announcement("2", "We have reset usage limits.")]
        )
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)
        self.assertEqual(
            {key for key, _, _, _, _ in self.deliveries()}, {"openai:2"}
        )

    # ─── Confirmed ───────────────────────────────────────────────────────────

    def test_a_forecast_gets_exactly_one_confirmation_once_it_lands(self):
        forecast = self.announcement(
            "2096035437299237298",
            "we will do the full banked reset today too. Lands end of day.",
            upstream_reset_type="banked",
        )
        self.write_feed(events=[forecast])
        self.write_feed(events=[forecast])
        self.notify()
        first = self.subscriber_mails()
        self.assertEqual(len(first), 2)
        self.assertTrue(all("not yet landed" in m["subject"] for m in first))
        self.assertTrue(
            all(claim == CLAIM_PENDING for *_, claim in self.deliveries())
        )

        # Nothing has landed yet, so a second tick writes to nobody.
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)

        # The bank rises: the forecast thing happened on our own account.
        self.write_cursor(used=100.0, credits=2)
        self.write_probe_log(
            {
                "kind": "credit_granted",
                "slot": "codex/10080",
                "t": NOW - 900,
                "credits_before": 1,
                "credits_after": 2,
            }
        )
        self.notify()
        follow_ups = [
            m
            for m in self.subscriber_mails()
            if "the announced banked reset landed" in m["subject"]
        ]
        self.assertEqual(len(follow_ups), 2)
        self.assertIn("single follow-up", follow_ups[0]["text_body"])
        self.assertIn("Landed on our Pro account", follow_ups[0]["html_body"])

        # At most one, forever.
        self.notify()
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 4)
        stages = sorted(stage for _, _, stage, _, _ in self.deliveries())
        self.assertEqual(stages, [STAGE_CONFIRMED] * 2 + [STAGE_NEW] * 2)

    def test_a_retrospective_post_we_then_observe_is_confirmed_once(self):
        post = self.announcement("2090766694897619318", "We have reset usage limits.")
        self.write_feed(events=[post])
        self.write_feed(events=[post])
        self.notify()
        self.assertIn("Not observed on our Pro account", self.subscriber_mails()[0]["html_body"])

        self.write_probe_log(self.seen_clear(NOW - 1500))
        self.write_cursor(used=0.0, credits=1)
        self.notify()
        follow_ups = [m for m in self.subscriber_mails() if "Confirmed" in m["html_body"]]
        self.assertEqual(len(follow_ups), 2)
        self.assertIn("Observed on our Pro account", follow_ups[0]["html_body"])

    def test_a_mail_that_already_said_observed_owes_no_follow_up(self):
        # Nothing is promised, so nothing is owed: the claim is settled at send
        # time and the row can never become a confirmation candidate.
        self.write_probe_log(self.seen_clear(NOW - 1500))
        self.write_cursor(used=0.0, credits=1)
        post = self.announcement("2090766694897619318", "We have reset usage limits.")
        self.write_feed(events=[post])
        self.notify()
        self.write_feed(events=[post])
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)
        self.assertTrue(all(claim == CLAIM_SETTLED for *_, claim in self.deliveries()))
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)

    # ─── Retracted ───────────────────────────────────────────────────────────

    def test_a_retraction_reaches_the_people_who_got_the_first_email_only(self):
        self.write_feed(observations=[self.observation()])
        self.write_feed(observations=[self.observation()])
        self.notify()
        self.assertEqual({m["to"] for m in self.subscriber_mails()},
                         {"one@example.com", "two@example.com"})

        # A third subscriber arrives AFTER the observation was mailed. A
        # correction is addressed to the people who read the thing being
        # corrected, and to nobody else.
        self.store.request_subscription("three@example.com", ("openai",))
        self.store.activate("three@example.com")

        self.write_feed(observations=[self.observation(status="retracted")])
        self.notify()
        retractions = [
            m for m in self.subscriber_mails() if "withdrawing" in m["subject"]
        ]
        self.assertEqual(len(retractions), 2)
        self.assertEqual(
            {m["to"] for m in retractions}, {"one@example.com", "two@example.com"}
        )
        self.assertIn("taken it back", retractions[0]["text_body"])
        # Never a claim about the vendor: this withdraws OUR measurement.
        self.assertNotIn("vendor was wrong", retractions[0]["html_body"])

        # And exactly one, however many ticks run.
        self.notify()
        self.assertEqual(
            len([m for m in self.subscriber_mails() if "withdrawing" in m["subject"]]), 2
        )

    def test_a_probe_retraction_never_corrects_a_vendors_own_announcement(self):
        probe_key = f"openai:probe-codex/10080-{NOW - 2160}"
        post = self.announcement("2094856679250919746", "We have reset usage limits.")
        merged = self.observation(
            incident_key="openai:2094856679250919746", merged_from=probe_key
        )
        self.write_feed(events=[post], observations=[merged])
        self.write_feed(events=[post], observations=[merged])
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)

        # Our own account gives it back. The vendor's post still stands, so
        # this is a fact about this machine: an owner alert, not a correction.
        self.write_feed(
            events=[post],
            observations=[merged | {"status": "retracted"}],
        )
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)
        owner = [m for m in self.mailer.messages if m["to"] == "owner@example.test"]
        self.assertTrue(
            any("incident:reverted:openai:2094856679250919746" in m["subject"] for m in owner),
            [m["subject"] for m in owner],
        )

    def test_a_reversion_status_is_an_owner_alert_and_nothing_else(self):
        post = self.announcement("1", "We have reset usage limits.")
        self.write_feed(events=[post])
        self.write_feed(events=[post | {"status": "reverted_on_our_account"}])
        self.notify()
        self.assertEqual(self.subscriber_mails(), [])
        owner = [m["subject"] for m in self.mailer.messages if m["to"] == "owner@example.test"]
        self.assertTrue(any("incident:reverted:openai:1" in s for s in owner), owner)

    # ─── The stages preference ───────────────────────────────────────────────

    def test_a_subscriber_can_decline_follow_ups_but_not_the_first_email(self):
        self.store.update_topics("two@example.com", ("openai",), (STAGE_NEW,))
        self.assertEqual(
            self.store.subscriber_preferences("two@example.com"),
            (("openai",), (STAGE_NEW,)),
        )
        forecast = self.announcement(
            "2096035437299237298",
            "we will do the full banked reset today too. Lands end of day.",
            upstream_reset_type="banked",
        )
        self.write_feed(events=[forecast])
        self.write_feed(events=[forecast])
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)  # both get the first mail

        self.write_cursor(used=100.0, credits=2)
        self.write_probe_log(
            {"kind": "credit_granted", "slot": "codex/10080", "t": NOW - 900,
             "credits_before": 1, "credits_after": 2}
        )
        self.notify()
        follow_ups = [
            m
            for m in self.subscriber_mails()
            if "the announced banked reset landed" in m["subject"]
        ]
        self.assertEqual([m["to"] for m in follow_ups], ["one@example.com"])

    def test_the_preferences_page_offers_the_follow_up_choices(self):
        token = self.app.make_token("one@example.com", "preferences", 3600)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = (
            f"http://127.0.0.1:{server.server_port}/api/preferences?"
            + urllib.parse.urlencode({"token": token})
        )
        with urllib.request.urlopen(url) as response:
            page = response.read().decode()
        self.assertIn('name="stages" value="confirmed"', page)
        self.assertIn('name="stages" value="retracted"', page)

        request = urllib.request.Request(
            url + "&redirect=1",
            data=urllib.parse.urlencode(
                [("topics", "openai"), ("stages", "retracted")]
            ).encode(),
            method="POST",
        )
        # The handler answers a saved form with a 303 to the public site,
        # which does not resolve from a test process.
        with contextlib.suppress(urllib.error.HTTPError, urllib.error.URLError):
            urllib.request.urlopen(request)
        self.assertEqual(
            self.store.subscriber_preferences("one@example.com"),
            (("openai",), (STAGE_NEW, STAGE_RETRACTED)),
        )

    # ─── Failure and retry ───────────────────────────────────────────────────

    def test_a_send_that_fails_is_retried_and_never_silently_dropped(self):
        post = self.announcement("1", "We have reset usage limits.")
        self.write_feed(events=[post])
        self.mailer.fail_next = 2
        self.notify()
        self.assertEqual(self.subscriber_mails(), [])
        self.assertEqual(
            [(status, claim) for *_, status, claim in self.deliveries()],
            [("failed", CLAIM_NONE)] * 2,
        )
        # The event is already known, so only the delivery row can bring it
        # back — which is exactly what the retry is for.
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 2)
        self.assertEqual(
            {status for *_, status, _ in self.deliveries()}, {"sent"}
        )

    # ─── The dry run ─────────────────────────────────────────────────────────

    def test_the_dry_run_names_every_bucket_and_writes_nothing(self):
        undated = self.announcement("undated", "We have reset usage limits.")
        del undated["announced_at"]
        self.write_feed(
            events=[
                self.announcement("fresh", "We have reset usage limits."),
                self.announcement(
                    "old", "We have reset usage limits.",
                    announced_at=iso(NOW - 72 * 3600),
                ),
                self.announcement("hint", "Something.", status="candidate"),
                undated,
            ]
        )
        before = self.store.known_event_keys()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_notifier(self.config, self.app, dry_run=True, now=NOW)
        output = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertEqual(self.mailer.messages, [])
        self.assertEqual(self.store.known_event_keys(), before)
        self.assertEqual(self.store.pending_deliveries(), [])
        self.assertIn("would send to 2", output)
        self.assertIn("historical, not queued: openai:old", output)
        self.assertIn("undatable, not queued: openai:undated", output)
        self.assertIn("status candidate, not queued: openai:hint", output)

    def test_the_dry_run_previews_the_follow_ups_and_the_rename(self):
        probe_key = f"openai:probe-codex/10080-{NOW - 2160}"
        forecast = self.announcement(
            "2096035437299237298",
            "we will do the full banked reset today too. Lands end of day.",
            upstream_reset_type="banked",
        )
        self.write_feed(events=[forecast], observations=[self.observation()])
        self.notify()
        self.assertEqual(len(self.subscriber_mails()), 4)

        # The forecast has landed, the observation has been withdrawn, and the
        # fusion layer has merged the observation into a post.
        self.write_cursor(used=100.0, credits=2)
        self.write_probe_log(
            {"kind": "credit_granted", "slot": "codex/10080", "t": NOW - 900,
             "credits_before": 1, "credits_after": 2}
        )
        self.write_feed(
            events=[forecast, self.announcement("later", "We have reset usage limits.")],
            observations=[
                self.observation(status="retracted"),
                self.observation(
                    event_id="clear-2",
                    incident_key="openai:later",
                    merged_from=probe_key,
                ),
            ],
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_notifier(self.config, self.app, dry_run=True, now=NOW)
        output = buffer.getvalue()
        self.assertIn(f"would adopt {probe_key} -> openai:later", output)
        self.assertIn("would confirm to one@example.com", output)
        self.assertIn("would retract to one@example.com", output)
        # Still a preview: nothing sent, nothing queued, nothing renamed.
        self.assertEqual(len(self.subscriber_mails()), 4)
        self.assertIn(probe_key, self.store.known_incident_keys())

    def test_releasing_a_held_batch_closes_the_condition_it_opened(self):
        batch = [
            self.announcement(str(index), "We have reset usage limits.")
            for index in range(FLOOD_CAP + 1)
        ]
        self.write_feed(events=batch)
        self.notify()
        self.assertEqual(self.subscriber_mails(), [])
        self.assertIn("deliveries:flood-held", self.store.open_alert_conditions())
        self.assertEqual(len(self.store.held_deliveries()), (FLOOD_CAP + 1) * 2)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            run_notifier(self.config, self.app, release=True, now=NOW)
        self.assertIn("released", buffer.getvalue())
        self.assertEqual(len(self.subscriber_mails()), (FLOOD_CAP + 1) * 2)
        self.assertNotIn("deliveries:flood-held", self.store.open_alert_conditions())

    # ─── Idempotency ─────────────────────────────────────────────────────────

    def test_the_resend_key_for_a_new_mail_is_the_one_it_has_always_been(self):
        # The 83 sent rows were sent under opaque_bucket("notify",
        # "<event_key>:<email>"), and an announcement's incident key IS that
        # event key. A follow-up must not collide with the mail it follows.
        legacy = self.app.opaque_bucket("notify", "openai:123:one@example.com")
        self.assertEqual(
            self.app.delivery_idempotency_key("openai:123", "one@example.com"),
            legacy,
        )
        for stage in (STAGE_CONFIRMED, STAGE_RETRACTED):
            with self.subTest(stage=stage):
                self.assertNotEqual(
                    self.app.delivery_idempotency_key("openai:123", "one@example.com", stage),
                    legacy,
                )


class OwnerAlertUnitTests(unittest.TestCase):
    """The OnFailure= target, under a restart loop."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.config = Config(
            api_key="test-key",
            secret="s" * 48,
            db_path=root / "subscriptions.db",
            public_url="https://resets.example.test",
            from_email="AI Reset Watch <updates@example.test>",
            data_file=root / "data.json",
            state_dir=root / "state",
            owner_email="owner@example.test",
        )
        self.store = Store(self.config.db_path)
        self.store.init()
        self.mailer = FakeMailer()
        self.app = SubscriptionApp(self.config, self.store, self.mailer)

    def trigger(self, now, **kwargs):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_unit_alert(
                self.app, "ai-resets-probe.service", now=now, **kwargs
            )
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_a_restart_loop_is_one_email_not_one_per_cycle(self):
        # ai-resets-probe.service is Restart=always, RestartSec=10, and
        # OnFailure= fires on every cycle: 8,640 triggers a day.
        self.trigger(NOW)
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertIn("unit:ai-resets-probe.service:failed", self.mailer.messages[0]["subject"])
        for cycle in range(1, 60):
            self.trigger(NOW + cycle * 10)
        self.assertEqual(len(self.mailer.messages), 1)

    def test_a_failure_that_is_still_going_reminds_the_owner_slowly(self):
        # One mail per outage forever would mean a fresh failure next week
        # never pages at all.
        self.trigger(NOW)
        self.trigger(NOW + UNIT_ALERT_REPEAT_SECONDS - 60)
        self.assertEqual(len(self.mailer.messages), 1)
        self.trigger(NOW + UNIT_ALERT_REPEAT_SECONDS + 60)
        self.assertEqual(len(self.mailer.messages), 2)

    def test_recovery_closes_the_condition_and_says_so_once(self):
        self.trigger(NOW)
        self.trigger(NOW + 3600, clear=True)
        self.assertEqual(len(self.mailer.messages), 2)
        self.assertIn("recovered", self.mailer.messages[1]["subject"])
        self.assertEqual(self.store.open_alert_conditions(), set())
        # A second clear has nothing to say.
        self.trigger(NOW + 3700, clear=True)
        self.assertEqual(len(self.mailer.messages), 2)
        # And the next failure pages again.
        self.trigger(NOW + 7200)
        self.assertEqual(len(self.mailer.messages), 3)

    def test_a_missing_owner_address_is_loud_and_still_exits_zero(self):
        app = SubscriptionApp(
            Config(**{**self.config.__dict__, "owner_email": ""}),
            self.store,
            self.mailer,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_unit_alert(app, "ai-resets-probe.service", now=NOW)
        self.assertEqual(code, 0)
        self.assertIn("no OWNER_EMAIL set", buffer.getvalue())
        self.assertEqual(self.mailer.messages, [])

    def test_a_held_flood_batch_is_never_reported_as_recovered_on_its_own(self):
        # dispatch_owner_alerts used to close every condition it did not
        # recompute, so a flood hold mailed "recovered" five minutes later
        # while the batch was still sitting unsent.
        self.store.open_alert("deliveries:flood-held", "4 events arrived at once.")
        self.config.data_file.write_text(
            json.dumps({"generated_at": iso(NOW - 60), "vendors": {}})
        )
        (self.config.state_dir).mkdir(exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            run_notifier(self.config, self.app, now=NOW)
        self.assertIn("deliveries:flood-held", self.store.open_alert_conditions())
        self.assertEqual(
            [m for m in self.mailer.messages if "recovered" in m["subject"]], []
        )
        self.assertEqual(self.store.release_held(now=NOW), 0)


class CommandLineTests(unittest.TestCase):
    """The wiring: a subcommand nothing calls is a feature that does not exist."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.db = root / "subscriptions.db"
        self.env = {
            "SUBSCRIPTION_DB": str(self.db),
            "RESEND_API_KEY": "test-key",
            "SUBSCRIPTION_SECRET": "s" * 48,
            "AI_RESETS_DATA": str(root / "data.json"),
            "AI_RESETS_STATE": str(root / "state"),
            "OWNER_EMAIL": "",
        }

    def run_main(self, *argv):
        buffer = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env, clear=False):
            with unittest.mock.patch.object(sys, "argv", ["subscriptions.py", *argv]):
                with contextlib.redirect_stdout(buffer):
                    code = subscriptions_main()
        return code, buffer.getvalue()

    def test_init_db_creates_the_schema_the_notifier_expects(self):
        code, output = self.run_main("init-db")
        self.assertEqual(code, 0)
        self.assertIn("initialized", output)
        with sqlite3.connect(self.db) as conn:
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='deliveries'"
            ).fetchone()[0]
        self.assertIn("incident_key", schema)
        self.assertIn("stage", schema)

    def test_the_alert_subcommand_is_reachable_from_the_unit_file(self):
        # infra/ai-resets-alert@.service runs exactly this. If the subcommand
        # is not wired, the OnFailure= target fails on every restart cycle.
        code, output = self.run_main("alert", "--unit", "ai-resets-probe.service")
        self.assertEqual(code, 0)
        self.assertIn("unit:ai-resets-probe.service:failed", output)
        self.assertIn("no OWNER_EMAIL set", output)

    def test_the_alert_subcommand_refuses_to_run_without_a_unit(self):
        with self.assertRaises(SystemExit):
            self.run_main("alert")


class MigrationTests(unittest.TestCase):
    """The 83 rows in the live database are the record of what real people got."""

    def legacy_database(self, path, *, deliveries=83):
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE subscribers (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'unsubscribed')),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                confirmed_at INTEGER,
                unsubscribed_at INTEGER,
                last_confirmation_sent_at INTEGER,
                topics TEXT NOT NULL DEFAULT 'anthropic,openai,google',
                consent_version TEXT,
                signup_source TEXT,
                confirmation_token_hash TEXT,
                suppressed_at INTEGER,
                suppression_reason TEXT
            );
            CREATE TABLE known_events (
                event_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                discovered_at INTEGER NOT NULL
            );
            CREATE TABLE "deliveries" (
                event_key TEXT NOT NULL REFERENCES known_events(event_key),
                email TEXT NOT NULL REFERENCES subscribers(email),
                status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held')),
                attempts INTEGER NOT NULL DEFAULT 0,
                resend_id TEXT,
                last_error TEXT,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (event_key, email)
            );
            """
        )
        emails = [f"subscriber{index}@example.com" for index in range(7)]
        for email in emails:
            conn.execute(
                "INSERT INTO subscribers(email, status, created_at, updated_at, topics)"
                " VALUES (?, 'active', 1, 1, 'anthropic,openai,google')",
                (email,),
            )
        written = 0
        post = 2082317452755751098
        while written < deliveries:
            key = f"openai:{post}"
            conn.execute(
                "INSERT INTO known_events VALUES (?, ?, 1)",
                (key, json.dumps({"vendor": "openai", "id": str(post)})),
            )
            for email in emails:
                if written >= deliveries:
                    break
                conn.execute(
                    "INSERT INTO deliveries VALUES (?, ?, 'sent', 1, ?, NULL, 1)",
                    (key, email, f"resend-{written}"),
                )
                written += 1
            post += 1
        conn.commit()
        conn.close()

    def test_the_eighty_three_sent_rows_survive_the_re_keying(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "legacy.db"
        self.legacy_database(path)
        store = Store(path)
        store.init()

        with store.connect() as conn:
            rows = list(conn.execute("SELECT * FROM deliveries"))
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='deliveries'"
            ).fetchone()["sql"]
            orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(len(rows), 83)
        self.assertEqual({row["status"] for row in rows}, {"sent"})
        self.assertEqual({row["stage"] for row in rows}, {STAGE_NEW})
        self.assertEqual(len({row["resend_id"] for row in rows}), 83)
        self.assertEqual(orphans, [])
        self.assertIn("incident_key", schema)
        # Every live row was filed under "<vendor>:<id>", which is exactly the
        # incident key derived for it. Nothing is re-mailable.
        self.assertTrue(all(row["incident_key"].startswith("openai:") for row in rows))
        with store.connect() as conn:
            mismatched = conn.execute(
                "SELECT COUNT(*) FROM known_events WHERE incident_key <> event_key"
            ).fetchone()[0]
        self.assertEqual(mismatched, 0)

        # Idempotent: a second init migrates nothing.
        self.assertFalse(store.migrate_deliveries_incidents())
        self.assertFalse(store.migrate_known_events_incidents())
        self.assertFalse(store.migrate_deliveries_held())

    def test_the_migration_cannot_make_old_mail_sprout_confirmations(self):
        # 83 people were written to before follow-ups existed. Their claim is
        # empty, so no confirmation is ever owed for any of them.
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "legacy.db"
        self.legacy_database(path)
        store = Store(path)
        store.init()
        with store.connect() as conn:
            claims = {row["claim"] for row in conn.execute("SELECT claim FROM deliveries")}
        self.assertEqual(claims, {CLAIM_NONE})
        self.assertEqual(store.confirmation_candidates(), [])

    def test_a_pre_held_database_migrates_through_both_steps(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "ancient.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE subscribers (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'unsubscribed')),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                topics TEXT NOT NULL DEFAULT 'anthropic,openai,google'
            );
            CREATE TABLE known_events (
                event_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                discovered_at INTEGER NOT NULL
            );
            CREATE TABLE deliveries (
                event_key TEXT NOT NULL REFERENCES known_events(event_key),
                email TEXT NOT NULL REFERENCES subscribers(email),
                status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                resend_id TEXT,
                last_error TEXT,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (event_key, email)
            );
            INSERT INTO subscribers VALUES ('kept@example.com','active',1,1,'openai');
            INSERT INTO known_events VALUES ('openai:kept','{}',1);
            INSERT INTO deliveries VALUES ('openai:kept','kept@example.com','sent',1,'resend-1',NULL,1);
            """
        )
        conn.commit()
        conn.close()

        store = Store(path)
        store.init()
        with store.connect() as conn:
            row = conn.execute("SELECT * FROM deliveries").fetchone()
        self.assertEqual(row["incident_key"], "openai:kept")
        self.assertEqual(row["stage"], STAGE_NEW)
        self.assertEqual(row["resend_id"], "resend-1")


if __name__ == "__main__":
    unittest.main()
