#!/usr/bin/env python3
"""Build the public data feed and crawlable HTML snapshots.

The interactive UI still recomputes rolling statistics against the viewer's
actual time. This builder also writes a compact server-rendered snapshot so
search engines, social crawlers, and text-only clients can read the current
signals without executing JavaScript.
"""

import hashlib
import html
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # Run by cron as an absolute path, which puts scripts/ on sys.path rather
    # than the repository root.
    sys.path.insert(0, str(ROOT))

from scripts import attribution  # noqa: E402
from scripts import groundtruth  # noqa: E402
from scripts import incidents  # noqa: E402
from scripts.timefmt import (  # noqa: E402
    PACIFIC,
    describe_age,
    format_pacific,
    parse_timestamp,
)

DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
OUT_FILE = SITE_DIR / "data.json"
INDEX_TEMPLATE = SITE_DIR / "index.template.html"
INDEX_FILE = SITE_DIR / "index.html"
SITEMAP_FILE = SITE_DIR / "sitemap.xml"
ANALYTICS_TEMPLATE = SITE_DIR / "analytics.template.js"
ANALYTICS_FILE = SITE_DIR / "analytics.js"
SITE_CONFIG_FILE = ROOT / "site.config.json"


def load_site_config(path=None):
    """Who this deployment is, read from one tracked file.

    A fork that changes nothing else still publishes correct canonical URLs and
    schema, and a fork that forgets gets a loud KeyError here rather than a site
    quietly claiming to be someone else's.
    """
    config = json.loads(Path(path or SITE_CONFIG_FILE).read_text(encoding="utf-8"))
    missing = [k for k in ("public_url", "maintainer_name", "maintainer_url") if not config.get(k)]
    if missing:
        raise SystemExit(f"site.config.json is missing {', '.join(missing)}")
    # Every f-string below appends a path directly, so the trailing slash is
    # part of the contract rather than the caller's problem.
    config["public_url"] = config["public_url"].rstrip("/") + "/"
    return config


SITE_CONFIG = load_site_config()
PUBLIC_URL = SITE_CONFIG["public_url"]
MAINTAINER_NAME = SITE_CONFIG["maintainer_name"]
MAINTAINER_URL = SITE_CONFIG["maintainer_url"]

VENDOR_META = {
    "anthropic": ("Anthropic", "Claude Code"),
    "openai": ("OpenAI", "Codex / ChatGPT"),
    "google": ("Google", "Gemini CLI / Code Assist"),
}
VENDOR_ORDER = ("anthropic", "openai", "google")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
PERIODS = (
    ("Overnight", 0, 6),
    ("Morning", 6, 12),
    ("Afternoon", 12, 18),
    ("Evening", 18, 24),
)


# ─── Own-account observations ────────────────────────────────────────────────
#
# The probe has computed a verdict for every counter clear since 2026-08-31 and
# nothing on the site ever read it. This is the export boundary: the probe's
# internal CLASS_* names stay internal, and only the vocabulary below is ever
# published.

OBSERVED_DIR = DATA_DIR / "observed"

# The public verdicts, in the words a reader sees. `self_applied` and
# `credit_granted` are deliberately absent: they say what the OWNER did with
# their own account, not what the vendor did, and are never published. So are
# `unresolved`, `unattributed` (one account, however clean) and
# `account_change`. `vendor_reset` is back, but only as scripts/attribution.py
# produces it: two of our accounts clearing within the hour.
VERDICT_LABELS = {
    "natural_expiry": "Scheduled expiry",
    "limit_change": "Limits rescaled",
    "vendor_reset": "Vendor reset · two accounts",
}
VERDICT_VENDOR_RESET = "vendor_reset"
# Reject stale exports too: an older exporter may still have marked an early
# account clear public, and its legacy vendor_reset verdict names no witness.
# A vendor_reset row is admitted only with the second account's row beside it
# (witnessed()), which no exporter before scripts/attribution.py could write.
PUBLIC_VERDICTS = frozenset(VERDICT_LABELS)

# Whitelists, not blacklists. The probe export is written by another program on
# its own schedule; when it grows a field nobody here has reviewed (an internal
# reason string naming the credit bank, a filesystem path in `last_error`) the
# default must be that it does NOT reach site/data.json.
# `detectable_now` is deliberately NOT here: nothing renders it, and its keys
# are raw internal slot identifiers (a vendor's own rollout labels) that the
# public feed has no reason to carry. It stays in data/observed/, which is the
# file the owner reads when the probe itself is what is wrong.
PROBE_FIELDS = ("status", "last_verified_at", "coverage")
OBSERVATION_FIELDS = (
    "observed_at",
    "window",
    "verdict",
    "headline",
    "evidence",
    "used_before",
    "used_after",
    "early_by_hours",
    "observed_before",
    "observed_after",
)

# 15 minutes, the same threshold the owner alert uses for "probe blind", so the
# badge can never say "verified" while the operator is being paged about
# silence on the very same probe.
PROBE_STALE_SECONDS = 900
OBSERVATIONS_SHOWN = 6
DEFAULT_COVERAGE = (
    "Own-account probe; see the methodology for what it can and cannot see."
)
NO_ANNOUNCEMENTS_HTML = (
    '<p class="static-event-copy">No tracked announcements for this provider '
    "yet. The lines below are what our own account measured.</p>"
)


# A Claude model-scoped event id: `claude:<account>:weekly_scoped:<model
# slug>/<minutes>:<epoch>`, the slot key claude_probe writes plus the moment.
SCOPED_EVENT_ID = re.compile(
    r"^(?P<head>claude:[^:/]+:weekly_scoped):(?P<slug>[^/]+)/(?P<tail>[^/]+)$"
)


def _digest(text):
    return f"obs-{hashlib.sha256(f'ai-resets/observation:{text}'.encode()).hexdigest()[:12]}"


def published_ids(raw_ids):
    """raw event id -> `obs-<12 hex>`, a stable public name for each record.

    The raw event id carries the probe's slot key, and a Claude model-scoped
    slot key carries a slug of the model's display name, which on 2026-09-06
    was an unreleased codename. The feed needs a stable id per row (it is the
    notifier's event key) and nothing more, so it gets a digest.

    A digest of the raw id was not enough: every other part of it (account,
    window shape, time) is on the public row, so anyone could hash a guessed
    codename and compare (privacy review). A scoped id is digested with its
    slug replaced by an ordinal among that account's scoped windows clearing
    at the same moment, sorted by slug, so the digest holds no model name.
    Every id without a model digests exactly as it always has, so no event
    key production has filed moves.
    """
    raw_ids = [raw for raw in raw_ids if isinstance(raw, str)]
    slugs = {}
    for raw in raw_ids:
        match = SCOPED_EVENT_ID.match(raw)
        if match:
            slugs.setdefault((match["head"], match["tail"]), set()).add(match["slug"])
    out = {}
    for raw in raw_ids:
        match = SCOPED_EVENT_ID.match(raw)
        if match is None:
            out[raw] = _digest(raw)
            continue
        ordinal = sorted(slugs[(match["head"], match["tail"])]).index(match["slug"])
        out[raw] = _digest(f"{match['head']}:#{ordinal}/{match['tail']}")
    return out


