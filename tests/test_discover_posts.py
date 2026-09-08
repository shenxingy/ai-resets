"""Tests for the hint channels (P5).

The Bluesky fixtures are trimmed captures of live `getAuthorFeed` responses
taken 2026-09-06 — the records are byte-for-byte upstream, only the number of
them is reduced. `bsky_claudedevs.json` deliberately contains the post this
whole phase exists for: 2026-09-01T18:35:27Z, "With Fable 5.1 out today, we've
also reset 5-hour and weekly limits for all users.", which no channel this
project had caught.

The RFC822 fixture below is NOT a capture. The owner has not yet forwarded a
real X notification (an open item in docs/robustness-plan.md), so it is a
reconstruction, and the tests are written to pin down what the parser must
never do — invent an id, trust a stranger's envelope, leak a password — rather
than to bless one layout as the truth.
"""

import contextlib
import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional

from scripts import discover_posts
from scripts.discover_posts import (
    Hint,
    Mirror,
    SecretInHintError,
    attach_post_ids,
    collect_imap_hints,
    collect_mirror,
    hints_from_feed,
    hints_path,
    hints_payload,
    imap_config,
    is_skippable,
    merge_hints,
    parse_x_notification,
    read_secret,
    run,
    write_hints,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOW = datetime(2026, 9, 6, 22, 0, 0, tzinfo=timezone.utc)

CLAUDEDEVS = discover_posts.MIRRORS[0]
THSOTTIAUX = discover_posts.MIRRORS[1]
TRQ212 = discover_posts.MIRRORS[2]
BCHERNY = discover_posts.MIRRORS[3]

# The two missed Claude resets, as measured. The first is the one the mirror
# carried and claude-resets.com does not.
SEP1_TEXT = (
    "With Fable 5.1 out today, we've also reset 5-hour and weekly limits for all users."
)
SEP1_CREATED_AT = "2026-09-01T18:35:27.000Z"
SEP1_POST_ID = "2094856679250919746"
SEP1_URI = "bsky:at://did:plc:jhrkugv2rlyvx5awfqdiwwiv/app.bsky.feed.post/3au5by5g2o622"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def feed_of(mirror: Mirror) -> Any:
    return load_fixture(mirror.fixture)


# A reconstruction, not a capture — see the module docstring. Quoted-printable
# with a soft line break is used on purpose: it is what real notification mail
# does to a long sentence, and a parser that reads the raw bytes would split
# the post text in half.
X_NOTIFICATION = b"""\
From: X <info@x.com>
To: owner@example.invalid
Subject: =?UTF-8?Q?ClaudeDevs_=28=40ClaudeDevs=29_posted?=
Date: Tue, 01 Sep 2026 18:37:11 +0000
Message-ID: <9f2c1a@x.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b1"

--b1
Content-Type: text/plain; charset="UTF-8"
Content-Transfer-Encoding: quoted-printable

ClaudeDevs (@ClaudeDevs)

With Fable 5.1 out today, we've also reset 5-hour and weekly limits for all=
 users.

https://x.com/ClaudeDevs/status/2094856679250919746

View post
Unsubscribe from these emails
Sent by X Corp.

--b1
Content-Type: text/html; charset="UTF-8"
Content-Transfer-Encoding: 7bit

<html><body><p>HTML RENDERING SHOULD NOT WIN</p></body></html>

--b1--
"""


class BlueskySkipRules(unittest.TestCase):
    """Replies, reposts and retweets are not the announcer announcing."""

    def test_replies_are_skipped(self) -> None:
        payload = feed_of(CLAUDEDEVS)
        replies = [
            item
            for item in payload["feed"]
            if "reply" in item["post"]["record"] or "reply" in item
        ]
        self.assertTrue(replies, "fixture must contain replies to be a test")
        hints, skipped = hints_from_feed(payload, CLAUDEDEVS, now=NOW)
        self.assertEqual(skipped, len(replies))
        reply_uris = {f"bsky:{item['post']['uri']}" for item in replies}
        self.assertFalse(reply_uris & {hint.uri for hint in hints})

    def test_reposts_are_skipped(self) -> None:
        payload = feed_of(TRQ212)
        reposts = [item for item in payload["feed"] if item.get("reason")]
        self.assertTrue(reposts, "fixture must contain reposts to be a test")
        hints, _ = hints_from_feed(payload, TRQ212, now=NOW)
        for hint in hints:
            self.assertFalse(hint.text.startswith("RT @"))
        repost_uris = {f"bsky:{item['post']['uri']}" for item in reposts}
        self.assertFalse(repost_uris & {hint.uri for hint in hints})

    def test_retweet_text_is_skipped_even_without_a_reason(self) -> None:
        # Measured 2026-09-06: all 5 reposts across 120 records carried BOTH
        # signals. This pins the fallback, so a mirror that stops setting
        # `reason` cannot make @bcherny the author of @AnthropicAI's words.
        item = next(item for item in feed_of(BCHERNY)["feed"] if item.get("reason"))
        self.assertTrue(item["post"]["record"]["text"].startswith("RT @"))
        stripped = {k: v for k, v in item.items() if k != "reason"}
        stripped["post"] = dict(stripped["post"])
        stripped["post"]["author"] = dict(stripped["post"]["author"])
        stripped["post"]["author"]["handle"] = BCHERNY.handle
        self.assertEqual(is_skippable(stripped, BCHERNY), "retweet")

    def test_pinned_post_is_kept(self) -> None:
        # A pin is the author's own post. Skipping every `reason` would drop it.
        item = next(
            item
            for item in feed_of(CLAUDEDEVS)["feed"]
            if item["post"]["record"].get("createdAt") == SEP1_CREATED_AT
        )
        pinned = dict(item)
        pinned["reason"] = {"$type": "app.bsky.feed.defs#reasonPin"}
        self.assertIsNone(is_skippable(pinned, CLAUDEDEVS))

    def test_empty_and_malformed_items_are_skipped_not_raised(self) -> None:
        payload = {"feed": [{}, {"post": {}}, {"post": {"record": {"text": "  "}}}]}
        hints, skipped = hints_from_feed(payload, CLAUDEDEVS, now=NOW)
        self.assertEqual(hints, [])
        self.assertEqual(skipped, 3)


class BlueskyHintContent(unittest.TestCase):
    """What a hint carries, and what it must never carry."""

    def test_verbatim_text_and_original_time_survive(self) -> None:
        hints, _ = hints_from_feed(feed_of(CLAUDEDEVS), CLAUDEDEVS, now=NOW)
        sep1 = [hint for hint in hints if hint.posted_at == SEP1_CREATED_AT]
        self.assertEqual(len(sep1), 1)
        hint = sep1[0]
        self.assertEqual(hint.text, SEP1_TEXT)  # exact, no strip, no rewrite
        self.assertEqual(hint.posted_at, SEP1_CREATED_AT)  # X's clock, not ours
        self.assertEqual(hint.observed_at, "2026-09-06T22:00:00Z")  # ours
        self.assertEqual(hint.announcer, "@ClaudeDevs")
        self.assertEqual(hint.vendor, "anthropic")
        self.assertEqual(hint.uri, SEP1_URI)
        self.assertEqual(hint.source, "bsky")

    def test_no_post_id_is_invented(self) -> None:
        # The mirrors genuinely carry no X id: the assertion below is on the
        # raw fixtures, so if a mirror ever starts publishing ids this test
        # fails and the promotion path can be revisited.
        for mirror in discover_posts.MIRRORS:
            raw = (FIXTURES / f"{mirror.fixture}.json").read_text(encoding="utf-8")
            self.assertIsNone(
                discover_posts.X_STATUS_URL_RE.search(raw),
                f"{mirror.fixture} unexpectedly contains a status URL",
            )
            hints, _ = hints_from_feed(json.loads(raw), mirror, now=NOW)
            for hint in hints:
                self.assertIsNone(hint.post_id)

    def test_hint_json_carries_the_six_agreed_keys(self) -> None:
        hints, _ = hints_from_feed(feed_of(THSOTTIAUX), THSOTTIAUX, now=NOW)
        row = hints[0].as_json()
        for key in ("source", "observed_at", "announcer", "text", "post_id", "uri"):
            self.assertIn(key, row)
        self.assertIsNone(row["post_id"])

    def test_duplicate_uri_yields_one_hint(self) -> None:
        payload = feed_of(CLAUDEDEVS)
        item = next(
            item
            for item in payload["feed"]
            if item["post"]["record"].get("createdAt") == SEP1_CREATED_AT
        )
        doubled = {"feed": [item, dict(item)]}
        hints, _ = hints_from_feed(doubled, CLAUDEDEVS, now=NOW)
        self.assertEqual(len(hints), 1)

    def test_merge_keeps_first_sighting_and_adopts_a_later_id(self) -> None:
        first = Hint(
            source="bsky",
            observed_at="2026-09-01T20:10:00Z",
            announcer="@ClaudeDevs",
            text=SEP1_TEXT,
            post_id=None,
            uri=SEP1_URI,
            posted_at=SEP1_CREATED_AT,
        )
        later = discover_posts.replace(
            first, observed_at="2026-09-06T22:00:00Z", post_id=SEP1_POST_ID
        )
        merged = merge_hints([first], [later])
        self.assertEqual(len(merged), 1)
        # When we could FIRST have known is the number the Sep-1 miss needs.
        self.assertEqual(merged[0].observed_at, "2026-09-01T20:10:00Z")
        self.assertEqual(merged[0].post_id, SEP1_POST_ID)

    def test_merge_is_capped(self) -> None:
        many = [
            Hint(
                source="bsky",
                observed_at="2026-09-06T22:00:00Z",
                announcer="@ClaudeDevs",
                text=f"post {index}",
                post_id=None,
                uri=f"bsky:at://x/{index:04d}",
                posted_at=f"2026-08-{(index % 28) + 1:02d}T00:00:00Z",
            )
            for index in range(discover_posts.MAX_HINTS_PER_SOURCE + 25)
        ]
        self.assertEqual(len(merge_hints([], many)), discover_posts.MAX_HINTS_PER_SOURCE)


class DeadMirrors(unittest.TestCase):
    """A mirror is a takedown target; it must degrade, never raise."""

    def _http_error(self, code: int) -> urllib.error.HTTPError:
        body = (FIXTURES / "bsky_error_profile_not_found.json").read_bytes()
        return urllib.error.HTTPError(
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
            code,
            "Bad Request",
            {},  # type: ignore[arg-type]
            io.BytesIO(body),
        )

    def test_deleted_mirror_is_logged_not_raised(self) -> None:
        def fetcher(_: Mirror) -> Any:
            raise self._http_error(400)

        report = collect_mirror(CLAUDEDEVS, now=NOW, fetcher=fetcher)
        self.assertFalse(report.ok)
        self.assertTrue(report.dead)
        self.assertIn("HTTP 400", report.note)
        self.assertIn("Profile not found", report.note)
        self.assertEqual(report.hints, ())

    def test_network_failure_is_logged_not_raised(self) -> None:
        def fetcher(_: Mirror) -> Any:
            raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")

        report = collect_mirror(THSOTTIAUX, now=NOW, fetcher=fetcher)
        self.assertFalse(report.ok)
        self.assertTrue(report.dead)
        self.assertIn("unreachable", report.note)

    def test_unreadable_payload_is_logged_not_raised(self) -> None:
        report = collect_mirror(TRQ212, now=NOW, fetcher=lambda _: {"error": "nope"})
        self.assertFalse(report.ok)
        self.assertTrue(report.dead)
        self.assertIn("unreadable", report.note)

    def test_silence_past_24h_is_dead(self) -> None:
        payload = feed_of(CLAUDEDEVS)
        newest = discover_posts.newest_record_at(payload)
        assert newest is not None
        late = newest + timedelta(hours=discover_posts.MIRROR_SILENT_HOURS, minutes=30)
        report = collect_mirror(CLAUDEDEVS, now=late, fetcher=lambda _: payload)
        self.assertTrue(report.ok)  # the fetch worked; the channel still says nothing
        self.assertTrue(report.dead)
        self.assertIn("SILENT: nothing since", report.note)
        self.assertEqual(report.silent_hours, 24.5)
        self.assertTrue(report.hints)  # what it did carry is still usable

    def test_a_reply_only_mirror_is_alive(self) -> None:
        # The liveness clock counts every record. Judging it on hints alone
        # called three reachable mirrors dead on the first live dry run,
        # because their announcers had only replied for three days.
        payload = feed_of(CLAUDEDEVS)
        replies = [
            item
            for item in payload["feed"]
            if "reply" in item["post"]["record"] or "reply" in item
        ]
        reply_times = [
            discover_posts.parse_iso(item["post"]["record"]["createdAt"])
            for item in replies
        ]
        newest_reply = max(stamp for stamp in reply_times if stamp is not None)
        report = collect_mirror(
            CLAUDEDEVS,
            now=newest_reply + timedelta(hours=1),
            fetcher=lambda _: {"feed": replies},
        )
        self.assertFalse(report.dead)
        self.assertEqual(report.hints, ())  # alive, but nothing to hint about

    def test_fresh_mirror_is_alive(self) -> None:
        payload = feed_of(CLAUDEDEVS)
        newest = discover_posts.newest_record_at(payload)
        assert newest is not None
        report = collect_mirror(
            CLAUDEDEVS, now=newest + timedelta(hours=1), fetcher=lambda _: payload
        )
        self.assertFalse(report.dead)
        self.assertIn("hint(s)", report.note)
        self.assertIsNotNone(report.lag_minutes)

    def test_empty_feed_is_dead(self) -> None:
        report = collect_mirror(BCHERNY, now=NOW, fetcher=lambda _: {"feed": []})
        self.assertTrue(report.dead)
        self.assertIn("not one datable record", report.note)

    def test_measured_mirror_lag_is_reported(self) -> None:
        # Sanity on the number the owner would use to decide whether this
        # channel is fast enough: the Sep-1 reset post was mirrored 89.9 min
        # after it went up (createdAt 18:35:27Z, indexedAt 20:05:19Z).
        item = next(
            item
            for item in feed_of(CLAUDEDEVS)["feed"]
            if item["post"]["record"].get("createdAt") == SEP1_CREATED_AT
        )
        lag = discover_posts.mirror_lag_minutes({"feed": [item]})
        assert lag is not None
        self.assertAlmostEqual(lag, 89.9, places=1)


class PostIdPromotion(unittest.TestCase):
    """A hint is promoted only on announcer AND time. Never on a guess."""

    def _sep1_hint(self) -> Hint:
        hints, _ = hints_from_feed(feed_of(CLAUDEDEVS), CLAUDEDEVS, now=NOW)
        return next(hint for hint in hints if hint.posted_at == SEP1_CREATED_AT)

    def test_promoted_when_a_tracker_supplies_the_id(self) -> None:
        known = [
            {
                "id": SEP1_POST_ID,
                "screen_name": "ClaudeDevs",  # no @, different case: still a match
                "created_at": "2026-09-01T18:35:27Z",
            }
        ]
        promoted = attach_post_ids([self._sep1_hint()], known)
        self.assertEqual(promoted[0].post_id, SEP1_POST_ID)
        self.assertEqual(promoted[0].uri, SEP1_URI)  # the key does not move

    def test_not_promoted_outside_the_window(self) -> None:
        known = [
            {
                "id": SEP1_POST_ID,
                "announcer": "@ClaudeDevs",
                "posted_at": "2026-09-01T18:38:00Z",  # 153 s away
            }
        ]
        self.assertIsNone(attach_post_ids([self._sep1_hint()], known)[0].post_id)

    def test_not_promoted_across_announcers(self) -> None:
        known = [
            {
                "id": "2095967323412930677",
                "announcer": "@lydiahallie",
                "posted_at": SEP1_CREATED_AT,
            }
        ]
        self.assertIsNone(attach_post_ids([self._sep1_hint()], known)[0].post_id)

    def test_ambiguous_window_leaves_the_hint_unpromoted(self) -> None:
        known = [
            {"id": "1", "announcer": "@ClaudeDevs", "posted_at": "2026-09-01T18:35:27Z"},
            {"id": "2", "announcer": "@ClaudeDevs", "posted_at": "2026-09-01T18:36:30Z"},
        ]
        self.assertIsNone(attach_post_ids([self._sep1_hint()], known)[0].post_id)

    def test_date_only_seed_row_does_not_match(self) -> None:
        # data/anthropic.json stores `announced_at` at midnight. Matching that
        # to a post made at 18:35 would be fabrication with a timestamp on it.
        known = [
            {
                "id": SEP1_POST_ID,
                "announcer": "@ClaudeDevs",
                "announced_at": "2026-09-01T00:00:00Z",
            }
        ]
        self.assertIsNone(attach_post_ids([self._sep1_hint()], known)[0].post_id)

    def test_unreadable_rows_are_ignored(self) -> None:
        known = [{}, {"id": "1"}, {"announcer": "@ClaudeDevs"}, {"id": "2", "announcer": "@ClaudeDevs"}]
        self.assertIsNone(attach_post_ids([self._sep1_hint()], known)[0].post_id)


class ImapChannel(unittest.TestCase):
    """Inert without a credential; honest about what it parsed."""

    def test_inert_without_a_credential(self) -> None:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("an unconfigured IMAP channel opened a connection")

        hints, notes = collect_imap_hints(now=NOW, env={}, connect=explode)
        self.assertEqual(hints, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("not configured", notes[0])
        self.assertIn("X_IMAP_HOST", notes[0])

    def test_partial_configuration_is_still_inert(self) -> None:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a half-configured IMAP channel opened a connection")

        env = {"X_IMAP_HOST": "imap.example.invalid", "X_IMAP_USER": "owner"}
        self.assertIsNone(imap_config(env))
        hints, notes = collect_imap_hints(now=NOW, env=env, connect=explode)
        self.assertEqual(hints, [])
        self.assertIn("not configured", notes[0])

    def test_password_read_from_credentials_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "X_IMAP_PASSWORD").write_text("hunter2-hunter2\n")
            env = {
                "X_IMAP_HOST": "imap.example.invalid",
                "X_IMAP_USER": "owner",
                "CREDENTIALS_DIRECTORY": tmp,
            }
            config = imap_config(env)
            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.password, "hunter2-hunter2")
            self.assertEqual(config.mailbox, "INBOX")
            self.assertEqual(config.port, 993)

    def test_read_secret_matches_the_notifier(self) -> None:
        # The duplication in discover_posts.read_secret is deliberate (see its
        # docstring); this is the test that keeps the two honest.
        try:
            from scripts import subscriptions
        except Exception as exc:  # pragma: no cover - depends on a sibling lane
            self.skipTest(f"scripts.subscriptions unavailable: {exc}")
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "X_IMAP_PASSWORD").write_text(" from-file \n")
            old = dict(os.environ)
            try:
                os.environ.pop("X_IMAP_PASSWORD", None)
                os.environ["CREDENTIALS_DIRECTORY"] = tmp
                self.assertEqual(
                    read_secret("X_IMAP_PASSWORD"),
                    subscriptions.read_secret("X_IMAP_PASSWORD"),
                )
                os.environ["X_IMAP_PASSWORD"] = " from-env "
                self.assertEqual(
                    read_secret("X_IMAP_PASSWORD"),
                    subscriptions.read_secret("X_IMAP_PASSWORD"),
                )
                self.assertEqual(read_secret("X_IMAP_PASSWORD"), "from-env")
            finally:
                os.environ.clear()
                os.environ.update(old)


