#!/usr/bin/env python3
"""Pull OpenAI Codex/ChatGPT reset events from codex-resets.com's public API.

Source: https://codex-resets.com/api/resets — unauthenticated JSON, tracks
@thsottiaux's reset announcements. Credited in the site footer.

Soft-failure contract (P0d). codex-resets.com is a third-party tracker on the
public internet and it goes away regularly: publish.log carries 52 tracebacks
raised by this one script — SSL handshake timeouts, bare read timeouts on the
response body, and `socket.gaierror: [Errno -3] Temporary failure in name
resolution`. Because infra/publish.sh ran under `set -e` with this script
first, each of those aborted the whole tick, so Anthropic and Google went
stale on the live site for a tracker outage that had nothing to do with them.

Every fetch/parse failure here therefore keeps the last good
data/openai.json, stamps `source.stale_since` once, prints one line and
exits 0. The caller goes on to build and publish the other vendors.
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ─── Upstream contract ───

API_URL = "https://codex-resets.com/api/resets"
OUT_FILE = Path(__file__).resolve().parent.parent / "data" / "openai.json"
TIMEOUT_SECONDS = 15

# How many events the feed may lose in one read before the payload is refused.
# Upstream has twice removed a single bad row; a truncated body once cut 52
# events to 1.
MAX_SHRINK_ROWS = 3

# urllib.error.URLError and json.JSONDecodeError are already OSError and
# ValueError subclasses; all five are named anyway so the failure modes this
# guard exists for stay readable next to the tracebacks in publish.log.
SOFT_ERRORS = (
    urllib.error.URLError,  # DNS failure, refused connection, TLS handshake timeout
    OSError,  # bare TimeoutError from r.read(), after urlopen already returned
    json.JSONDecodeError,  # an HTML error page or a truncated body
    KeyError,  # upstream renamed or dropped a field
    ValueError,  # a row that would crash scripts/build.py (see normalise_event)
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch() -> Any:
    req = urllib.request.Request(
        API_URL,
        headers={
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (compatible; ai-resets/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        return json.loads(r.read())


# ─── Normalisation ───


def parse_timestamp(value: Any) -> datetime:
    """Parse the way scripts/build.py does, and reject what it cannot USE.

    Parsing is not the whole requirement. build.py compares this value against
    a timezone-aware `now` (build.py: `generated - latest_at`, and the 30-day
    recency window), so a naive timestamp — "2026-09-03T12:00:00", or a
    date-only "2026-09-03" — parses cleanly here and then raises
    "can't compare offset-naive and offset-aware datetimes" on every
    five-minute tick. Measured against the real build.py, which exits 1.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"announced_at is not a timestamp: {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"announced_at has no timezone: {value!r}")
    return parsed


def normalise_event(raw_event: dict) -> dict:
    """Map one upstream row, rejecting anything scripts/build.py would choke on.

    On 2026-08-27 upstream served event 2093014447833116908 with a null
    `tweet_url`. The fetch succeeded, the null went straight into
    data/openai.json, and build.py then died in html.escape() on 40
    consecutive ticks (publish.log; about 3.3 hours of the five-minute cron)
    until upstream filled the field in by itself. Nothing else on the site
    could update in that window, so a row that build.py cannot render is
    treated exactly like a broken fetch: the payload is refused whole and the
    last good file is kept intact, which also means the row comes back
    complete once upstream fixes it instead of silently vanishing from the
    published history.
    """
    if not isinstance(raw_event, dict):
        # A reshaped feed (a list of ids, say) must land in the soft-failure
        # path like any other upstream break, not as an AttributeError.
        raise ValueError(f"event row is not an object: {raw_event!r}")

    identifier = raw_event.get("tweet_id")
    for field in ("tweet_id", "tweet_url"):
        value = raw_event[field]  # KeyError: upstream dropped the field entirely
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"event {identifier!r} has unusable {field}: {value!r}")
    parse_timestamp(raw_event["announced_at"])

    text = raw_event.get("text")
    if text is not None and not isinstance(text, str):
        # build.py's clean_event_text runs re.sub over this value; an int or an
        # object raises TypeError there on every tick, which is the same
        # whole-site freeze as the 2026-08-27 null tweet_url. Missing, None and
        # "" stay cosmetic as designed; a wrong TYPE refuses the payload.
        raise ValueError(f"event {identifier!r} has a non-string text: {type(text).__name__}")

    event = {
        "id": raw_event["tweet_id"],
        # build.py renders a missing/None text as an empty string, so an empty
        # text is a cosmetic upstream quirk, not a reason to freeze the column.
        "text": text or "",
        "url": raw_event["tweet_url"],
        "announced_at": raw_event["announced_at"],
        "kind": "reset",
    }
    # Upstream distinguishes how it learned of an event (webhook / backfill
    # / observed) and what kind of reset it was. Both were being dropped.
    # "observed" records are the unreliable ones: their announced_at is a
    # hand-rounded estimate rather than a post timestamp, and at least one
    # has a tweet_url pointing at an unrelated later reply.
    for optional in ("source", "reset_type"):
        if raw_event.get(optional) is not None:
            event[f"upstream_{optional}"] = raw_event[optional]
    return event


