#!/usr/bin/env python3
"""Hint sources: a second way to hear that a reset was announced (P5).

Why this exists. On 2026-09-01 at 18:35:27 UTC @ClaudeDevs posted "With Fable
5.1 out today, we've also reset 5-hour and weekly limits for all users." Every
channel this project had missed it: `data/anthropic.json` is hand-seeded with
no discovery job, and claude-resets.com — read again on 2026-09-06 — carries
the 2026-09-04 @lydiahallie post but still not this one. The event was
invisible here, and everyone who had opted into Anthropic alerts was told
nothing.

That same post IS in the Bluesky mirror of @ClaudeDevs, verbatim, with the
original X timestamp on it, indexed 89.9 minutes after it went up (measured
2026-09-06 on the live feed: createdAt 18:35:27Z, indexedAt 20:05:19Z). One
free unauthenticated GET would have surfaced it.

A HINT IS NOT TRUTH, and nothing here ever mails anybody. A mirror is a bot
retyping someone else's words: it can be wrong, stale, or taken down (X sent
Nitter a cease-and-desist on 2026-08-24, and these mirrors are the same kind
of target). What this module produces is a CANDIDATE — "somebody who looks
like @ClaudeDevs appears to have said this at this time" — written to
`data/hints/<source>.json` for the P1 verification path to confirm against
`cdn.syndication.twimg.com/tweet-result` or drop. The site's own rule stands:
a reset is something that happens to accounts, not something that gets
tweeted.

Two channels live here:

  bsky   the public Bluesky appview, no auth. Mirrors carry the verbatim text
         and the ORIGINAL createdAt but no X post id, so a hint is keyed by
         its at-uri and can only be promoted once another channel supplies an
         id (see `attach_post_ids`).
  imap   the owner's X notification mailbox, behind `--imap`. An X
         notification does carry the status id. Entirely inert without a
         credential: no host, no user, no password, no connection attempt.

Both degrade to a logged line. A hint job that raises would take the publish
tick down with it, and it is the least important job in the repository.
"""

from __future__ import annotations

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Callable, Any, Callable, Iterable, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
HINTS_DIR = REPO_ROOT / "data" / "hints"

# Hint files are gitignored working state, not published data. Kept bounded so
# a mirror that suddenly starts emitting cannot grow the file without limit.
MAX_HINTS_PER_SOURCE = 200


# ─── What a hint is ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hint:
    """One candidate post. Never published, never mailed, never "happened".

    `observed_at` is when WE saw it; `posted_at` is the announcer's original
    timestamp as the mirror recorded it. Both are kept because the gap between
    them IS the mirror's lag, and the lag is the only thing that says whether
    this channel is fast enough to be worth having (measured 2026-09-06 over
    120 records: @thsottiaux median 9 min, @ClaudeDevs median 96 min).

    `post_id` is None until some other channel supplies it. It is never
    guessed: a mirror record has no X id in it anywhere — checked across all
    120 records of the four mirrors, the only x.com links present are profile
    links inside @-mention facets.
    """

    source: str  # "bsky" | "imap"
    observed_at: str  # UTC, when this run recorded the hint
    announcer: str  # the X handle, "@ClaudeDevs"
    text: str  # verbatim, exactly as the channel carried it
    post_id: Optional[str]  # X status id, or None
    uri: str  # dedupe key: "bsky:at://…" or "x:<status id>"
    posted_at: Optional[str] = None  # the announcer's original timestamp
    vendor: str = ""  # "anthropic" | "openai"
    channel: str = ""  # which mirror or mailbox carried it

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observed_at": self.observed_at,
            "announcer": self.announcer,
            "text": self.text,
            "post_id": self.post_id,
            "uri": self.uri,
            "posted_at": self.posted_at,
            "vendor": self.vendor,
            "channel": self.channel,
        }

    @classmethod
    def from_json(cls, row: Mapping[str, Any]) -> "Hint":
        return cls(
            source=str(row.get("source", "")),
            observed_at=str(row.get("observed_at", "")),
            announcer=str(row.get("announcer", "")),
            text=str(row.get("text", "")),
            post_id=row.get("post_id") or None,
            uri=str(row.get("uri", "")),
            posted_at=row.get("posted_at") or None,
            vendor=str(row.get("vendor", "")),
            channel=str(row.get("channel", "")),
        )


# ─── Time ────────────────────────────────────────────────────────────────────


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse an upstream timestamp, or return None. Never raises.

    Bluesky sends "2026-09-01T18:35:27.000Z"; the seed files send
    "2026-04-16T00:00:00Z"; a tracker may send an offset. Anything else is a
    field we do not understand, and a hint job may not crash over it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ─── Bluesky mirrors ─────────────────────────────────────────────────────────