class XNotificationParsing(unittest.TestCase):
    """RFC822 in, one candidate out — or nothing at all."""

    def test_parses_handle_id_and_text(self) -> None:
        hint = parse_x_notification(X_NOTIFICATION, now=NOW)
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual(hint.announcer, "@ClaudeDevs")
        self.assertEqual(hint.post_id, SEP1_POST_ID)
        self.assertEqual(hint.uri, f"x:{SEP1_POST_ID}")
        self.assertEqual(hint.source, "imap")
        self.assertEqual(hint.vendor, "anthropic")
        # Quoted-printable soft break rejoined, wrapper furniture dropped, the
        # HTML alternative ignored.
        self.assertEqual(hint.text, SEP1_TEXT)
        self.assertNotIn("HTML RENDERING", hint.text)
        self.assertNotIn("Unsubscribe", hint.text)
        self.assertEqual(hint.posted_at, "2026-09-01T18:37:11Z")

    def test_stranger_envelope_is_refused(self) -> None:
        forged = X_NOTIFICATION.replace(b"From: X <info@x.com>", b"From: X <info@x.com.evil.tld>")
        self.assertIsNone(parse_x_notification(forged, now=NOW))

    def test_message_without_a_status_url_yields_nothing(self) -> None:
        stripped = X_NOTIFICATION.replace(
            b"https://x.com/ClaudeDevs/status/2094856679250919746", b"https://x.com/ClaudeDevs"
        )
        self.assertIsNone(parse_x_notification(stripped, now=NOW))

    def test_percent_encoded_redirect_still_yields_the_id(self) -> None:
        wrapped = X_NOTIFICATION.replace(
            b"https://x.com/ClaudeDevs/status/2094856679250919746",
            b"https://t.example/r?url=https%3A%2F%2Fx.com%2FClaudeDevs%2Fstatus%2F2094856679250919746&t=1",
        )
        hint = parse_x_notification(wrapped, now=NOW)
        assert hint is not None
        self.assertEqual(hint.post_id, SEP1_POST_ID)

    def test_garbage_bytes_do_not_raise(self) -> None:
        self.assertIsNone(parse_x_notification(b"\x00\x01not a message", now=NOW))

    def test_unknown_announcer_gets_no_vendor(self) -> None:
        other = X_NOTIFICATION.replace(b"/ClaudeDevs/status/", b"/somebody_else/status/")
        hint = parse_x_notification(other, now=NOW)
        assert hint is not None
        self.assertEqual(hint.announcer, "@somebody_else")
        self.assertEqual(hint.vendor, "")  # P1 decides; this layer does not

    def test_collect_reads_the_mailbox_read_only(self) -> None:
        selected: list[tuple[str, bool]] = []

        class FakeIMAP:
            def __init__(self, host: str, port: int) -> None:
                self.host = host
                self.port = port
                self.logged_out = False

            def login(self, user: str, password: str) -> None:
                self.user = user

            def select(self, mailbox: str, readonly: bool = False) -> Any:
                selected.append((mailbox, readonly))
                return "OK", [b"1"]

            def search(self, charset: Optional[str], *criteria: str) -> Any:
                if '"x.com"' in criteria:
                    return "OK", [b"1"]
                return "OK", [b""]

            def fetch(self, message_id: str, spec: str) -> Any:
                return "OK", [(b"1 (RFC822 {123}", X_NOTIFICATION), b")"]

            def logout(self) -> None:
                self.logged_out = True

        env = {
            "X_IMAP_HOST": "imap.example.invalid",
            "X_IMAP_USER": "owner",
            "X_IMAP_PASSWORD": "hunter2-hunter2",
            "X_IMAP_MAILBOX": "X-Notifications",
        }
        hints, notes = collect_imap_hints(now=NOW, env=env, connect=FakeIMAP)
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].post_id, SEP1_POST_ID)
        self.assertEqual(selected, [("X-Notifications", True)])  # never marks mail read
        self.assertIn("1 hint(s)", notes[0])

    def test_a_flooded_mailbox_reads_only_the_newest(self) -> None:
        fetched: list[str] = []

        class FloodedIMAP:
            def __init__(self, host: str, port: int) -> None:
                pass

            def login(self, user: str, password: str) -> None:
                pass

            def select(self, mailbox: str, readonly: bool = False) -> Any:
                return "OK", [b"1"]

            def search(self, charset: Optional[str], *criteria: str) -> Any:
                if '"x.com"' in criteria:
                    # SEARCH returns ascending ids; the newest are last.
                    return "OK", [b" ".join(str(n).encode() for n in range(1, 251))]
                return "OK", [b""]

            def fetch(self, message_id: str, spec: str) -> Any:
                fetched.append(message_id)
                return "OK", [(b"1 (RFC822 {123}", X_NOTIFICATION), b")"]

            def logout(self) -> None:
                pass

        env = {
            "X_IMAP_HOST": "imap.example.invalid",
            "X_IMAP_USER": "owner",
            "X_IMAP_PASSWORD": "hunter2-hunter2",
        }
        hints, notes = collect_imap_hints(now=NOW, env=env, connect=FloodedIMAP)
        self.assertEqual(len(fetched), discover_posts.IMAP_MAX_MESSAGES)
        self.assertEqual(fetched[0], "51")  # the newest 200 of 250
        self.assertEqual(fetched[-1], "250")
        self.assertTrue(any("reading the newest 200" in note for note in notes))
        self.assertEqual(len(hints), 1)  # all 250 are the same post: one hint

    def test_unreachable_mailbox_is_logged_not_raised(self) -> None:
        def refuse(host: str, port: int) -> Any:
            raise OSError("[Errno 111] Connection refused")

        env = {
            "X_IMAP_HOST": "imap.example.invalid",
            "X_IMAP_USER": "owner",
            "X_IMAP_PASSWORD": "hunter2-hunter2",
        }
        hints, notes = collect_imap_hints(now=NOW, env=env, connect=refuse)
        self.assertEqual(hints, [])
        self.assertIn("unreachable", notes[0])