def published_id(raw_id):
    """One record's public id, on its own (published_ids, alone in its set)."""
    return published_ids([raw_id])[raw_id]


def witnessed(item, vendor_rows):
    """Is this vendor_reset row backed by its second account's row?

    The row must carry scripts/attribution.py's stamp, and the witness it names
    must be another row of the same export that is itself a public, stamped
    vendor_reset on a different account. A stale or hand-edited export that
    says `vendor_reset` without all of that is dropped, which is the gate that
    kept 2026-09-30's legacy rows private and still does.
    """
    if not attribution.is_vendor_reset(item):
        return False
    other = vendor_rows.get(item.get("witness"))
    # A row naming itself already fails is_vendor_reset() above.
    return (
        other is not None
        and other.get("public") is True
        and attribution.is_vendor_reset(other)
        and other.get("account") != item.get("account")
    )


def public_observations(raw):
    """The rows that may be shown, newest first.

    Two independent gates, because publishing a `self_applied` clear would tell
    the world what the owner spent their own banked credit on: the exporter's
    `public` flag AND the verdict itself. Either one alone failing is enough to
    drop the row. A `vendor_reset` has a third: its witness must be in the
    same export (witnessed()).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        # Every other malformed shape is tolerated; a number here used to raise
        # TypeError out of build.py and abort the tick before the rsync.
        print("skipping observations: not a list")
        return []
    vendor_rows = {
        item["event_id"]: item
        for item in raw
        if isinstance(item, dict)
        and item.get("verdict") == VERDICT_VENDOR_RESET
        and isinstance(item.get("event_id"), str)
    }
    # Ordinals are counted over the whole export, private rows included, so
    # a row's public id does not depend on which of its neighbours is public.
    public_ids = published_ids(
        [item.get("event_id") for item in raw if isinstance(item, dict)]
        + [item.get("witness") for item in raw if isinstance(item, dict)]
    )
    rows = []
    for item in raw:
        if not isinstance(item, dict) or item.get("public") is not True:
            continue
        verdict = item.get("verdict")
        if verdict not in PUBLIC_VERDICTS:
            continue
        if verdict == VERDICT_VENDOR_RESET and not witnessed(item, vendor_rows):
            continue
        if not (item.get("headline") or item.get("evidence")):
            # Nothing a reader could see. A row with no sentence on it is a
            # blank bullet, not evidence.
            continue
        row = {field: item[field] for field in OBSERVATION_FIELDS if field in item}
        row["verdict"] = verdict
        if verdict == VERDICT_VENDOR_RESET:
            # What the notifier and the fusion layer need to treat the pair as
            # one incident: a stable id per row, the row it is paired with,
            # and which of our accounts it was. Labels only, never a
            # fingerprint. Other verdicts gain none of this, so the event keys
            # production already filed for them do not move.
            row["event_id"] = public_ids[item["event_id"]]
            row["witness"] = public_ids[item["witness"]]
            row["account"] = item.get("account")
        rows.append(row)
    # ISO-8601 Z strings sort lexicographically in chronological order, so this
    # needs no parsing and cannot raise on a malformed timestamp.
    rows.sort(key=lambda row: str(row.get("observed_at") or ""), reverse=True)
    return rows


def probe_age_seconds(probe, generated):
    value = probe.get("last_verified_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        verified = parse_timestamp(value)
    except ValueError:
        return None
    return max(0.0, (generated - verified).total_seconds())


def probe_badge(probe, generated, observations=None):
    """`(text, state)` for the vendor card's status pill.

    The card used to show a hardcoded "Tracking" for every vendor, which let
    Anthropic and Google borrow the credibility of the one vendor we actually
    measure. Each card now says what it really is.
    """
    if not isinstance(probe, dict):
        return ("Announcements only", "none")
    status = probe.get("status")
    if status not in ("ok", "blind", "absent"):
        return ("Announcements only", "none")
    if status == "absent":
        if not observations:
            # A probe that ships but has never reported, on a card with nothing
            # to show for it. Claiming "ground truth" here would promise a
            # measurement the card does not contain.
            return ("Announcements only", "none")
        # We DO have observations from this probe but no health file to say
        # whether it is still running — a rotated file, or a site built on a
        # host that is not the probe host. "Announcements only" would claim
        # there is no probe at all, directly above its own measurements.
        return ("Ground truth · probe state unknown", "offline")
    age = probe_age_seconds(probe, generated)
    if age is None:
        return ("Ground truth · probe state unknown", "offline")
    if probe.get("status") == "ok" and age <= PROBE_STALE_SECONDS:
        return (f"Ground truth · verified {describe_age(age)} ago", "verified")
    return (f"Ground truth · probe offline {describe_age(age)}", "offline")


def observation_time(value):
    """Pacific timestamp, or "" when the row carries nothing parseable."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return format_pacific(value)
    except ValueError:
        return ""


def observation_note(probe, total, shown):
    """The population sentence, taken from the probe's own coverage line.

    Never hardcoded here: "one Codex Pro account, weekly window only" was
    true of the Codex probe until it read a second account, and a sentence
    typed here would have gone on saying it. The exporter writes the
    sentence; this only frames it.
    """
    parts = []
    if total > shown:
        parts.append(f"Showing the {shown} most recent of {total}.")
    coverage = probe.get("coverage") if isinstance(probe, dict) else None
    parts.append(coverage.strip() if isinstance(coverage, str) and coverage.strip() else DEFAULT_COVERAGE)
    return (
        f'<p class="observation-note">{html.escape(" ".join(parts))} '
        '<a href="methodology.html#ground-truth">How a reset is told from an expiry ↗</a></p>'
    )


def renderable_observations(rows):
    """The rows a renderer may draw: a known label, and a vendor_reset only
    beside the row it names as its witness.

    public_observations() already enforces both; this is the renderer's own
    lock, so a row handed to it by any other route still cannot draw a
    "Vendor reset" on one account's word.
    """
    rows = [row for row in rows or [] if isinstance(row, dict)]
    ids = {
        row.get("event_id")
        for row in rows
        if row.get("verdict") == VERDICT_VENDOR_RESET and isinstance(row.get("event_id"), str)
    }
    return [
        row
        for row in rows
        if row.get("verdict") in VERDICT_LABELS
        and (
            row.get("verdict") != VERDICT_VENDOR_RESET
            or (row.get("witness") in ids and row.get("witness") != row.get("event_id"))
        )
    ]