BSKY_ENDPOINT = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
BSKY_TIMEOUT_SECONDS = 15
BSKY_PAGE_LIMIT = 30

# A mirror that has posted nothing for this long is either taken down, broken,
# or the announcer has gone quiet — and this job cannot tell those apart, so it
# reports "dead" and lets a human look. 24 h is chosen against the measured
# posting cadence: over the four mirrors' 120 most recent records the longest
# gap on the fastest mirror was under a day, and @ClaudeDevs, the slowest
# poster, went 3 days between posts — so this WILL flag a quiet official
# account. That is the intended direction of error for a channel whose whole
# job is to notice silence.
MIRROR_SILENT_HOURS = 24

# Reposts arrive with this reason. Pins (`#reasonPin`) are the author's own
# post and must NOT be skipped.
REPOST_REASON = "app.bsky.feed.defs#reasonRepost"

# The mirrors also prefix a retweet's text with "RT @". Measured 2026-09-06:
# across 120 records exactly 5 carried `reason=reasonRepost` and exactly the
# same 5 began "RT @" — zero mismatches either way. Both checks are kept
# because they fail differently: if a mirror bot stops setting `reason`, the
# text prefix is all that stands between us and attributing @AnthropicAI's
# words to @bcherny.
RETWEET_TEXT_PREFIX = "RT @"


@dataclass(frozen=True)
class Mirror:
    handle: str  # Bluesky actor
    announcer: str  # the X account it mirrors
    vendor: str
    role: str  # "official" (plan Tier A) | "staff" (plan Tier B)
    fixture: str  # tests/fixtures/<fixture>.json


MIRRORS: tuple[Mirror, ...] = (
    Mirror(
        handle="claudedevs-mirr.selfhosted.social",
        announcer="@ClaudeDevs",
        vendor="anthropic",
        role="official",
        fixture="bsky_claudedevs",
    ),
    Mirror(
        handle="thsottiaux-mirr.selfhosted.social",
        announcer="@thsottiaux",
        vendor="openai",
        role="staff",
        fixture="bsky_thsottiaux",
    ),
    Mirror(
        handle="trq212-mirr.selfhosted.social",
        announcer="@trq212",
        vendor="anthropic",
        role="staff",
        fixture="bsky_trq212",
    ),
    Mirror(
        handle="bcherny-mirr.selfhosted.social",
        announcer="@bcherny",
        vendor="anthropic",
        role="staff",
        fixture="bsky_bcherny",
    ),
)


@dataclass(frozen=True)
class MirrorReport:
    """What one mirror gave us this run, including the ways it gave nothing."""

    mirror: str
    announcer: str
    ok: bool  # the fetch itself succeeded and parsed
    dead: bool  # unreachable, or silent past MIRROR_SILENT_HOURS
    note: str  # one log line, always safe to print
    hints: tuple[Hint, ...] = ()
    last_post_at: Optional[str] = None  # newest record of ANY kind
    silent_hours: Optional[float] = None
    lag_minutes: Optional[float] = None  # how far behind X the mirror ran
    skipped: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "mirror": self.mirror,
            "announcer": self.announcer,
            "ok": self.ok,
            "dead": self.dead,
            "note": self.note,
            "last_post_at": self.last_post_at,
            "silent_hours": self.silent_hours,
            "lag_minutes": self.lag_minutes,
            "hints": len(self.hints),
            "skipped": self.skipped,
        }


# urllib.error.URLError is already an OSError; both are named so the failure
# modes stay readable, the way scripts/fetch_openai.py names its five.
FETCH_ERRORS = (
    urllib.error.HTTPError,  # 400 "Profile not found" once a mirror is deleted
    urllib.error.URLError,  # DNS gone, TLS handshake timeout, refused
    OSError,  # bare TimeoutError from r.read()
    json.JSONDecodeError,  # an HTML error page from a proxy
    ValueError,
)