def to_common_schema(raw: dict, fetched_at: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"upstream payload is not an object: {type(raw).__name__}")
    raw_events = raw.get("events") or []
    if not isinstance(raw_events, list):
        raise ValueError(f"upstream events is not a list: {type(raw_events).__name__}")

    events = [normalise_event(event) for event in raw_events]
    events.sort(key=lambda e: e["announced_at"])
    return {
        "vendor": "openai",
        "source": {
            "name": "codex-resets.com",
            "url": "https://codex-resets.com/",
            "note": "Tracks @thsottiaux's reset announcements on X.",
            # No stale_since key on a fresh read: a successful fetch clears the
            # marker just by rebuilding the source block from scratch.
        },
        # The API exposes no generation timestamp — the previous
        # raw.get("generated_at") always resolved to null, so the site has
        # been shipping "fetched_at": null since launch. Stamp our own read.
        "fetched_at": fetched_at,
        "events": events,
    }


# ─── Keep-last-good ───


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
        # An unreadable cache is not an absent one. Saying "no cached file"
        # while 52 events sit on disk sends the operator looking for the wrong
        # problem, and build.py is about to die on that same file.
        print(f"cached {OUT_FILE.name} unreadable: {short_reason(exc)}")
        return None
    return cached if isinstance(cached, dict) else None


def write_json(path: Path, payload: dict) -> None:
    """Write via a temp file + os.replace so a reader never sees a half file.

    build.py globs data/*.json on a five-minute cron and json.loads every hit;
    a torn write would crash the build on the next tick. The temp name ends in
    .tmp so it cannot match that glob either.
    """
    # A per-process temp name: the five-minute cron and an ad-hoc manual run
    # (how this script is actually debugged) would otherwise collide on one
    # fixed name and one of them would take a FileNotFoundError out of
    # os.replace. The suffix still cannot match build.py's data/*.json glob.
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def soft_fail(reason: str) -> int:
    """Report the failure, keep the cached file, stamp stale_since once."""
    print(f"FETCH FAILED codex-resets.com: {reason}")
    cached = load_existing()
    if cached is None:
        # First run, or a cache we cannot parse. Creating a file here would
        # mean publishing an empty OpenAI history as if it were the record.
        print("no cached data/openai.json — openai events absent from this build")
        return 0

    source = cached.get("source")
    if not isinstance(source, dict):
        source = {}
        cached["source"] = source
    stale_since = source.get("stale_since")
    if not stale_since:
        # First failure of this outage only. Re-stamping every five minutes
        # would erase how long the column has actually been stale, which is
        # the one number the site and the owner alert need.
        stale_since = utc_now()
        source["stale_since"] = stale_since
        try:
            write_json(OUT_FILE, cached)
        except OSError as exc:
            # A local disk problem must not turn a soft failure into a hard
            # one; the cached file on disk is still the good one.
            print(f"could not stamp stale_since: {short_reason(exc)}")
    print(f"keeping {len(cached.get('events') or [])} cached openai events, stale since {stale_since}")
    return 0


# ─── Entry point ───


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--stamp-stale" in argv:
        # Called by infra/publish.sh when THIS script died hard (a syntax
        # error, a missing interpreter, a full disk). Without it the tick
        # publishes an ever-staler OpenAI column with no marker anywhere, and
        # the only trace is one echo into a cron log with MAILTO="".
        return soft_fail("fetcher did not complete")

    try:
        raw = fetch()
        data = to_common_schema(raw, utc_now())
    except SOFT_ERRORS as exc:
        return soft_fail(short_reason(exc))

    cached = load_existing()
    cached_events = (cached or {}).get("events") or []
    lost = len(cached_events) - len(data["events"])
    if cached_events and (not data["events"] or lost > MAX_SHRINK_ROWS):
        # This feed appends: an announcement, once posted, stays. A response
        # with far FEWER events than the cache is upstream being broken, and
        # writing it destroys published history rather than merely freezing it
        # — a truncated body once turned a 52-event cache into a 1-event file
        # with no failure line, no stale_since and exit 0.
        #
        # A small shrink is real, though: upstream has twice removed a single
        # bad row (a mis-ingested reply, a post it retracted). Refusing those
        # forever would freeze the column on a legitimate correction, so a few
        # rows are tolerated and anything larger is refused and stamped.
        return soft_fail(
            f"upstream returned {len(data['events'])} events, cache holds "
            f"{len(cached_events)}"
        )
    if lost > 0:
        # Accepted, but never silent: this is published history disappearing.
        print(f"upstream dropped {lost} event(s) since the last read; accepting")

    write_json(OUT_FILE, data)
    print(f"wrote {len(data['events'])} openai events -> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