def build_observations(vendor):
    """The static twin of renderObservations() in site/app.js.

    Both sides render the same classes, the same `data-verdict` values and the
    same label vocabulary; tests/test_build.py fails if one side grows a term
    the other does not have.
    """
    rows = renderable_observations(vendor.get("observations"))
    if not rows:
        return ""
    shown = rows[:OBSERVATIONS_SHOWN]
    items = []
    for row in shown:
        verdict = row["verdict"]
        stamp = observation_time(row.get("observed_at"))
        time_html = (
            f'<time datetime="{html.escape(str(row.get("observed_at")), quote=True)}">'
            f"{html.escape(stamp)}</time>"
            if stamp
            else ""
        )
        lines = ""
        if row.get("headline"):
            lines += f'<p class="observation-headline">{html.escape(str(row["headline"]))}</p>'
        if row.get("evidence"):
            lines += f'<p class="observation-evidence">{html.escape(str(row["evidence"]))}</p>'
        items.append(
            f'<li class="observation" data-verdict="{html.escape(verdict, quote=True)}">'
            f'<div class="observation-head"><span class="observation-verdict">'
            f"{html.escape(VERDICT_LABELS[verdict])}</span>{time_html}</div>{lines}</li>"
        )
    return (
        '<div class="observations">'
        '<p class="observations-label">Our own account</p>'
        f'<ol class="observation-list">{"".join(items)}</ol>'
        f"{observation_note(vendor.get('probe') or {}, len(rows), len(shown))}</div>"
    )


def clean_event_text(value):
    without_urls = re.sub(r"https?://\S+", "", value or "")
    return re.sub(r"\s+", " ", without_urls).strip()


def format_event_timestamp(event):
    if event.get("confidence") == "approx":
        occurred = parse_timestamp(event["announced_at"])
        return f"~{occurred.strftime('%b')} {occurred.day}, {occurred.year}"
    return format_pacific(event["announced_at"])


def replace_marker(document, name, content):
    pattern = re.compile(
        rf"<!-- STATIC_{re.escape(name)}_START -->.*?<!-- STATIC_{re.escape(name)}_END -->",
        re.DOTALL,
    )
    replacement = f"<!-- STATIC_{name}_START -->{content}<!-- STATIC_{name}_END -->"
    updated, count = pattern.subn(lambda _match: replacement, document)
    if count != 1:
        raise RuntimeError(f"expected one STATIC_{name} marker pair, found {count}")
    return updated


def build_announcement_body(vendor, events, generated):
    """The announcement half of a vendor card: unchanged, and deliberately so.

    Observations never enter this block, `timing_stats`, or "Last tracked
    move". Those describe when a vendor SAID something; an observation
    describes what happened on our account, and mixing the two would inflate
    the announcement counts with events nobody announced.
    """
    latest = events[-1]
    latest_at = parse_timestamp(latest["announced_at"])
    age_days = max(0, (generated - latest_at).total_seconds() / 86400)
    age = f"{round(age_days * 24)}h" if age_days < 1 else f"{age_days:.1f}d"
    source = vendor.get("source") or {}
    source_html = ""
    if source.get("url") and source.get("name"):
        source_html = (
            '<p class="static-source">Source: '
            f'<a href="{html.escape(source["url"], quote=True)}" rel="noopener">'
            f'{html.escape(source["name"])}</a></p>'
        )
    return f'''<div class="primary-stat">
    <div class="stat-label">Last tracked move</div>
    <div class="primary-stat-value"><strong>{age}</strong><span>ago</span></div>
  </div>
  <div class="kpi-row">
    <div class="kpi-tile"><div class="kpi-label">Latest</div><div class="kpi-value static-date">{format_pacific(latest["announced_at"], date_only=True)}</div><div class="kpi-sub">Pacific Time</div></div>
    <div class="kpi-tile"><div class="kpi-label">Tracked</div><div class="kpi-value">{len(events)}</div><div class="kpi-sub">events</div></div>
  </div>
  <p class="static-event-copy">{html.escape(clean_event_text(latest.get("text", "")))}</p>
  <p><a class="event-link" href="{html.escape(latest["url"], quote=True)}" rel="noopener">Read original ↗</a></p>
  {source_html}'''


def build_vendor_cards(vendors, generated):
    cards = []
    for index, key in enumerate(VENDOR_ORDER, start=1):
        vendor = vendors.get(key)
        if not vendor:
            continue
        events = vendor.get("events") or []
        observations_html = build_observations(vendor)
        if not events and not observations_html:
            continue
        company, product = VENDOR_META[key]
        badge_text, badge_state = probe_badge(
            vendor.get("probe"), generated, renderable_observations(vendor.get("observations"))
        )
        # A vendor can now be probe-only: data/openai.json is a gitignored
        # fetch cache, so on a fresh clone the probe export is the only thing
        # this vendor has. Rendering the announcement block against an empty
        # list used to be an IndexError on events[-1].
        body = build_announcement_body(vendor, events, generated) if events else NO_ANNOUNCEMENTS_HTML
        cards.append(
            f'''<article class="vendor static-vendor" style="--vendor-color:var(--v-{key})">
  <div class="vendor-head">
    <div class="vendor-identity"><span class="vendor-number">0{index}</span><div>
      <p class="vendor-company">{company}</p><h3>{product}</h3>
    </div></div>
    <span class="signal-status" data-probe="{badge_state}">{html.escape(badge_text)}</span>
  </div>
  {body}
  {observations_html}
</article>'''
        )
    return "\n".join(cards)


def recent_events(vendors, generated, days=30):
    cutoff = generated - timedelta(days=days)
    rows = []
    for key in VENDOR_ORDER:
        for event in vendors.get(key, {}).get("events", []):
            occurred = parse_timestamp(event["announced_at"])
            if cutoff <= occurred <= generated:
                rows.append((occurred, key, event))
    return sorted(rows, key=lambda row: row[0], reverse=True)


EMPTY_EVENTS_MARKER = "no-recent-events"

# How many of our accounts a confirmation names, in words; the email and the
# probe-first subject count the same way (incidents.probe_accounts_phrase).
SEEN_COUNT_WORDS = {2: "two", 3: "three", 4: "four"}