def fetch_author_feed(
    handle: str,
    *,
    limit: int = BSKY_PAGE_LIMIT,
    timeout: int = BSKY_TIMEOUT_SECONDS,
    opener: Optional[Callable[..., Any]] = None,
) -> Any:
    """GET one mirror's author feed. Raises; callers turn that into a report."""
    query = urllib.parse.urlencode({"actor": handle, "limit": limit})
    request = urllib.request.Request(
        f"{BSKY_ENDPOINT}?{query}",
        headers={
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (compatible; ai-resets/1.0)",
        },
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as response:
        return json.loads(response.read())


def short_reason(exc: BaseException) -> str:
    """One line, no traceback, no URL query — a log line, not a stack dump."""
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001 - a closed body is not worth a crash
            body = b""
        if body:
            try:
                detail = str(json.loads(body).get("message", ""))
            except (ValueError, AttributeError):
                detail = ""
        return f"HTTP {exc.code}" + (f" ({detail})" if detail else "")
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def is_skippable(item: Mapping[str, Any], mirror: Mirror) -> Optional[str]:
    """Why this feed item is not a hint, or None if it is one.

    Replies and reposts are both excluded by the plan: a reply is the
    announcer answering somebody (the 2026-09-01 reset post's own thread has
    three of them, all "Docs: …"), and a repost is not the announcer's words
    at all.
    """
    reason = item.get("reason")
    if isinstance(reason, Mapping) and reason.get("$type") == REPOST_REASON:
        return "repost"
    post = item.get("post")
    if not isinstance(post, Mapping):
        return "malformed"
    record = post.get("record")
    if not isinstance(record, Mapping):
        return "malformed"
    if "reply" in record or "reply" in item:
        return "reply"
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return "empty"
    if text.startswith(RETWEET_TEXT_PREFIX):
        return "retweet"
    author = post.get("author")
    if isinstance(author, Mapping):
        handle = author.get("handle")
        if isinstance(handle, str) and handle and handle != mirror.handle:
            # Defence in depth: a repost's `post.author` is the ORIGINAL
            # author, so this catches a repost even if `reason` were dropped.
            return "other-author"
    return None


def hints_from_feed(
    payload: Any,
    mirror: Mirror,
    *,
    now: Optional[datetime] = None,
) -> tuple[list[Hint], int]:
    """Pure: feed payload -> (hints newest first, count skipped)."""
    moment = iso_z(now or utc_now())
    feed = payload.get("feed") if isinstance(payload, Mapping) else None
    if not isinstance(feed, list):
        raise ValueError("feed is not a list")
    hints: list[Hint] = []
    skipped = 0
    seen: set[str] = set()
    for item in feed:
        if not isinstance(item, Mapping):
            skipped += 1
            continue
        if is_skippable(item, mirror) is not None:
            skipped += 1
            continue
        post = item["post"]
        record = post["record"]
        uri = post.get("uri")
        if not isinstance(uri, str) or not uri:
            skipped += 1
            continue
        key = f"bsky:{uri}"
        if key in seen:
            # The appview can repeat a post across a page boundary.
            continue
        seen.add(key)
        hints.append(
            Hint(
                source="bsky",
                observed_at=moment,
                announcer=mirror.announcer,
                # Verbatim. No strip, no collapse, no shortener rewriting: the
                # email quotes the post and the reader compares it with X.
                text=record["text"],
                post_id=None,
                uri=key,
                posted_at=record.get("createdAt"),
                vendor=mirror.vendor,
                channel=mirror.handle,
            )
        )
    hints.sort(key=lambda h: h.posted_at or "", reverse=True)
    return hints, skipped


def newest_record_at(payload: Any) -> Optional[datetime]:
    """When the mirror last carried ANYTHING, replies and reposts included.

    This is the liveness clock, and it is deliberately not the hint clock. The
    first live dry run of this script called three of the four mirrors DEAD
    because @ClaudeDevs, @trq212 and @bcherny had posted nothing top-level for
    three days — all three mirrors were answering HTTP 200 with fresh replies
    the whole time. An alarm that fires whenever an announcer is quiet over a
    weekend is an alarm the owner learns to ignore, which is exactly how the
    35 blind probe polls on 2026-09-03 went unnoticed.
    """
    feed = payload.get("feed") if isinstance(payload, Mapping) else None
    if not isinstance(feed, list):
        return None
    stamps = []
    for item in feed:
        if not isinstance(item, Mapping):
            continue
        post = item.get("post")
        if not isinstance(post, Mapping):
            continue
        record = post.get("record")
        stamp = record.get("createdAt") if isinstance(record, Mapping) else None
        parsed = parse_iso(stamp) or parse_iso(post.get("indexedAt"))
        if parsed is not None:
            stamps.append(parsed)
    return max(stamps) if stamps else None


def mirror_lag_minutes(payload: Any) -> Optional[float]:
    """How far behind X the mirror ran on its newest record, in minutes.

    Measured 2026-09-06 over the four mirrors' 120 most recent records:
    @thsottiaux median 9 min, @ClaudeDevs median 96 min, @bcherny 40 min,
    @trq212 50 min. This is the number that decides whether the channel is
    worth having; the plan's stated range was 2 min to 2 h and it holds.
    """
    feed = payload.get("feed") if isinstance(payload, Mapping) else None
    if not isinstance(feed, list):
        return None
    best: Optional[tuple[datetime, float]] = None
    for item in feed:
        if not isinstance(item, Mapping):
            continue
        post = item.get("post")
        if not isinstance(post, Mapping):
            continue
        record = post.get("record")
        created = parse_iso(record.get("createdAt")) if isinstance(record, Mapping) else None
        indexed = parse_iso(post.get("indexedAt"))
        if created is None or indexed is None or indexed < created:
            continue
        lag = (indexed - created).total_seconds() / 60
        if best is None or created > best[0]:
            best = (created, lag)
    return None if best is None else round(best[1], 1)


def collect_mirror(
    mirror: Mirror,
    *,
    now: Optional[datetime] = None,
    fetcher: Optional[Callable[[Mirror], Any]] = None,
) -> MirrorReport:
    """Fetch one mirror and classify the result. NEVER raises."""
    moment = now or utc_now()
    read = fetcher or (lambda m: fetch_author_feed(m.handle))
    try:
        payload = read(mirror)
    except FETCH_ERRORS as exc:
        return MirrorReport(
            mirror=mirror.handle,
            announcer=mirror.announcer,
            ok=False,
            dead=True,
            note=f"unreachable ({short_reason(exc)}) — treated as dead, no hints",
        )
    try:
        hints, skipped = hints_from_feed(payload, mirror, now=moment)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return MirrorReport(
            mirror=mirror.handle,
            announcer=mirror.announcer,
            ok=False,
            dead=True,
            note=f"unreadable payload ({short_reason(exc)}) — treated as dead",
        )
    newest = newest_record_at(payload)
    lag = mirror_lag_minutes(payload)
    if newest is None:
        return MirrorReport(
            mirror=mirror.handle,
            announcer=mirror.announcer,
            ok=True,
            dead=True,
            note="SILENT: not one datable record in the feed",
            hints=tuple(hints),
            skipped=skipped,
        )
    silent_for = moment - newest
    if silent_for > timedelta(hours=MIRROR_SILENT_HOURS):
        hours = silent_for.total_seconds() / 3600
        return MirrorReport(
            mirror=mirror.handle,
            announcer=mirror.announcer,
            # `ok` says the fetch worked, `dead` says the channel is not
            # currently telling us anything. A consumer that needs the
            # difference — mirror taken down vs announcer on holiday — reads
            # both, and this job never claims to know which it is.
            ok=True,
            dead=True,
            note=(
                f"SILENT: nothing since {iso_z(newest)} ({hours:.1f} h) — "
                "mirror down or announcer quiet, this job cannot tell which"
            ),
            hints=tuple(hints),
            last_post_at=iso_z(newest),
            silent_hours=round(hours, 1),
            lag_minutes=lag,
            skipped=skipped,
        )
    lag_note = f", lag {lag:.0f} min" if lag is not None else ""
    return MirrorReport(
        mirror=mirror.handle,
        announcer=mirror.announcer,
        ok=True,
        dead=False,
        note=(
            f"{len(hints)} hint(s), {skipped} skipped, newest record "
            f"{(moment - newest).total_seconds() / 60:.0f} min old{lag_note}"
        ),
        hints=tuple(hints),
        last_post_at=iso_z(newest),
        silent_hours=round(silent_for.total_seconds() / 3600, 1),
        lag_minutes=lag,
        skipped=skipped,
    )


# ─── Linking a hint to a post id ─────────────────────────────────────────────

# A mirror record carries no X id, so a bsky hint can only be promoted when a
# channel that HAS ids (a tracker feed, the mailbox, the seed file) shows the
# same announcer at the same instant. 120 s is the plan's window: the mirror
# copies the ORIGINAL createdAt, so a correct match is normally exact to the
# second, and the slack is for feeds that round to the minute.
POST_ID_WINDOW_SECONDS = 120


@dataclass(frozen=True)
class KnownPost:
    announcer: str
    post_id: str
    posted_at: Optional[datetime]

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> Optional["KnownPost"]:
        """Read one row of somebody else's feed.

        The aliases are deliberate: P1's Anthropic fetcher is being written in
        parallel and its row shape is not frozen, while `data/anthropic.json`
        already uses `announced_at` and claude-resets.com uses `date`. Reading
        several key names is cheaper than coupling this file to a schema that
        is still moving, and an unreadable row is skipped, never guessed at.
        """
        post_id = row.get("post_id") or row.get("id")
        announcer = (
            row.get("announcer")
            or row.get("screen_name")
            or row.get("handle")
            or row.get("account")
            or ""
        )
        stamp = (
            row.get("posted_at")
            or row.get("created_at")
            or row.get("announced_at")
            or row.get("date")
        )
        if not post_id or not str(announcer).strip():
            return None
        return cls(
            announcer=str(announcer),
            post_id=str(post_id),
            posted_at=parse_iso(stamp),
        )


def normalise_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def attach_post_ids(
    hints: Sequence[Hint],
    known_posts: Iterable[Mapping[str, Any]],
    *,
    window_seconds: int = POST_ID_WINDOW_SECONDS,
) -> list[Hint]:
    """Fill in `post_id` where a known post matches announcer AND time.

    Two guards, both about never inventing an id:
      - a hint whose timestamp will not parse is left alone;
      - if TWO known posts land inside the window the hint stays unpromoted,
        because picking one would be a coin flip presented as provenance.
    """
    candidates = [
        known
        for known in (KnownPost.from_mapping(row) for row in known_posts)
        if known is not None and known.posted_at is not None
    ]
    window = timedelta(seconds=window_seconds)
    out: list[Hint] = []
    for hint in hints:
        if hint.post_id:
            out.append(hint)
            continue
        posted = parse_iso(hint.posted_at)
        if posted is None:
            out.append(hint)
            continue
        matches = [
            known
            for known in candidates
            if normalise_handle(known.announcer) == normalise_handle(hint.announcer)
            and known.posted_at is not None
            and abs(known.posted_at - posted) <= window
        ]
        if len(matches) == 1:
            out.append(replace(hint, post_id=matches[0].post_id))
        else:
            out.append(hint)
    return out


# ─── The owner's X mailbox (IMAP) ────────────────────────────────────────────

IMAP_HOST_ENV = "X_IMAP_HOST"
IMAP_USER_ENV = "X_IMAP_USER"
IMAP_MAILBOX_ENV = "X_IMAP_MAILBOX"
IMAP_PORT_ENV = "X_IMAP_PORT"
IMAP_PASSWORD_SECRET = "X_IMAP_PASSWORD"
IMAP_LOOKBACK_DAYS = 3

# An X notification mailbox can hold hundreds of messages a day, and every one
# of them costs a FETCH of the full RFC822 body. Bound the work: the newest
# messages are the ones a reset announcement would be in, and a hint that
# arrives one run late costs nothing because hints are not the alarm.
IMAP_MAX_MESSAGES = 200

# X has sent notification mail from several envelopes over the years. A
# message from anywhere else is not parsed at all — an attacker who can put
# mail in this mailbox should not be able to put a handle and an id into a
# hint file. This is a filter, not authentication: the hint is still only a
# candidate, and P1 re-verifies the id against tweet-result before anything
# reaches the site or a subscriber.
X_SENDER_DOMAINS = ("x.com", "twitter.com")

X_STATUS_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{5,25})",
    re.IGNORECASE,
)