class SecretsNeverReachDisk(unittest.TestCase):
    """A hint file is written from text we did not author. Guard it."""

    def _payload_with(self, text: str) -> dict:
        hint = Hint(
            source="imap",
            observed_at="2026-09-06T22:00:00Z",
            announcer="@ClaudeDevs",
            text=text,
            post_id=SEP1_POST_ID,
            uri=f"x:{SEP1_POST_ID}",
            posted_at=SEP1_CREATED_AT,
        )
        return hints_payload("imap", [hint], now=NOW)

    def test_write_refuses_a_payload_containing_a_secret(self) -> None:
        env = {"X_IMAP_PASSWORD": "correct-horse-battery"}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "imap.json"
            payload = self._payload_with("login failed for correct-horse-battery")
            with self.assertRaises(SecretInHintError):
                write_hints(path, payload, env=env)
            self.assertFalse(path.exists())  # nothing partial left behind

    def test_a_clean_write_contains_no_credential(self) -> None:
        env = {
            "X_IMAP_PASSWORD": "correct-horse-battery",
            "RESEND_API_KEY": "re_liveKey_0123456789",
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "imap.json"
            write_hints(path, self._payload_with(SEP1_TEXT), env=env)
            written = path.read_text(encoding="utf-8")
            self.assertNotIn("correct-horse-battery", written)
            self.assertNotIn("re_liveKey_0123456789", written)
            self.assertIn(SEP1_TEXT, written)

    def test_a_short_env_value_is_not_treated_as_a_secret(self) -> None:
        # "abc" appears in half the English language; treating it as a secret
        # would make every write fail and the guard would be turned off.
        env = {"X_IMAP_PASSWORD": "abc"}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "imap.json"
            write_hints(path, self._payload_with("abc happens"), env=env)
            self.assertTrue(path.exists())


class EndToEnd(unittest.TestCase):
    """The whole pass, offline, against the saved fixtures."""

    def test_dry_run_writes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            lines, payloads = run(
                now=NOW,
                fetcher=discover_posts.fixture_fetcher(FIXTURES),
                dry_run=True,
                hints_dir=Path(tmp),
                env={},
            )
            self.assertFalse(hints_path("bsky", Path(tmp)).exists())
            self.assertTrue(any("dry run" in line for line in lines))
            self.assertTrue(payloads["bsky"]["hints"])

    def test_run_writes_and_second_run_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            fetcher = discover_posts.fixture_fetcher(FIXTURES)
            run(now=NOW, fetcher=fetcher, hints_dir=base, env={})
            first = json.loads(hints_path("bsky", base).read_text(encoding="utf-8"))
            later = NOW + timedelta(hours=1)
            run(now=later, fetcher=fetcher, hints_dir=base, env={})
            second = json.loads(hints_path("bsky", base).read_text(encoding="utf-8"))
            self.assertEqual(len(first["hints"]), len(second["hints"]))
            self.assertEqual(
                [row["uri"] for row in first["hints"]],
                [row["uri"] for row in second["hints"]],
            )
            self.assertEqual(
                first["hints"][0]["observed_at"], second["hints"][0]["observed_at"]
            )
            self.assertIn(SEP1_TEXT, json.dumps(second))

    def test_a_dead_mirror_does_not_stop_the_others(self) -> None:
        healthy = discover_posts.fixture_fetcher(FIXTURES)

        def fetcher(mirror: Mirror) -> Any:
            if mirror.handle == CLAUDEDEVS.handle:
                raise urllib.error.URLError("gone")
            return healthy(mirror)

        with TemporaryDirectory() as tmp:
            lines, payloads = run(
                now=NOW, fetcher=fetcher, dry_run=True, hints_dir=Path(tmp), env={}
            )
            self.assertTrue(any("unreachable" in line for line in lines))
            self.assertTrue(any("silent or unreachable" in line for line in lines))
            self.assertTrue(payloads["bsky"]["hints"])

    def test_cli_dry_run_against_fixtures(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            # env={} and a connect that refuses: without them this test opens
            # a real authenticated IMAP session the moment the owner follows
            # the setup instructions and exports X_IMAP_* on this machine.
            def _refuse(*_args, **_kwargs):
                raise AssertionError("a test must never open a real mailbox")

            code = discover_posts.main(
                ["--dry-run", "--imap", "--fixtures", str(FIXTURES)],
                env={},
                connect=_refuse,
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("bsky claudedevs-mirr.selfhosted.social", output)
        self.assertIn("dry run: would write", output)
        # --imap with no credential says so and stays silent about everything else.
        self.assertIn("not configured", output)


if __name__ == "__main__":
    unittest.main()