def seen_span(minutes):
    """`2 min`, `1 h`, `3 h 5 min`: a whole number of minutes, unsigned."""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def seen_line(event):
    """`Seen on our account 2 min before this post`, or "".

    What the methodology promises a confirmed announcement shows: that our
    own account saw it, and when, relative to the post. Built only from the
    annotation's labels, minutes and verdict (annotate_announcements), never
    an id, a time of day or a level: one account's clear stays private
    except as the timing of a reset the vendor announced. A `vendor_reset`
    names how many accounts, an `unattributed` clear says "our account", and
    anything else, or an annotation that is not `confirmed`, draws nothing.
    The interactive twin is seenLine() in site/app.js; tests/test_build.py
    runs both on the same cases.
    """
    observed = event.get("observed") if isinstance(event, dict) else None
    if not isinstance(observed, dict) or observed.get("status") != OBSERVED_CONFIRMED:
        return ""
    minutes = observed.get("lead_minutes")
    if isinstance(minutes, float) and minutes.is_integer():
        # JSON has one number type: app.js reads 2.0 as the integer 2.
        minutes = int(minutes)
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return ""
    labels = observed.get("accounts")
    count = len({label for label in labels if isinstance(label, str) and label}) if isinstance(labels, list) else 0
    verdict = observed.get("verdict")
    if verdict == VERDICT_VENDOR_RESET and count >= 2:
        who = f"{SEEN_COUNT_WORDS.get(count, str(count))} of our accounts"
    elif verdict == incidents.VERDICT_UNATTRIBUTED and count >= 1:
        who = "our account"
    else:
        return ""
    if minutes == 0:
        when = "within a minute of this post"
    elif minutes > 0:
        when = f"{seen_span(minutes)} before this post"
    else:
        when = f"{seen_span(-minutes)} after this post"
    return f"Seen on {who} {when}"


def build_event_rows(rows, limit=8):
    if not rows:
        # An explicit empty state, not a blank gap. It also gives
        # scripts/check_site.py something definite to assert on: without it a
        # build with no recent announcements and a build that silently lost its
        # event rendering look identical, which is why the site checks failed
        # on a fresh clone with no cached OpenAI feed.
        return (
            f'<article class="event-item {EMPTY_EVENTS_MARKER}">'
            "<div class=\"event-content\"><p>No tracked announcements in the "
            "last 30 days.</p></div></article>"
        )
    output = []
    for _occurred, key, event in rows[:limit]:
        company, _product = VENDOR_META[key]
        confidence = "approximate" if event.get("confidence") == "approx" else "observed"
        seen = seen_line(event)
        seen_html = f'<p class="event-seen">{html.escape(seen)}</p>' if seen else ""
        output.append(
            f'''<article class="event-item" style="--vendor-color:var(--v-{key})">
  <div class="event-meta"><time datetime="{html.escape(event["announced_at"], quote=True)}">{html.escape(format_event_timestamp(event))}</time><span>{company}</span></div>
  <div class="event-content"><span class="event-kind">{html.escape(event.get("kind", "signal"))}</span><p>{html.escape(clean_event_text(event.get("text", "")))}</p>{seen_html}<span class="sr-only">Timestamp confidence: {confidence}.</span></div>
  <a class="event-link" href="{html.escape(event["url"], quote=True)}" rel="noopener">Original ↗</a>
</article>'''
        )
    return "\n".join(output)


def timing_stats(events, generated):
    cells = [[0 for _period in PERIODS] for _day in WEEKDAYS]
    recent = []
    dated = []
    cutoff = generated - timedelta(days=30)
    for event in events:
        occurred = parse_timestamp(event["announced_at"])
        local = occurred.astimezone(PACIFIC)
        period_index = next(index for index, (_label, start, end) in enumerate(PERIODS) if start <= local.hour < end)
        cells[local.weekday()][period_index] += 1
        dated.append(local)
        if cutoff <= occurred <= generated:
            recent.append(local)
    friday_total = sum(cells[4])
    friday_evening = cells[4][3]
    recent_friday = sum(1 for value in recent if value.weekday() == 4)
    return {
        "cells": cells,
        "total": len(dated),
        "recent_total": len(recent),
        "friday_total": friday_total,
        "friday_evening": friday_evening,
        "recent_friday": recent_friday,
        "first": min(dated) if dated else None,
        "last": max(dated) if dated else None,
    }


def build_timing_pattern(events, generated):
    stats = timing_stats(events, generated)
    maximum = max((count for row in stats["cells"] for count in row), default=1) or 1
    recent_phrase = (
        f'{stats["recent_friday"]} of {stats["recent_total"]}'
        if stats["recent_total"]
        else "No"
    )
    all_pct = round(100 * stats["friday_total"] / stats["total"]) if stats["total"] else 0
    range_text = "No events yet"
    if stats["first"] and stats["last"]:
        range_text = f'{stats["first"].strftime("%b")} {stats["first"].day}, {stats["first"].year} – {stats["last"].strftime("%b")} {stats["last"].day}, {stats["last"].year} PT'

    header = "".join(f'<span role="columnheader">{label}</span>' for label, _start, _end in PERIODS)
    rows = []
    for day_index, day in enumerate(WEEKDAYS):
        cells = []
        for period_index, (label, _start, _end) in enumerate(PERIODS):
            count = stats["cells"][day_index][period_index]
            intensity = count / maximum
            cells.append(
                f'<span class="heat-cell" role="cell" style="--heat:{intensity:.3f}" '
                f'aria-label="{day} {label}: {count} events"><strong>{count}</strong></span>'
            )
        rows.append(f'<div class="heat-row" role="row"><span class="heat-day" role="rowheader">{day[:3]}</span>{"".join(cells)}</div>')

    return f'''<div class="timing-summary-grid">
  <article class="pattern-callout">
    <p class="panel-label">Recent observation</p>
    <strong>{recent_phrase} announcements landed on Friday in the last 30 days.</strong>
    <p>Across all {stats["total"]} tracked events, Friday accounts for {stats["friday_total"]} ({all_pct}%), including {stats["friday_evening"]} Friday evenings. That is not yet a statistically reliable weekly routine.</p>
    <p class="pattern-range">{range_text} · public announcement time, not internal execution time</p>
  </article>
  <div class="heatmap-wrap">
    <div class="heatmap" role="table" aria-label="Codex announcement counts by Pacific weekday and time of day">
      <div class="heat-head" role="row"><span aria-hidden="true"></span>{header}</div>
      {''.join(rows)}
    </div>
    <p class="heat-legend"><span>Fewer</span><i></i><i></i><i></i><i></i><span>More announcements</span></p>
  </div>
</div>'''