# Lines that belong to the mail wrapper, not to the post.
MAIL_FOOTER_MARKERS = (
    "unsubscribe",
    "view post",
    "view on x",
    "open in x",
    "settings",
    "you received this",
    "why am i seeing this",
    "notification preferences",
    "reply to this email",
    "sent by x corp",
    "x corp.",
)


@dataclass(frozen=True)
class ImapConfig:
    host: str
    user: str
    password: str
    mailbox: str = "INBOX"
    port: int = 993


def read_secret(name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Env, then a systemd credential file — the contract from subscriptions.py.

    Deliberately a local copy of `scripts.subscriptions.read_secret` rather
    than an import: importing that module pulls an HTTP server, a SQLite
    schema and a Resend client into a hint job whose defining property is that
    it does nothing at all when unconfigured. The two are seven lines each and
    a test asserts this one behaves identically.
    """
    environ = os.environ if env is None else env
    value = environ.get(name, "").strip()
    if value:
        return value
    credentials_dir = environ.get("CREDENTIALS_DIRECTORY", "")
    if credentials_dir:
        path = Path(credentials_dir) / name
        if path.is_file():
            return path.read_text().strip()
    return ""


def imap_config(env: Optional[Mapping[str, str]] = None) -> Optional[ImapConfig]:
    """The config, or None. None means: do not touch the network."""
    environ = os.environ if env is None else env
    host = environ.get(IMAP_HOST_ENV, "").strip()
    user = environ.get(IMAP_USER_ENV, "").strip()
    password = read_secret(IMAP_PASSWORD_SECRET, environ)
    if not (host and user and password):
        return None
    try:
        port = int(environ.get(IMAP_PORT_ENV, "993"))
    except ValueError:
        port = 993
    return ImapConfig(
        host=host,
        user=user,
        password=password,
        mailbox=environ.get(IMAP_MAILBOX_ENV, "INBOX").strip() or "INBOX",
        port=port,
    )


NOT_CONFIGURED_NOTE = (
    f"imap: not configured — set {IMAP_HOST_ENV}, {IMAP_USER_ENV} and "
    f"{IMAP_PASSWORD_SECRET} (env or CREDENTIALS_DIRECTORY); no connection attempted"
)


def _decoded_body(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, (bytes, bytearray)):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return bytes(payload).decode(charset, errors="replace")
    except LookupError:
        return bytes(payload).decode("utf-8", errors="replace")


def message_body(msg: Message) -> str:
    """Prefer text/plain; fall back to de-tagged text/html."""
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() == "text/plain":
            plain.append(_decoded_body(part))
        elif part.get_content_type() == "text/html":
            html.append(_decoded_body(part))
    if plain:
        return "\n".join(plain)
    if html:
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(html))
        stripped = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", stripped)
        stripped = re.sub(r"(?s)<[^>]+>", "", stripped)
        return re.sub(r"[ \t]+", " ", stripped)
    return ""


def _is_footer(line: str) -> bool:
    low = line.strip().lower()
    return any(marker in low for marker in MAIL_FOOTER_MARKERS)


def extract_post_text(body: str, handle: str) -> str:
    """Best-effort read of the quoted post out of a notification body.

    Honest about its status: the exact layout of an X "posted" notification is
    a HYPOTHESIS here. The owner has not yet forwarded a real one (it is an
    open item in docs/robustness-plan.md), so this keeps only what is
    structurally safe — drop URLs, drop the wrapper's own furniture, drop the
    "Name (@handle)" line — and takes the first surviving block. The status ID
    is the load-bearing part of an IMAP hint; the text is a preview, and P1
    fetches the verbatim wording by id before anybody reads it.
    """
    handle_low = normalise_handle(handle)
    block: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            if block:
                break  # first contiguous block wins
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue
        if _is_footer(line):
            if block:
                break
            continue
        low = line.lower()
        if f"@{handle_low}" in low and len(line) <= 80 and not block:
            continue  # the "Name (@handle)" header line
        block.append(line)
    return "\n".join(block).strip()


def is_x_sender(from_header: Optional[str]) -> bool:
    """True only when the envelope domain IS an X domain.

    Substring matching is not good enough and it is not a theoretical worry:
    `"@x.com" in "info@x.com.evil.tld"` is True, so a naive check would let
    anybody who can send the owner mail write a handle and a status id into a
    hint file. The address is parsed and the domain compared exactly, or as a
    subdomain (X really does send from notify.twitter.com).
    """
    _, address = email.utils.parseaddr(from_header or "")
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    return any(domain == known or domain.endswith(f".{known}") for known in X_SENDER_DOMAINS)


def parse_x_notification(
    raw: bytes,
    *,
    now: Optional[datetime] = None,
) -> Optional[Hint]:
    """RFC822 bytes -> one hint, or None. Never raises on a malformed message."""
    try:
        msg = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001 - a broken message is not an outage
        return None
    if not is_x_sender(msg.get("From")):
        return None
    body = message_body(msg)
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject") or "")))
    haystack = f"{subject}\n{body}"
    match = X_STATUS_URL_RE.search(haystack)
    if match is None:
        # X wraps links; a percent-encoded status URL inside a redirect is the
        # same evidence, so unquote once and look again. Still no id means no
        # hint: this channel exists to supply ids, and one is never invented.
        match = X_STATUS_URL_RE.search(urllib.parse.unquote(haystack))
    if match is None:
        return None
    handle, post_id = match.group(1), match.group(2)
    text = extract_post_text(body, handle)
    if not text:
        text = subject.strip()
    # A Date header is RFC 2822 ("Mon, 01 Sep 2026 18:36:02 +0000"), which is
    # the mail wrapper's clock, not X's. Close enough to bracket the post and
    # honest about which clock it is.
    stamp = _parse_rfc2822_date(msg.get("Date"))
    return Hint(
        source="imap",
        observed_at=iso_z(now or utc_now()),
        announcer=f"@{handle}",
        text=text,
        post_id=post_id,
        uri=f"x:{post_id}",
        posted_at=iso_z(stamp) if stamp else None,
        vendor=vendor_for_announcer(handle),
        channel="x-notifications",
    )


def _parse_rfc2822_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def vendor_for_announcer(handle: str) -> str:
    """Vendor from the mirror registry; "" when the handle is not one we track.

    An empty vendor is correct and useful: it says "a post arrived from an
    account this project has no allow-list entry for", which the P1 layer
    must decide about, not this one.
    """
    wanted = normalise_handle(handle)
    for mirror in MIRRORS:
        if normalise_handle(mirror.announcer) == wanted:
            return mirror.vendor
    return ""


def collect_imap_hints(
    *,
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
    connect: Optional[Callable[..., Any]] = None,
) -> tuple[list[Hint], list[str]]:
    """Read the mailbox if and only if it is configured. NEVER raises."""
    config = imap_config(env)
    if config is None:
        # The inert path: no host resolution, no socket, no imaplib call.
        return [], [NOT_CONFIGURED_NOTE]
    moment = now or utc_now()
    since = (moment - timedelta(days=IMAP_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    open_imap = connect or imaplib.IMAP4_SSL
    client = None
    hints: list[Hint] = []
    notes: list[str] = []
    try:
        client = open_imap(config.host, config.port)
        client.login(config.user, config.password)
        # readonly: this is the owner's real mailbox and a hint job has no
        # business marking their mail as read.
        client.select(config.mailbox, readonly=True)
        seen_uris: set[str] = set()
        for domain in X_SENDER_DOMAINS:
            status, data = client.search(None, "FROM", f'"{domain}"', "SINCE", since)
            if status != "OK" or not data:
                continue
            ids = (data[0] or b"").split()
            if len(ids) > IMAP_MAX_MESSAGES:
                notes.append(
                    f"imap: {len(ids)} messages from {domain} since {since}, "
                    f"reading the newest {IMAP_MAX_MESSAGES}"
                )
                ids = ids[-IMAP_MAX_MESSAGES:]  # SEARCH returns ascending ids
            for raw_id in ids:
                # imaplib hands back byte ids from SEARCH and wants a str for
                # FETCH; the ids are decimal ASCII either way.
                message_id = raw_id.decode("ascii", "ignore") if isinstance(raw_id, bytes) else str(raw_id)
                status, payload = client.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload:
                    continue
                raw = _first_rfc822(payload)
                if raw is None:
                    continue
                hint = parse_x_notification(raw, now=moment)
                if hint is None or hint.uri in seen_uris:
                    continue
                seen_uris.add(hint.uri)
                hints.append(hint)
        notes.append(f"imap: {len(hints)} hint(s) from {config.mailbox} since {since}")
    except (imaplib.IMAP4.error, OSError, ValueError) as exc:
        notes.append(f"imap: unreachable ({short_reason(exc)}) — no hints this run")
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 - logout failing changes nothing
                pass
    hints.sort(key=lambda h: h.posted_at or "", reverse=True)
    return hints, notes


def _first_rfc822(payload: Any) -> Optional[bytes]:
    """imaplib returns [(b'1 (RFC822 {N}', b'<message>'), b')'] — pick bytes."""
    if not isinstance(payload, (list, tuple)):
        return None
    for part in payload:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return None


# ─── Writing hint files ──────────────────────────────────────────────────────

# Names checked before a hint file is written. A hint carries text copied off
# the public internet and out of the owner's mailbox; if a credential ever
# reached one of those strings, this file would be where it landed on disk.
SECRET_ENV_NAMES = (
    IMAP_PASSWORD_SECRET,
    "RESEND_API_KEY",
    "SUBSCRIPTION_SECRET",
    "RESEND_WEBHOOK_SECRET",
    # Not secrets, but identifiers. A failed connection puts str(exc) into a
    # note that is printed AND written to data/hints/imap.json, and an SSL or
    # DNS error carries the hostname and often the login. The owner's mail
    # provider and address are not this file's business.
    "X_IMAP_HOST",
    "X_IMAP_USER",
)

# Short values are not secrets, they are coincidences: a 4-character password
# would match half the English language and make the guard useless.
MIN_SECRET_LENGTH = 8


class SecretInHintError(RuntimeError):
    """A configured secret appeared in a hint payload. Nothing is written."""


def configured_secrets(env: Optional[Mapping[str, str]] = None) -> list[str]:
    values = []
    for name in SECRET_ENV_NAMES:
        value = read_secret(name, env)
        if len(value) >= MIN_SECRET_LENGTH:
            values.append(value)
    return values


def assert_no_secret(serialised: str, env: Optional[Mapping[str, str]] = None) -> None:
    for value in configured_secrets(env):
        if value in serialised:
            # The value itself is never printed, here or anywhere else.
            raise SecretInHintError("a configured secret appeared in the hint payload")


def hints_path(source: str, base: Optional[Path] = None) -> Path:
    return (base or HINTS_DIR) / f"{source}.json"


def load_hints(path: Path) -> list[Hint]:
    """Existing hints, or none. A corrupt file is replaced, never raised over."""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    rows = payload.get("hints") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return []
    return [Hint.from_json(row) for row in rows if isinstance(row, Mapping)]


def merge_hints(existing: Sequence[Hint], fresh: Sequence[Hint]) -> list[Hint]:
    """Dedupe by uri, keeping the FIRST sighting's `observed_at`.

    Keeping the earliest observation is the point of the file: it is the
    record of when this channel could first have told us, which is exactly the
    number the 2026-09-01 miss needs answered. A later run that re-reads the
    same post must not quietly move that moment forward. A post_id learned
    later IS adopted — that is a hint being promoted, not rewritten.
    """
    by_uri: dict[str, Hint] = {}
    order: list[str] = []
    for hint in list(existing) + list(fresh):
        if hint.uri in by_uri:
            previous = by_uri[hint.uri]
            by_uri[hint.uri] = replace(
                previous,
                post_id=previous.post_id or hint.post_id,
            )
            continue
        by_uri[hint.uri] = hint
        order.append(hint.uri)
    merged = [by_uri[uri] for uri in order]
    merged.sort(key=lambda h: (h.posted_at or "", h.uri), reverse=True)
    return merged[:MAX_HINTS_PER_SOURCE]


def hints_payload(
    source: str,
    hints: Sequence[Hint],
    *,
    now: datetime,
    channels: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "source": source,
        "updated_at": iso_z(now),
        "note": (
            "Candidates only. A hint is never truth, never publishable and "
            "never mailed; the P1 verification path confirms or drops it."
        ),
        "channels": list(channels),
        "hints": [hint.as_json() for hint in hints],
    }


def write_hints(path: Path, payload: Mapping[str, Any], *, env: Optional[Mapping[str, str]] = None) -> None:
    """Serialise, refuse on a secret, then replace atomically."""
    serialised = json.dumps(payload, indent=2, ensure_ascii=False)
    assert_no_secret(serialised, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(serialised + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ─── Running it ──────────────────────────────────────────────────────────────


def fixture_fetcher(directory: Path) -> Callable[[Mirror], Any]:
    """Read feeds from saved fixtures — the offline path used by the tests."""

    def read(mirror: Mirror) -> Any:
        path = directory / f"{mirror.fixture}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    return read


def run(
    *,
    now: Optional[datetime] = None,
    mirrors: Sequence[Mirror] = MIRRORS,
    fetcher: Optional[Callable[[Mirror], Any]] = None,
    use_imap: bool = False,
    dry_run: bool = False,
    hints_dir: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    connect: Optional[Callable[..., Any]] = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """One pass over every channel. Returns (log lines, payload per source)."""
    moment = now or utc_now()
    base = hints_dir or HINTS_DIR
    lines: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}

    reports = [collect_mirror(mirror, now=moment, fetcher=fetcher) for mirror in mirrors]
    fresh: list[Hint] = []
    for report in reports:
        fresh.extend(report.hints)
        lines.append(f"bsky {report.mirror}: {report.note}")
    dead = [r.mirror for r in reports if r.dead]
    if dead:
        lines.append(
            f"bsky: {len(dead)} of {len(reports)} mirrors silent or unreachable: "
            f"{', '.join(dead)}"
        )
    payloads["bsky"] = hints_payload(
        "bsky",
        merge_hints(load_hints(hints_path("bsky", base)), fresh),
        now=moment,
        channels=[r.as_json() for r in reports],
    )

    if use_imap:
        imap_hints, notes = collect_imap_hints(now=moment, env=env, connect=connect)
        lines.extend(notes)
        payloads["imap"] = hints_payload(
            "imap",
            merge_hints(load_hints(hints_path("imap", base)), imap_hints),
            now=moment,
            channels=[{"channel": "x-notifications", "note": notes[-1] if notes else ""}],
        )

    for source, payload in payloads.items():
        path = hints_path(source, base)
        count = len(payload["hints"])
        if dry_run:
            # The guard runs on the dry-run too: a preview that skipped it
            # would pass while the real write refused.
            assert_no_secret(json.dumps(payload, ensure_ascii=False), env)
            lines.append(f"dry run: would write {count} hint(s) -> {path}")
        else:
            write_hints(path, payload, env=env)
            lines.append(f"wrote {count} hint(s) -> {path}")
    return lines, payloads


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[dict] = None,
    connect: Optional[Callable] = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Surface candidate reset announcements from Bluesky mirrors and, "
            "with --imap, the owner's X notification mailbox. Hints are never "
            "truth and never mail anybody."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--imap", action="store_true", help="also read the X mailbox")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="read mirror feeds from this directory instead of the network",
    )
    parser.add_argument(
        "--mirror",
        action="append",
        default=None,
        help="limit to this Bluesky handle (repeatable)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    mirrors = MIRRORS
    if args.mirror:
        wanted = {m.lower() for m in args.mirror}
        mirrors = tuple(m for m in MIRRORS if m.handle.lower() in wanted)
        if not mirrors:
            print(f"no mirror matches {sorted(wanted)}")
            return 0
    fetcher = fixture_fetcher(args.fixtures) if args.fixtures else None

    try:
        lines, _ = run(
            mirrors=mirrors,
            fetcher=fetcher,
            use_imap=args.imap,
            dry_run=args.dry_run,
            # Injectable so a CLI test can exercise --imap without reaching a
            # real mailbox. Passing them through rather than letting run()
            # fall back to os.environ is what stops a test from opening an
            # authenticated connection the moment the owner configures the
            # channel on this machine.
            env=env,
            connect=connect,
        )
    except SecretInHintError as exc:
        # Loud, and still not a crash for the publish tick to trip over.
        print(f"REFUSED: {exc}")
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
