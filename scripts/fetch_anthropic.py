#!/usr/bin/env python3
"""Discover Anthropic reset announcements instead of waiting to be told.

Before this script, `data/anthropic.json` was a single hand-seeded commit from
2026-07-25 with no fetcher and no timer, and its published source note claimed
a "weekly scheduled search" that did not exist. Two resets were missed:
2026-09-01T18:35:27Z by @ClaudeDevs (post 2094856679250919746) and
2026-09-04T20:08:45Z by @lydiahallie (post 2095967323412930677). Almost every
active subscriber had opted into Anthropic alerts and none had ever received
one. Whether a Claude reset reached anybody depended on which handle posted it.

Two sources, in strict order of authority:

1. https://claude-resets.com/api/resets — a free, unauthenticated feed that
   carries a `claude` provider block. It is hand-curated in practice: its own
   `meta.detector.status` read "degraded" with `lastSuccessfulCheckAt: null` on
   2026-09-07, it carried the Sep 4 reset and MISSED the Sep 1 one, and its
   `note` field is a summary written by the tracker, not the post.
2. https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=0 — free, no
   auth, returns the post's real `text`, `created_at` and `user.screen_name`.
   Every numeric id is checked here before its words are trusted, because the
   email quotes the post VERBATIM and a paraphrase in a blockquote is a
   fabricated quotation.

Nothing is ever dropped for failing verification: an unverified row keeps the
tracker's text with `text_verified: false`, and the notifier footnotes it.

Soft-failure contract, identical to scripts/fetch_openai.py (P0d): every fetch
or parse failure keeps the last good data/anthropic.json, stamps
`source.stale_since` once, prints one line and exits 0, so a third-party
tracker outage costs one column and not the whole five-minute publish.

Unlike data/openai.json, data/anthropic.json is BOTH the tracked seed and the
merge target: the tracker demonstrably misses events (Sep 1), so hand-seeded
rows have to survive every fetch. The merge is a union by id in which a row on
disk is never deleted and never downgraded — see `merge_events`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # Run by cron as an absolute path, which puts scripts/ on sys.path rather
    # than the repository root.
    sys.path.insert(0, str(ROOT))

from scripts.incidents import announcer_from_url, announcer_role  # noqa: E402

# ─── Upstream contract ───────────────────────────────────────────────────────

API_URL = "https://claude-resets.com/api/resets"
PROVIDER_KEY = "claude"
OUT_FILE = ROOT / "data" / "anthropic.json"
TIMEOUT_SECONDS = 15

SOURCE_NAME = "claude-resets.com + verified X posts"
SOURCE_URL = "https://claude-resets.com/"
# Kept byte-identical to the `source.note` committed in data/anthropic.json so
# an ordinary fetch does not rewrite the tracked seed's prose. It replaces the
# note that claimed a "weekly scheduled search"; there was no search, no job
# and no timer.
SOURCE_NOTE = (
    "Anthropic publishes no reset API. Rows come from claude-resets.com's "
    "public /api/resets feed, merged with hand-seeded posts that feed does not "
    "carry (it misses 2094856679250919746, the 2026-09-01 reset). Every "
    "numeric post id is checked against cdn.syndication.twimg.com/tweet-result "
    "before its words are used; a row with text_verified false shows the "
    "tracker's own summary, not the post."
)

# How many events the tracker may lose in one read before the payload is
# refused. The feed appends, so a large shrink is upstream being broken. Three
# rows of tolerance mirrors fetch_openai.py, where upstream twice removed a
# single row it should not have published.
MAX_SHRINK_ROWS = 3

# urllib.error.URLError and json.JSONDecodeError are already OSError and
# ValueError subclasses; all five are named anyway so the failure modes stay
# readable next to the 52 tracebacks publish.log holds for the OpenAI fetcher.
SOFT_ERRORS = (
    urllib.error.URLError,  # DNS failure, refused connection, TLS handshake timeout
    OSError,  # bare TimeoutError from r.read(), after urlopen already returned
    json.JSONDecodeError,  # an HTML error page or a truncated body
    KeyError,  # upstream renamed or dropped a field
    ValueError,  # a row that would crash scripts/build.py (see normalise_event)
)


# ─── Provenance ranks ────────────────────────────────────────────────────────
#
# Every row records where its text and its timestamp came from, and a merge may
# only ever move a field UP this ladder. That is what lets the same file be a
# hand-curated seed and a fetch target at once: the tracker's paraphrase can
# never overwrite a verbatim post, and its second-precision post timestamp CAN
# overwrite a hand-typed midnight.

TEXT_POST = "post"  # fetched from tweet-result by id: the vendor's own words
TEXT_SEED = "seed"  # hand-curated in this repository, reviewed in a commit
TEXT_TRACKER = "tracker"  # claude-resets.com's own field for that row

# The two ladders are deliberately NOT the same ordering, because the two
# fields have different best sources and this was measured, not assumed:
#
#   text  post > seed > tracker.  The tracker's `note` is a summary written by
#         the tracker ("Reset 5-hour and weekly rate limits for all users"),
#         while the seed rows hold a hand copy of the post itself ("We've reset
#         everyone's 5-hour and weekly rate limits"). The email blockquotes
#         this field, so a summary must never displace a copy of the words.
#   date  post > tracker > seed.  The tracker's `date` IS a post timestamp to
#         the second — its row for 2095967323412930677 reads
#         2026-09-04T20:08:45Z and tweet-result returns created_at
#         2026-09-04T20:08:45.000Z for that id — while every hand-seeded date
#         is a midnight UTC placeholder the seed itself marked "approx".
TEXT_RANK = {TEXT_POST: 3, TEXT_SEED: 2, TEXT_TRACKER: 1}
DATE_RANK = {TEXT_POST: 3, TEXT_TRACKER: 2, TEXT_SEED: 1}

ORIGIN_TRACKER = "claude-resets.com"
ORIGIN_SEED = "seed"


def text_rank(value: Any) -> int:
    """Rank of a text's provenance. A cached row with NO source counts as seed.

    data/anthropic.json is both the seed and the merge target, and the project's
    own workflow invites hand-seeding it. A missing `text_source` therefore means
    "already on disk, and this fetcher did not write it" — hand-curated by
    definition. Ranking that 0 let the tracker's own summary overwrite a hand
    copy of the vendor's words on the very next five-minute tick.
    """
    key = str(value or "")
    return TEXT_RANK.get(key, TEXT_RANK[TEXT_SEED] if not key else 0)


def date_rank(value: Any) -> int:
    """Same rule as text_rank: an unlabelled cached date is a seed date."""
    key = str(value or "")
    return DATE_RANK.get(key, DATE_RANK[TEXT_SEED] if not key else 0)


# ─── Post verification (cdn.syndication.twimg.com) ───────────────────────────

TWEET_RESULT_URL = "https://cdn.syndication.twimg.com/tweet-result?id={id}&token=0"
POSTS_FILE = ROOT / "data" / "posts.json"

# Verification is capped per run and cached forever, so one id is fetched once.
# cdn.syndication.twimg.com is a courtesy endpoint with no published quota and
# this script runs on a five-minute cron; verifying all sixteen tracked ids on
# the first tick would be sixteen calls in one second for a backlog that has
# waited months. Three per run drains any backlog within the hour and settles
# at zero calls once every id is cached.
VERIFY_PER_RUN = 3
# A deleted or protected post never resolves. Without a ceiling it would eat
# the per-run budget on every tick forever and starve real new events.
VERIFY_MAX_ATTEMPTS = 5
VERIFY_RETRY_SECONDS = 6 * 3600


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def request_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (compatible; ai-resets/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def fetch() -> Any:
    return request_json(API_URL)


def fetch_post(post_id: str) -> Any:
    return request_json(TWEET_RESULT_URL.format(id=post_id))


def load_posts(path: Optional[Path] = None) -> dict:
    """The tweet-result cache: {"posts": {id: record}}.

    Deliberately NOT a data/<vendor>.json shape. build.py globs data/*.json and
    keys the result by each file's `vendor` field, so a cache carrying one
    would be read as an announcement seed and clobber the real anthropic entry.
    """
    # Resolved at call time, never as a default argument: a default would bind
    # the module constant once at import and quietly ignore a redirected
    # POSTS_FILE, which is how a test writes into the repository by accident.
    target = POSTS_FILE if path is None else path
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing or corrupt cache costs re-verification, nothing else.
        return {}
    posts = raw.get("posts") if isinstance(raw, dict) else None
    return posts if isinstance(posts, dict) else {}


def normalise_created_at(value: Any) -> Optional[str]:
    """`2026-09-01T18:35:27.000Z` -> `2026-09-01T18:35:27Z`, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_post_response(raw: Any) -> dict:
    """The four fields a verified post contributes, or a ValueError.

    Measured against live responses for 2094856679250919746 and
    2095967323412930677 on 2026-09-06: top-level `text`, `created_at`,
    `id_str`, `display_text_range`, `entities`, `user{screen_name,...}`, and NO
    in-reply-to keys at all when the post is not a reply.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"tweet-result payload is not an object: {type(raw).__name__}")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("tweet-result carried no text")
    created_at = normalise_created_at(raw.get("created_at"))
    if created_at is None:
        raise ValueError(f"tweet-result created_at unusable: {raw.get('created_at')!r}")
    user = raw.get("user")
    screen_name = user.get("screen_name") if isinstance(user, dict) else None
    if not isinstance(screen_name, str) or not screen_name.strip():
        raise ValueError("tweet-result carried no author")
    return {
        "text": text,
        "created_at": created_at,
        "screen_name": screen_name.strip(),
        # A reply is a conversation turn, not an announcement; the plan refuses
        # to treat one as trusted. Absent keys mean "not a reply".
        "is_reply": bool(
            raw.get("in_reply_to_screen_name") or raw.get("in_reply_to_status_id_str")
        ),
    }


def verify_ids(
    ids: Iterable[str],
    cache: dict,
    now: str,
    *,
    budget: int = VERIFY_PER_RUN,
    fetcher: Any = None,
) -> tuple[dict, int]:
    """Fill the cache for as many ids as the budget allows. Never raises.

    Returns the updated cache and how many network calls were spent. A failure
    is cached too, with an attempt count and a timestamp, so a deleted post
    stops being retried every five minutes.
    """
    call = fetcher or fetch_post
    spent = 0
    for post_id in ids:
        if spent >= budget:
            break
        record = cache.get(post_id)
        if isinstance(record, dict) and record.get("verified"):
            continue
        if isinstance(record, dict) and not _may_retry(record, now):
            continue
        spent += 1
        try:
            fields = read_post_response(call(post_id))
        except SOFT_ERRORS as exc:
            attempts = 0
            if isinstance(record, dict) and isinstance(record.get("attempts"), int):
                attempts = record["attempts"]
            cache[post_id] = {
                "verified": False,
                "attempts": attempts + 1,
                "last_attempt_at": now,
                "error": short_reason(exc),
            }
            print(f"post {post_id} not verified: {short_reason(exc)}")
            continue
        cache[post_id] = dict(fields, verified=True, fetched_at=now)
    return cache, spent


def _may_retry(record: dict, now: str) -> bool:
    attempts = record.get("attempts")
    if isinstance(attempts, int) and attempts >= VERIFY_MAX_ATTEMPTS:
        return False
    last = normalise_created_at(record.get("last_attempt_at"))
    moment = normalise_created_at(now)
    if last is None or moment is None:
        return True
    elapsed = datetime.fromisoformat(moment.replace("Z", "+00:00")) - datetime.fromisoformat(
        last.replace("Z", "+00:00")
    )
    return elapsed.total_seconds() >= VERIFY_RETRY_SECONDS


def pending_ids(events: list) -> list:
    """Numeric ids whose text is not yet the post's, newest announcement first.

    Newest first because the backlog is history and the next event is what a
    subscriber is about to be mailed about. Hand ids like
    `claudedevs-2026-07-10` are skipped: there is nothing to look up.
    """
    rows = [
        event
        for event in events
        if str(event.get("id", "")).isdigit()
        and text_rank(event.get("text_source")) < TEXT_RANK[TEXT_POST]
    ]
    rows.sort(key=lambda event: str(event.get("announced_at") or ""), reverse=True)
    return [str(event["id"]) for event in rows]


def apply_verification(event: dict, record: dict) -> dict:
    """Replace a tracker paraphrase with the vendor's own words.

    The author comes from the response, not the tracker: tweet-result is keyed
    by post id and names that post's real author, so when the two disagree the
    tracker attached the wrong handle. The disagreement is recorded rather than
    smoothed over — an announcer the feed got wrong is exactly the kind of
    thing the allow-list in scripts/incidents.py has to be told about.
    """
    updated = dict(event)
    screen_name = str(record.get("screen_name") or "")
    if screen_name and updated.get("announcer") and updated["announcer"].lower() != screen_name.lower():
        updated["announcer_mismatch"] = updated["announcer"]
    if screen_name:
        updated["announcer"] = screen_name
        updated["announcer_role"] = announcer_role(screen_name)
        # Rebuilt from verified facts rather than copied from the tracker,
        # which has been known to attach a URL pointing at a different post.
        updated["url"] = f"https://x.com/{screen_name}/status/{updated['id']}"
    updated["text"] = str(record.get("text") or updated.get("text") or "")
    updated["text_source"] = TEXT_POST
    updated["text_verified"] = True
    updated["post_is_reply"] = bool(record.get("is_reply"))
    created_at = record.get("created_at")
    if isinstance(created_at, str) and created_at:
        updated["announced_at"] = created_at
        updated["date_source"] = TEXT_POST
    updated["confidence"] = confidence_for(updated.get("date_source"))
    return updated


def confidence_for(date_source: Any) -> str:
    """`approx` only while the timestamp is still a hand-typed date.

    build.py renders this as "Timestamp confidence: approximate/observed", so
    it has to follow the timestamp's provenance and nothing else.
    """
    return "approx" if date_rank(date_source) < DATE_RANK[TEXT_TRACKER] else "high"


# ─── Normalisation ───────────────────────────────────────────────────────────


def parse_timestamp(value: Any) -> datetime:
    """Parse the way scripts/build.py does, and reject what it cannot USE.

    build.py compares this value against a timezone-aware `now`, so a naive
    stamp ("2026-09-03T12:00:00") or a date-only one ("2026-09-03") parses
    cleanly here and then raises "can't compare offset-naive and offset-aware
    datetimes" on every five-minute tick. Same guard, same reason, as
    fetch_openai.parse_timestamp.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"date is not a timestamp: {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"date has no timezone: {value!r}")
    return parsed


def _required_string(row: dict, field: str, identifier: Any) -> str:
    value = row[field]  # KeyError: upstream dropped the field entirely
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"event {identifier!r} has unusable {field}: {value!r}")
    return value


def _optional_string(row: dict, field: str, identifier: Any) -> str:
    value = row.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        # build.py runs html.escape() and re.sub() over these on every tick; a
        # dict or an int there is the 2026-08-27 whole-site freeze again.
        raise ValueError(
            f"event {identifier!r} has a non-string {field}: {type(value).__name__}"
        )
    return value


def normalise_event(raw_event: Any, fallback_announcer: str) -> dict:
    """Map one tracker row, rejecting anything scripts/build.py would choke on."""
    if not isinstance(raw_event, dict):
        # A reshaped feed (a list of ids, say) must land in the soft-failure
        # path like any other upstream break, not as an AttributeError.
        raise ValueError(f"event row is not an object: {raw_event!r}")

    identifier = raw_event.get("id")
    event_id = _required_string(raw_event, "id", identifier)
    url = _required_string(raw_event, "url", identifier)
    announced_at = _required_string(raw_event, "date", identifier)
    parse_timestamp(announced_at)
    note = _optional_string(raw_event, "note", identifier)
    scope = _optional_string(raw_event, "scope", identifier)
    kind = _optional_string(raw_event, "kind", identifier).strip().lower()

    announcer = announcer_from_url(url) or fallback_announcer
    event = {
        "id": event_id,
        # The tracker's summary, held only until tweet-result answers for this
        # id. build.py renders a missing note as an empty string, so an empty
        # one is a cosmetic upstream quirk and not a reason to freeze a column.
        "text": note,
        "url": url,
        "announced_at": announced_at,
        # An unlabelled row must claim nothing: incidents.event_kind maps
        # "unknown" to KIND_UNKNOWN, which gets a subject that asserts nothing.
        "kind": kind or "unknown",
        "scope": scope,
        "announcer": announcer,
        "announcer_role": announcer_role(announcer),
        "text_verified": False,
        "text_source": TEXT_TRACKER,
        "date_source": TEXT_TRACKER,
        "confidence": confidence_for(TEXT_TRACKER),
        "origin": ORIGIN_TRACKER,
        "in_tracker": True,
    }
    verification = _optional_string(raw_event, "verification", identifier)
    if verification:
        # The tracker's own word for how it learned the row ("curated"), kept
        # so a reader can see that a row nobody verified came from a human
        # typing it into a third-party site.
        event["upstream_verification"] = verification
    return event


def to_common_schema(raw: Any, fetched_at: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"upstream payload is not an object: {type(raw).__name__}")
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        raise ValueError(f"upstream providers is not an object: {type(providers).__name__}")
    provider = providers.get(PROVIDER_KEY)
    if not isinstance(provider, dict):
        # Losing the whole provider block is not "Anthropic announced nothing".
        raise ValueError(f"upstream carries no {PROVIDER_KEY} provider")
    raw_events = provider.get("events") or []
    if not isinstance(raw_events, list):
        raise ValueError(f"upstream events is not a list: {type(raw_events).__name__}")

    fallback = announcer_from_url(provider.get("accountUrl")) or ""
    events = [normalise_event(event, fallback) for event in raw_events]
    events.sort(key=lambda event: event["announced_at"])

    source = {
        "name": SOURCE_NAME,
        "url": SOURCE_URL,
        "note": SOURCE_NOTE,
        # No stale_since key on a fresh read: a successful fetch clears the
        # marker just by rebuilding the source block from scratch.
    }
    detector = (raw.get("meta") or {}).get("detector") if isinstance(raw.get("meta"), dict) else None
    if isinstance(detector, dict) and isinstance(detector.get("status"), str):
        # The evidence for calling this feed hand-curated: it reported
        # "degraded" with lastSuccessfulCheckAt null on 2026-09-07.
        source["upstream_detector_status"] = detector["status"]
    return {
        "vendor": "anthropic",
        "source": source,
        "fetched_at": fetched_at,
        "events": events,
    }


# ─── Merge ───────────────────────────────────────────────────────────────────

# Fields the tracker owns outright: its classification of the row, refreshed on
# every read so an upstream correction reaches the site.
TRACKER_OWNED = ("kind", "scope", "upstream_verification")


def merge_events(cached: list, fetched: list, *, now: str | None = None) -> list:
    """Union by id: nothing on disk is deleted, nothing is downgraded.

    The tracker is not a superset of the truth — it carries the 2026-09-04
    reset and misses the 2026-09-01 one — so a hand-seeded row has to survive
    every fetch. Within a row the ladder in SOURCE_RANK decides each field: a
    verified post beats a hand seed beats a tracker summary, and equal ranks
    take the newer value so an upstream correction still lands.
    """
    seen_now = now or utc_now()
    merged: dict[str, dict] = {}
    for event in cached:
        if isinstance(event, dict) and str(event.get("id") or ""):
            row = dict(event)
            # Recomputed from this read, so a row the tracker has stopped
            # carrying is visible instead of silently counted forever.
            row["in_tracker"] = False
            # A row already on disk predates this field. Its announcement time
            # is the honest answer: it was seeded, not discovered, and stamping
            # it with `now` would tell derive_status that twenty old
            # announcements were all first seen today — every one of them
            # mailable, which is the flood the age cutoff exists to prevent.
            row.setdefault("first_seen_at", row.get("announced_at"))
            merged[str(event["id"])] = row

    for event in fetched:
        key = str(event["id"])
        existing = merged.get(key)
        if existing is None:
            # Genuinely new to this file, so this read IS the first sighting.
            # Without it derive_status falls back to the announcement time and
            # every trusted row on disk reads as `historical`, which removes
            # the announced -> mailable path entirely.
            merged[key] = {**event, "first_seen_at": seen_now}
            continue
        row = dict(existing)
        row["in_tracker"] = True
        if text_rank(event.get("text_source")) >= text_rank(row.get("text_source")):
            row["text"] = event["text"]
            row["text_source"] = event["text_source"]
            row["text_verified"] = event["text_verified"]
        if date_rank(event.get("date_source")) >= date_rank(row.get("date_source")):
            row["announced_at"] = event["announced_at"]
            row["date_source"] = event["date_source"]
        if text_rank(row.get("text_source")) < TEXT_RANK[TEXT_POST]:
            # A verified row's URL was rebuilt from its verified author; only an
            # unverified row still takes the tracker's link, which is how a
            # hand row with a bare profile URL gains a real status link.
            row["url"] = event["url"]
            row["announcer"] = event["announcer"]
            row["announcer_role"] = event["announcer_role"]
        for field in TRACKER_OWNED:
            if field in event:
                row[field] = event[field]
        row["confidence"] = confidence_for(row.get("date_source"))
        merged[key] = row

    events = list(merged.values())
    events.sort(key=lambda event: str(event.get("announced_at") or ""))
    return events


def tracked_by_upstream(events: Iterable[Any]) -> int:
    return sum(1 for event in events if isinstance(event, dict) and event.get("in_tracker"))


# ─── Keep-last-good ──────────────────────────────────────────────────────────


def short_reason(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())
    if len(detail) > 160:
        detail = detail[:157] + "..."
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def load_existing() -> Optional[dict]:
    """Return the cached payload, or None if there isn't a usable one."""
    try:
        cached = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        # An unreadable seed is not an absent one. Saying "no cached file"
        # while the hand-curated history sits on disk sends the operator
        # looking for the wrong problem, and build.py is about to skip that
        # same file.
        print(f"cached {OUT_FILE.name} unreadable: {short_reason(exc)}")
        return None
    return cached if isinstance(cached, dict) else None


def payload_differs(cached: Any, fresh: dict) -> bool:
    """Is there anything here a reader would notice, ignoring `fetched_at`?

    `fetched_at` moves on every tick by construction. Everything else — the
    events, their verification state, the source block — changes only when
    upstream does or when a post is newly verified.
    """
    if not isinstance(cached, dict):
        return True
    strip = lambda payload: {k: v for k, v in payload.items() if k != "fetched_at"}
    return json.dumps(strip(cached), sort_keys=True) != json.dumps(
        strip(fresh), sort_keys=True
    )


def write_json(path: Path, payload: dict) -> None:
    """Write via a temp file + os.replace so a reader never sees a half file.

    build.py globs data/*.json on a five-minute cron and json.loads every hit;
    a torn write would crash the build on the next tick. A per-process temp
    name keeps the cron and an ad-hoc manual run from colliding, and the .tmp
    suffix cannot match that glob either.
    """
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def soft_fail(reason: str) -> int:
    """Report the failure, keep the cached file, stamp stale_since once."""
    print(f"FETCH FAILED claude-resets.com: {reason}")
    cached = load_existing()
    if cached is None:
        print("no cached data/anthropic.json — anthropic events absent from this build")
        return 0

    source = cached.get("source")
    if not isinstance(source, dict):
        source = {}
        cached["source"] = source
    stale_since = source.get("stale_since")
    if not stale_since:
        # First failure of this outage only. Re-stamping every five minutes
        # would erase how long the column has actually been stale, which is the
        # one number the site and the owner alert need.
        stale_since = utc_now()
        source["stale_since"] = stale_since
        try:
            write_json(OUT_FILE, cached)
        except OSError as exc:
            # A local disk problem must not turn a soft failure into a hard
            # one; the file on disk is still the good one.
            print(f"could not stamp stale_since: {short_reason(exc)}")
    print(
        f"keeping {len(cached.get('events') or [])} cached anthropic events, "
        f"stale since {stale_since}"
    )
    return 0


# ─── Entry point ─────────────────────────────────────────────────────────────


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stamp-stale",
        action="store_true",
        help="mark the cached file stale without fetching (infra/publish.sh uses "
        "this when this script itself died hard)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip cdn.syndication.twimg.com entirely; merge the tracker only",
    )
    parser.add_argument(
        "--verify-budget",
        type=int,
        default=VERIFY_PER_RUN,
        help=f"post verifications per run (default {VERIFY_PER_RUN})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.stamp_stale:
        # Called by infra/publish.sh when THIS script died hard (a syntax
        # error, a missing interpreter, a full disk). Without it the tick
        # publishes an ever-staler Anthropic column with no marker anywhere.
        return soft_fail("fetcher did not complete")

    now = utc_now()
    try:
        fetched = to_common_schema(fetch(), now)
    except SOFT_ERRORS as exc:
        return soft_fail(short_reason(exc))

    cached = load_existing()
    cached_events = (cached or {}).get("events") or []
    lost = tracked_by_upstream(cached_events) - len(fetched["events"])
    if tracked_by_upstream(cached_events) and (not fetched["events"] or lost > MAX_SHRINK_ROWS):
        # The feed appends: an announcement, once posted, stays. A response
        # with far fewer rows is upstream being broken. The merge would not
        # DELETE anything here, but it would silently mark most of the history
        # as no longer tracked and accept a truncated body as the new truth.
        return soft_fail(
            f"upstream returned {len(fetched['events'])} events, cache holds "
            f"{tracked_by_upstream(cached_events)} tracked"
        )
    if lost > 0:
        print(f"upstream dropped {lost} tracked event(s) since the last read; accepting")

    events = merge_events(cached_events if isinstance(cached_events, list) else [], fetched["events"])

    if not args.no_verify and args.verify_budget > 0:
        posts = load_posts()
        posts, spent = verify_ids(pending_ids(events), posts, now, budget=args.verify_budget)
        by_id = {str(event.get("id")): event for event in events}
        for post_id, record in posts.items():
            event = by_id.get(str(post_id))
            if event is not None and isinstance(record, dict) and record.get("verified"):
                by_id[str(post_id)] = apply_verification(event, record)
        events = sorted(by_id.values(), key=lambda event: str(event.get("announced_at") or ""))
        try:
            write_json(POSTS_FILE, {"posts": posts, "updated_at": now})
        except OSError as exc:
            # The cache is an optimisation. Losing it costs re-verification on
            # the next tick and nothing else, so it never fails the run.
            print(f"could not write {POSTS_FILE.name}: {short_reason(exc)}")
        if spent:
            # "checked", not "verified": the count is network calls spent, and a
            # call that came back 404 is still a call. Each failure already
            # printed its own line naming the id.
            print(f"checked {spent} post id(s) against cdn.syndication.twimg.com")

    payload = dict(fetched, events=events)
    # data/anthropic.json is TRACKED, and this runs from a five-minute cron in
    # the deployed checkout. Rewriting it every tick for a new `fetched_at`
    # alone would leave that checkout permanently dirty and make the next
    # `git pull --ff-only` deploy fail. Compare everything EXCEPT that stamp,
    # and skip the write when nothing a reader would notice has changed.
    if not payload_differs(load_existing(), payload):
        print(
            f"anthropic feed unchanged ({len(events)} events); "
            "not rewriting the tracked file"
        )
        return 0
    write_json(OUT_FILE, payload)
    # Counted from what is actually on disk, not from this run's verifications:
    # under --no-verify the line used to read "0 text-verified" while four rows
    # on disk carried the vendor's own words, which is the opposite of what an
    # operator reading the publish log needs to know.
    verified_count = sum(1 for event in events if event.get("text_verified"))
    print(
        f"wrote {len(events)} anthropic events ({verified_count} text-verified, "
        f"{tracked_by_upstream(events)} in the tracker) -> {OUT_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