def build_schema(vendors, generated_at):
    all_events = [event for vendor in vendors.values() for event in vendor["events"]]
    dates = sorted(event["announced_at"] for event in all_events)
    sources = sorted(
        {
            vendor["source"]["url"]
            for vendor in vendors.values()
            if vendor.get("source") and vendor["source"].get("url")
        }
    )
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{PUBLIC_URL}#website",
                "name": "AI Reset Watch",
                "url": PUBLIC_URL,
                "description": "Independent public record of AI coding-assistant reset and quota-change announcements.",
                "publisher": {"@id": f"{PUBLIC_URL}#maintainer"},
            },
            {
                "@type": "CollectionPage",
                "@id": PUBLIC_URL,
                "name": "AI Reset Watch — Claude, Codex & Gemini Reset Signals",
                "url": PUBLIC_URL,
                "isPartOf": {"@id": f"{PUBLIC_URL}#website"},
                "dateModified": generated_at,
                "primaryImageOfPage": {"@id": f"{PUBLIC_URL}#share-image"},
                "mainEntity": {"@id": f"{PUBLIC_URL}#dataset"},
            },
            {
                "@type": "ImageObject",
                "@id": f"{PUBLIC_URL}#share-image",
                "url": f"{PUBLIC_URL}og-image.png",
                "width": 1200,
                "height": 630,
                "caption": "AI Reset Watch — Know when the limits move",
            },
            {
                "@type": "Dataset",
                "@id": f"{PUBLIC_URL}#dataset",
                "name": "AI coding-assistant reset and quota signal history",
                "description": "Tracked public announcements for Claude Code, Codex/ChatGPT, and Gemini CLI/Code Assist. Events may be incomplete and are not provider operational telemetry.",
                "url": PUBLIC_URL,
                "dateModified": generated_at,
                "temporalCoverage": f"{dates[0]}/{dates[-1]}" if dates else None,
                "creator": {"@id": f"{PUBLIC_URL}#maintainer"},
                "isAccessibleForFree": True,
                "keywords": ["Claude Code", "Codex", "ChatGPT", "Gemini CLI", "usage reset", "quota change"],
                "isBasedOn": sources,
                "measurementTechnique": "Aggregation of linked public announcements with confidence labels",
                "distribution": {
                    "@type": "DataDownload",
                    "contentUrl": f"{PUBLIC_URL}data.json",
                    "encodingFormat": "application/json",
                },
            },
            {
                "@type": "Person",
                "@id": f"{PUBLIC_URL}#maintainer",
                "name": MAINTAINER_NAME,
                "url": MAINTAINER_URL,
            },
        ],
    }
    graph["@graph"][3] = {key: value for key, value in graph["@graph"][3].items() if value is not None}
    return '<script type="application/ld+json" id="site-schema">\n  ' + json.dumps(graph, ensure_ascii=False, separators=(",", ":")) + "\n  </script>"


def write_sitemap(generated):
    lastmod = generated.date().isoformat()
    SITEMAP_FILE.write_text(
        f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{PUBLIC_URL}</loc><lastmod>{lastmod}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>
  <url><loc>{PUBLIC_URL}methodology.html</loc><lastmod>{lastmod}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>
</urlset>
''',
        encoding="utf-8",
    )


def write_analytics():
    """Render the analytics script, or an inert stand-in when no key is set.

    Three pages carry `<script src="analytics.js">`, so the file has to exist
    whatever happens; the choice is between a working beacon and a comment. A
    fork gets the comment, because AI_RESETS_POSTHOG_KEY is unset for anyone
    who has not deliberately supplied their own project key, and a key baked
    into a public repository is one that strangers can post events to.
    """
    key = os.environ.get("AI_RESETS_POSTHOG_KEY", "").strip()
    if not key:
        ANALYTICS_FILE.write_text(
            '"use strict";\n'
            "// ai-resets-analytics: disabled\n"
            "// AI_RESETS_POSTHOG_KEY is not set. This file exists so the pages\n"
            "// that reference it do not 404. Every call site uses optional\n"
            "// invocation, so defining nothing here is safe.\n",
            encoding="utf-8",
        )
        return
    document = ANALYTICS_TEMPLATE.read_text(encoding="utf-8")
    placeholder = "__AI_RESETS_POSTHOG_KEY__"
    if placeholder not in document:
        raise SystemExit(f"{ANALYTICS_TEMPLATE} no longer carries {placeholder}")
    # A key with a quote in it would break out of the string literal it lands
    # in. Refuse rather than emit script that a reviewer would have to audit.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise SystemExit("AI_RESETS_POSTHOG_KEY may only contain [A-Za-z0-9_-]")
    ANALYTICS_FILE.write_text(document.replace(placeholder, key), encoding="utf-8")


def update_index(vendors, generated_at):
    generated = parse_timestamp(generated_at)
    document = INDEX_TEMPLATE.read_text(encoding="utf-8")
    rows = recent_events(vendors, generated)
    document = replace_marker(document, "SCHEMA", "\n  " + build_schema(vendors, generated_at) + "\n  ")
    document = replace_marker(document, "UPDATED", f"Updated {html.escape(format_pacific(generated_at))}")
    document = replace_marker(document, "VENDOR_CARDS", "\n" + build_vendor_cards(vendors, generated) + "\n          ")
    document = replace_marker(document, "EVENT_COUNT", f"{len(rows)} events / last 30 days")
    document = replace_marker(document, "EVENTS", "\n" + build_event_rows(rows) + "\n        ")
    document = replace_marker(document, "TIMING", "\n" + build_timing_pattern(vendors.get("openai", {}).get("events", []), generated) + "\n          ")
    INDEX_FILE.write_text(document, encoding="utf-8")
    write_sitemap(generated)


def load_vendors(data_dir):
    """The tracked announcement seeds and their gitignored fetch caches.

    `glob("*.json")` is not recursive, so the `data/observed/` directory the
    probe writes into cannot be read as a vendor seed; `is_file()` also covers
    a directory that happens to be named `*.json`. A seed with no `vendor` key
    is skipped with a line on stdout rather than raising: this runs from cron
    ahead of the rsync, and one malformed file used to abort the whole publish.
    """
    vendors = {}
    for path in sorted(data_dir.glob("*.json")):
        if not path.is_file():
            print(f"skipping {path.name}: not a file")
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"skipping {path.name}: {error}")
            continue
        vendor = raw.get("vendor") if isinstance(raw, dict) else None
        if not isinstance(vendor, str) or not vendor:
            print(f"skipping {path.name}: no vendor key")
            continue
        events = [
            event
            for event in (raw.get("events") or [])
            if isinstance(event, dict) and event.get("announced_at")
        ]
        vendors[vendor] = {
            "source": raw.get("source"),
            "fetched_at": raw.get("fetched_at"),
            "events": sorted(events, key=lambda event: event["announced_at"]),
        }
    return vendors


def attach_observations(vendors, observed_dir):
    """Join the probe's verdicts onto the vendors, public rows only.

    A vendor with a probe but no announcement seed is created here rather than
    skipped: an own-account measurement is a signal about that vendor whether
    or not anybody tweeted about it, and that is the case this whole phase
    exists to show.
    """
    for key in VENDOR_ORDER:
        path = observed_dir / f"{key}.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"skipping observed/{path.name}: {error}")
            continue
        if not isinstance(raw, dict):
            print(f"skipping observed/{path.name}: not an object")
            continue
        stated = raw.get("vendor")
        if isinstance(stated, str) and stated and stated != key:
            # The file says which account it measured. Trusting the filename
            # instead would print one account's measurements on another
            # company's card the first time a path or a unit file is copied,
            # and "cleared early, unexplained" is the strongest claim here.
            print(f"skipping observed/{path.name}: says vendor {stated!r}, not {key!r}")
            continue
        vendor = vendors.setdefault(
            key, {"source": None, "fetched_at": None, "events": []}
        )
        probe = raw.get("probe")
        if isinstance(probe, dict):
            vendor["probe"] = {
                field: probe[field] for field in PROBE_FIELDS if field in probe
            }
        vendor["observations"] = public_observations(raw.get("observations"))
        print(
            f"{key}: probe {(vendor.get('probe') or {}).get('status', 'unknown')}, "
            f"{len(vendor['observations'])} public observations"
        )
    return vendors



# ─── Joining an announcement to what our own account saw ─────────────────────

# How far from an announcement an observation may sit and still be the same
# event. Symmetric on purpose: on 2026-08-30 this account cleared two and a
# half minutes BEFORE the post, so a rollout arriving before the vendor writes
# about it is the ordinary case, not an anomaly.
OBSERVED_MATCH_SECONDS = 6 * 3600

OBSERVED_CONFIRMED = "confirmed"
OBSERVED_NOT_SEEN = "not_seen"
OBSERVED_UNPROBED = "unprobed"
# A probe that ships but has never reported. Distinct from unprobed (we measure
# nothing here) and from not_seen (we looked and saw nothing), because only the
# last of those three means the announcement went unobserved.
OBSERVED_NO_READING = "no_reading"


def event_epoch(event):
    for field in ("observed_at", "announced_at"):
        raw = event.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return int(parse_timestamp(raw).timestamp())
        except (ValueError, TypeError):
            continue
    return None


def annotate_announcements(vendors, state_dir=None):
    """Say, on each announcement, whether our own account saw the thing.

    This is the other half of the question the site exists to answer. The card
    already shows what a vendor SAID; without this it never shows whether that
    reached a real account, which is the only part we can actually check.

    Confirmation follows incidents.corroborates, the rule the lifecycle uses:
    a `vendor_reset` (two of our accounts) within +-6 h, or one account's
    `unattributed` clear between three hours before and one hour after a
    TRUSTED retrospective reset post. Legacy global candidates are unresolved
    and confirm nothing. A natural expiry says nothing about the vendor, and a
    self-applied clear says what the account holder did with their own banked
    credit — offering either as corroboration would be false and, in the second
    case, would publish something private.

    A confirmed annotation names the verdict, our account label(s) and the
    signed minutes between our clear and the post (`lead_minutes`, positive
    when our account saw it first), so the page can say "seen on account b
    2 min before the post" without re-reading the probe.
    """
    state = Path(state_dir) if state_dir else groundtruth.DEFAULT_STATE_DIR
    running = groundtruth.probed_vendors(state)
    for key, vendor in vendors.items():
        spec = groundtruth.PROBE_SPECS.get(key)
        if spec is None or not spec.exists():
            # No probe for this vendor at all.
            for event in vendor.get("events") or []:
                event["observed"] = {"status": OBSERVED_UNPROBED}
            continue
        if key not in running:
            # The probe ships but has never written a health block, so it has
            # never looked. "Not seen" would claim a look that did not happen.
            for event in vendor.get("events") or []:
                event["observed"] = {"status": OBSERVED_NO_READING}
            continue
        try:
            observations = groundtruth.clear_observations(state, vendor=key)
        except Exception as error:  # never take the publish down for an annotation
            print(f"skipping {key} annotations: {error}")
            continue
        clears = [
            o for o in observations
            if o["verdict"] in (groundtruth.VERDICT_VENDOR, incidents.VERDICT_UNATTRIBUTED)
        ]
        for event in vendor.get("events") or []:
            announced = event_epoch(event)
            if announced is None:
                continue
            near = [
                o for o in clears
                if abs(o["t"] - announced) <= OBSERVED_MATCH_SECONDS
                and incidents.corroborates(event, o["verdict"], iso_z(o["t"]))
            ]
            if not near:
                event["observed"] = {"status": OBSERVED_NOT_SEEN}
                continue
            match = min(near, key=lambda o: (abs(o["t"] - announced), o["key"]))
            lead = announced - match["t"]
            event["observed"] = {
                "status": OBSERVED_CONFIRMED,
                "observed_at": iso_z(match["t"]),
                # Negative means the announcement came first; positive means we
                # saw it first. The sign is the interesting part.
                "lead_seconds": lead,
                "lead_minutes": int(math.floor(lead / 60 + 0.5)),
                "verdict": match["verdict"],
                "accounts": clear_accounts(match),
            }


def iso_z(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clear_accounts(clear):
    """The account labels behind one clear row: its own, and its witness's."""
    labels = []
    for field in ("account", "witness_account"):
        label = clear.get(field)
        if isinstance(label, str) and label and label not in labels:
            labels.append(label)
    return sorted(labels)


# ─── Incident fusion: what each announcement IS, written once ────────────────
#
# The lifecycle states (docs/robustness-plan.md, "Incident lifecycle") are
# derived here, at build time, and stamped onto every event so the page and
# the notifier read `status` from site/data.json instead of each recomputing
# it. A probe-first incident is created here too, and only from what the
# owner's 2026-10-11 decision allows (docs/reset-attribution.md): a
# `vendor_reset` pair that no trusted announcement claims. One account's clear
# never starts an incident, whatever it reads.


def confirmed_by(event, members, withdrawn_keys=frozenset()):
    """Would any of these clears, as they stood, confirm this announcement?

    The test a post must pass to take a pair over: one member inside the
    +-6 h match window that speaks_to() the post.
    """
    announced = event_epoch(event)
    return announced is not None and any(
        abs(clear["t"] - announced) <= OBSERVED_MATCH_SECONDS
        and speaks_to(event, clear, withdrawn_keys)
        for clear in members
    )


def withdrawn_verdict(clear, withdrawn_keys=frozenset()):
    """The verdict a withdrawn clear held while it stood.

    attribution never stamps a retracted clear, so on the log it reads as its
    own class (`unattributed`, `unresolved`, ...). A member of a pair that
    stood before the retraction (groundtruth.withdrawn_pair_clears) was a
    `vendor_reset`, and that is what it was evidence of.
    """
    return groundtruth.VERDICT_VENDOR if clear.get("key") in withdrawn_keys else clear["verdict"]


def speaks_to(event, clear, withdrawn_keys=frozenset()):
    """Does this clear, standing or withdrawn, bear on this announcement?

    Standing: incidents.corroborates says it confirms the post — its kind, its
    stage and the verdict's own window (a banked reset is confirmed by a
    credit grant, never by a window clear; one account's clear only inside
    the tight window of a trusted retrospective post).

    Withdrawn: only if, while it stood, it WOULD have confirmed this post by
    that same rule, and only on a window that can pair. Any retraction within
    the +-6 h used to count, so a 5-hour session clear taken back nearer the
    post than a two-account pair turned a confirmed reset into
    `reverted_on_our_account` and the post mailed nobody (review case [20]).
    """
    if clear.get("retracted"):
        family = clear.get("window_family")
        if not isinstance(family, str) or family not in attribution.PAIRING_FAMILIES:
            return False
        verdict = withdrawn_verdict(clear, withdrawn_keys)
    else:
        verdict = clear["verdict"]
    return incidents.corroborates(event, verdict, iso_z(clear["t"]))


def match_observation(event, clears, withdrawn_keys=frozenset()):
    """The one probe clear that speaks to this announcement, if any.

    The nearest standing clear that confirms it, else the nearest withdrawn
    one that would have (speaks_to): a withdrawn clear under a standing
    announcement is the `reverted_on_our_account` footnote, and fusion must
    see it precisely because it is no longer evidence. Standing evidence
    wins over a withdrawal however near the withdrawal sits: an account
    whose first clear reverted and which then cleared again beside a second
    account has seen the announced reset, and calling the post reverted
    would leave the standing pair to mail "not announced yet" beside it.
    """
    announced = event_epoch(event)
    if announced is None:
        return None
    candidates = [
        clear
        for clear in clears
        # The +-6 h match window bounds every candidate first. derive_status
        # gives a forecast no upper bound, and without this a credit grant
        # weeks later "confirmed" the 2026-08-21 forecast in rehearsal.
        if abs(clear["t"] - announced) <= OBSERVED_MATCH_SECONDS
        and speaks_to(event, clear, withdrawn_keys)
    ]
    standing = [clear for clear in candidates if not clear.get("retracted")]
    candidates = standing or candidates
    if not candidates:
        return None
    nearest = min(candidates, key=lambda clear: (abs(clear["t"] - announced), str(clear.get("key"))))
    return {
        "verdict": nearest["verdict"],
        "observed_at": iso_z(nearest["t"]),
        "retracted": bool(nearest.get("retracted")),
        "witness": nearest.get("witness"),
        "key": nearest.get("key"),
    }


def probe_groups(vendor_key, clears, withdrawn=()):
    """The probe-first incidents in one vendor's clears: (live, withdrawn).

    Live: the `vendor_reset` clears joined through the witness each names —
    one incident per pair, however many accounts it spans. Withdrawn: the
    pairs groundtruth.withdrawn_pair_clears found a retraction has broken,
    whose readers are owed a correction. Each group is (incident_key, members
    earliest first), from groundtruth.probe_group_key: the key is built from
    the earliest member's lower bracket, so a pair keeps one key from its
    first build to its retraction, and the correction mail finds its group
    under the same key.
    """
    # A withdrawn clear is never stamped vendor_reset (attribution excludes
    # it), so this is the live pairs and nothing else.
    by_key = {
        clear["key"]: clear
        for clear in clears
        if clear.get("key") and clear["verdict"] == groundtruth.VERDICT_VENDOR
    }
    live = attribution.pair_components(
        {key: clear.get("witness") for key, clear in by_key.items()}
    )

    return (
        [groundtruth.probe_group_key(vendor_key, [by_key[key] for key in members])
         for members in live],
        [groundtruth.probe_group_key(vendor_key, members) for members in withdrawn if members],
    )


def mailed_hold_passed(members):
    """Could a probe-first mail have gone out before this pair broke?

    Only once the lifecycle's hold had run out: it starts at the later clear
    (fuse_incidents passes derive_status that clear as `observed_at`) and
    nothing mails before it ends. A member whose clear or revert time cannot
    be read answers yes — a correction owed and not sent is the worse error,
    and the row carries no clear either way.
    """
    seen = [clear.get("t") for clear in members]
    reverts = [clear.get("reverted_at") for clear in members if clear.get("retracted")]
    readable = [
        value for value in seen + reverts
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if not reverts or len(readable) != len(seen) + len(reverts):
        return True
    return min(reverts) >= max(seen) + incidents.OBSERVED_HOLD_SECONDS


def _clear_summary(clear):
    """One clear as the email's ground-truth block quotes it: labels, never fingerprints."""
    return {
        "account": clear.get("account"),
        "observed_at": iso_z(clear["t"]),
        "used_before": clear.get("used_before"),
        "used_after": clear.get("used_after"),
    }


def _incident_fields(key, members):
    accounts = sorted({c.get("account") for c in members if isinstance(c.get("account"), str)})
    return {
        "incident_key": key,
        "origin": incidents.ORIGIN_PROBE,
        "kind": incidents.KIND_RESET,
        "accounts": accounts,
        "clears": [_clear_summary(clear) for clear in sorted(members, key=lambda c: c["t"])],
    }


def fuse_incidents(vendors, state_dir=None, now=None):
    """Stamp every event with its incident `status` and `incident_key`, and
    turn each unclaimed two-account pair into one probe-first incident.

    One clock for the whole build, the same instant the notifier's own guards
    use, so a forecast cannot be "still watching" in data.json and "lapsed" in
    the email of the same minute. A vendor with no probe fuses against an
    empty observation list and a None probe: `forecast_lapsed` then stays
    unreachable, which is the honest answer when nothing is watching.

    Rows written, in the shape the notifier's own tests hand it
    (tests/test_subscriptions.py, StageAwareNotifierTests) with the key stated
    rather than left to be derived from a `slot` the feed does not carry:
      - the announcement that claims a pair carries `merged_from` = the pair's
        probe key, so subscriptions.plan_adoptions renames a probe-first
        incident already mailed instead of mailing the post as a second one;
      - every observation row of a claimed pair carries the announcement's
        `incident_key` and no `status`: it never mails on its own;
      - an unclaimed pair's rows carry its probe key, `origin`, `kind`, the
        account labels and both clears, and the earliest row alone carries
        the `status` (one incident, one mailable row);
      - a pair that stood and was since withdrawn is one extra row with
        `status: retracted`, its key, its id and no verdict, and nothing
        else, so the page draws nothing, the feed shows no clear, and the
        notifier can still correct what it sent.
    """
    state = Path(state_dir) if state_dir else groundtruth.DEFAULT_STATE_DIR
    for key, vendor in vendors.items():
        events = vendor.get("events") or []
        observations = vendor.get("observations") or []
        spec = groundtruth.PROBE_SPECS.get(key)
        if spec is None or not spec.exists():
            clears = []
        else:
            try:
                clears = groundtruth.incident_observations(state, vendor=key)
            except Exception as error:  # never take the publish down for fusion
                print(f"skipping {key} fusion: {error}")
                clears = []
        probe = vendor.get("probe")
        probe = probe if isinstance(probe, dict) else None
        try:
            broken = (
                groundtruth.withdrawn_pair_clears(state, vendor=key)
                if spec is not None and spec.exists()
                else []
            )
        except Exception as error:  # never take the publish down for fusion
            print(f"skipping {key} withdrawn pairs: {error}")
            broken = []
        live, withdrawn = probe_groups(key, clears, broken)
        withdrawn_keys = frozenset(
            clear["key"] for _, members in withdrawn for clear in members
        )
        claimed = {}
        # A withdrawn pair's correction is owed under the incident it was
        # filed under. A post that took the pair over while it stood wears
        # its key, and so must the correction: stated under the probe key
        # instead, the notifier renamed the post's incident back to the probe
        # key and forward again on every tick, and opened a second owner
        # alert for the one reversion (review case [09]).
        withdrawn_claims = {}
        for event in events:
            match = match_observation(event, clears, withdrawn_keys)
            trusted = incidents.is_trusted_announcement(event)
            own = match
            if (
                not trusted
                and match is not None
                and not match["retracted"]
                and match["verdict"] in incidents.ORIGINATING_VERDICTS
            ):
                # The pair is a probe-first incident below, keyed and mailed
                # once. Letting an unverified post wear it as well would make
                # one reset two incidents.
                own = None
            event["status"] = incidents.derive_status(event, own, probe, now=now)
            stable_id = str(event.get("id") or "").strip()
            if stable_id:
                event["incident_key"] = f"{key}:{stable_id}"
            if not (trusted and stable_id):
                continue
            if event["status"] in (incidents.STATUS_CONFIRMED, incidents.STATUS_REVERTED):
                # Confirmed by the pair while it stood: reverted now if the
                # withdrawn half was what confirmed it, still confirmed if
                # standing evidence does.
                for incident, members in withdrawn:
                    if incident not in withdrawn_claims and confirmed_by(
                        event, members, withdrawn_keys
                    ):
                        withdrawn_claims[incident] = event["incident_key"]
            if event["status"] != incidents.STATUS_CONFIRMED:
                # Only a post the pair actually CONFIRMS may take it over. A
                # pair handed to a post that ends up reverted, historical or
                # merely announced loses its own status row and mails as
                # neither (review case [20]: two subscribers, no mail at all).
                continue
            # Every pair this post confirms is this post's reset, not only the
            # nearest clear: a lone clear on another window can sit closer to
            # the post than the pair does, and leaving the pair unclaimed
            # would mail one reset twice. Events are in announcement order, so
            # the earliest trusted post that confirms a pair claims it.
            mine = [
                incident
                for incident, members in live
                if incident not in claimed and confirmed_by(event, members)
            ]
            for incident in mine:
                claimed[incident] = event["incident_key"]
            if mine:
                # One name is enough for the rename: the other pairs' rows
                # carry this key too, and the notifier's store re-files them.
                event["merged_from"] = mine[0]

        rows = {
            row["event_id"]: row
            for row in observations
            if isinstance(row, dict) and isinstance(row.get("event_id"), str)
        }
        for incident, members in live:
            member_rows = [rows.get(published_id(clear["key"])) for clear in members]
            if not any(member_rows):
                # The export lags the log by at most a tick, and a pair whose
                # rows are missing has nothing to show or quote yet. It is
                # created on the next build, under the same key.
                print(f"skipping {incident}: no exported row for it yet")
                continue
            if incident in claimed:
                for row in member_rows:
                    if row is not None:
                        row["incident_key"] = claimed[incident]
                        row["origin"] = incidents.ORIGIN_PROBE
                continue
            status = incidents.derive_status(
                None,
                {
                    "verdict": groundtruth.VERDICT_VENDOR,
                    # The LATER clear starts the hold: the pair is not a pair
                    # until its second half exists.
                    "observed_at": iso_z(max(clear["t"] for clear in members)),
                    "witness": members[0].get("witness"),
                },
                probe,
                now=now,
            )
            if status == incidents.STATUS_OBSERVED_SINGLE:
                # Never shown, never mailed. Unreachable with a witness, and
                # kept unreachable by not writing it anywhere.
                continue
            fields = _incident_fields(incident, members)
            for row in member_rows:
                if row is not None:
                    row.update(fields)
            next(row for row in member_rows if row is not None)["status"] = status

        for incident, members in withdrawn:
            if not mailed_hold_passed(members):
                # Broken inside the hold, so never mailed: nobody is owed a
                # correction, and the row would be the one public trace of
                # the clear left standing — a lone `unattributed` clear the
                # contract keeps private. A pair can form and break between
                # two five-minute publishes, so "it was never on the page" is
                # not something this build can rule out otherwise (privacy
                # re-verification: the key and digest gave away the surviving
                # clear's account and second).
                print(f"no correction for {incident}: withdrawn inside the mail hold")
                continue
            # What the notifier needs to correct a mail it sent, and nothing
            # a reader could learn a clear from: the key it filed the mail
            # under, the event key it recorded, and the status. The clears,
            # the account labels and the times stay out. The correction is
            # composed from the notifier's own record of what it sent and from
            # the raw log on this host (groundtruth), never from this row, so
            # a lone clear can never ride a correction onto the public feed.
            # check_site.WITHDRAWN_FIELDS holds the published row to exactly
            # these keys.
            vendor.setdefault("observations", []).append({
                "incident_key": withdrawn_claims.get(incident, incident),
                "event_id": published_id(members[0]["key"]),
                "status": incidents.STATUS_RETRACTED,
                "verdict": None,
            })


def main():
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    vendors = load_vendors(DATA_DIR)
    attach_observations(vendors, OBSERVED_DIR)
    annotate_announcements(vendors)
    fuse_incidents(vendors, now=generated_at)

    out = {"generated_at": generated_at, "vendors": vendors}
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    update_index(vendors, generated_at)
    write_analytics()
    for name, vendor in vendors.items():
        print(f"{name}: {len(vendor['events'])} events")
    print(f"wrote {OUT_FILE}, {INDEX_FILE}, {SITEMAP_FILE}, and {ANALYTICS_FILE}")


if __name__ == "__main__":
    main()
