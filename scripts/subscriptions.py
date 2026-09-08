#!/usr/bin/env python3
"""Email subscriptions and reset-event notifications for AI Reset Watch.

The public site remains static. This module provides a small localhost HTTP API,
SQLite-backed subscription state, and a notifier command used by systemd.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parseaddr
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # systemd runs this file by absolute path, which puts scripts/ on sys.path
    # instead of the repository root, so the sibling modules below would not
    # import. The unit tests import them as `scripts.*`; keep one spelling.
    sys.path.insert(0, str(ROOT))

from scripts.groundtruth import (  # noqa: E402
    DEFAULT_STATE_DIR,
    STATUS_ABSENT,
    PROBE_STALE_SECONDS,
    ground_truth_line,
    load_probe_health,
    probe_status,
    stale_age_line,
)
from scripts.incidents import (  # noqa: E402
    describe_event,
    landed_subject,
    paragraphs,
)
from scripts.timefmt import (  # noqa: E402
    describe_age,
    format_pacific_with_utc,
    parse_timestamp,
)

DEFAULT_DATA_FILE = ROOT / "site" / "data.json"
SITE_CONFIG_FILE = ROOT / "site.config.json"


def site_public_url() -> str:
    """The canonical URL from the same tracked file build.py reads.

    Imported lazily rather than pulled from scripts.build: this module runs as
    a long-lived service and build.py loads the whole announcement corpus at
    import time.
    """
    try:
        config = json.loads(SITE_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(config.get("public_url") or "").rstrip("/")


# An announcement first seen more than this long after it was made is recorded
# but never mailed. Without it, wiring up any new source mails its whole
# history: the Anthropic feed alone would have sent 11 events x 7 subscribers.
MAX_NOTIFY_AGE_SECONDS = 48 * 3600

# More than this many mailable events appearing in a single run is a symptom
# (a re-keyed upstream feed, a backfill, a new source), not a busy news day.
# The whole batch is held rather than trimmed to the cap: choosing three of
# eight to send would be worse than sending none and saying so.
FLOOD_CAP = 3

# The deployed feed is rebuilt every five minutes; if the notifier is reading
# one much older than that, the publish pipeline is dead and no announcement
# can arrive at all.
DATA_STALE_SECONDS = 30 * 60
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")
VENDOR_LABELS = {
    "anthropic": "Anthropic / Claude Code",
    "openai": "OpenAI / Codex",
    "google": "Google / Gemini",
}
VALID_TOPICS = tuple(VENDOR_LABELS)
CONSENT_VERSION = "email-alerts-v1"
SUPPRESSION_EVENTS = {
    "email.bounced": "bounce",
    "email.complained": "complaint",
    "email.suppressed": "provider_suppression",
}


def now_ts() -> int:
    return int(time.time())


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def normalize_email(raw: str) -> str:
    address = parseaddr(raw.strip())[1].lower()
    if len(address) > 254 or not EMAIL_RE.fullmatch(address):
        raise ValueError("Enter a valid email address.")
    return address


def normalize_topics(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Choose at least one provider.")
    requested = {str(value).strip().lower() for value in raw}
    topics = tuple(topic for topic in VALID_TOPICS if topic in requested)
    if not topics:
        raise ValueError("Choose at least one provider.")
    return topics


def safe_http_url(raw: str, fallback: str) -> str:
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw
    return fallback


def verify_svix_signature(
    secret: str,
    message_id: str,
    timestamp: str,
    signature_header: str,
    body: bytes,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify a Standard Webhooks/Svix signature without storing its payload."""
    if not all((secret, message_id, timestamp, signature_header)):
        return False
    try:
        timestamp_int = int(timestamp)
        if abs(now_ts() - timestamp_int) > tolerance_seconds:
            return False
        secret_value = secret.removeprefix("whsec_")
        key = base64.b64decode(secret_value, validate=True)
    except (ValueError, TypeError):
        return False
    signed = f"{message_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()
    for candidate in signature_header.split():
        if "," not in candidate:
            continue
        version, supplied = candidate.split(",", 1)
        if version == "v1" and hmac.compare_digest(expected, supplied):
            return True
    return False


def read_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if credentials_dir:
        path = Path(credentials_dir) / name
        if path.is_file():
            return path.read_text().strip()
    return ""


@dataclass(frozen=True)
class Config:
    api_key: str
    secret: str
    db_path: Path
    public_url: str
    from_email: str
    webhook_secret: str = ""
    host: str = "127.0.0.1"
    port: int = 8787
    data_file: Path = DEFAULT_DATA_FILE
    state_dir: Path = DEFAULT_STATE_DIR
    owner_email: str = ""

    @classmethod
    def from_env(
        cls, *, require_secrets: bool = True, require_sender: bool | None = None
    ) -> "Config":
        """Load configuration; `require_secrets=False` is for read-only commands.

        The Resend key and the signing secret exist only as encrypted systemd
        credentials, readable by the unit and not by a person at a shell. That
        made `notify --dry-run` — the command whose whole point is to be run by
        hand before wiring up a new source — impossible to run. A dry run sends
        nothing and signs nothing, so it may proceed with placeholders, and it
        says so in its output.
        """
        api_key = read_secret("RESEND_API_KEY")
        secret = read_secret("SUBSCRIPTION_SECRET")
        webhook_secret = read_secret("RESEND_WEBHOOK_SECRET")
        if require_secrets:
            if not api_key:
                raise RuntimeError("RESEND_API_KEY is required")
            if len(secret) < 32:
                raise RuntimeError("SUBSCRIPTION_SECRET must be at least 32 characters")
        else:
            api_key = api_key or "dry-run-no-key"
            secret = secret if len(secret) >= 32 else "dry-run-unsigned-links" + "-" * 32
        from_email = os.environ.get("RESEND_FROM_EMAIL", "").strip()
        if require_sender is None:
            require_sender = require_secrets
        if require_sender and not from_email:
            # No default, deliberately. A sending identity is the one setting a
            # fork must never inherit: a deployment that quietly kept this
            # project's address would put someone else's domain in the From
            # line of its own mail, and the bounces and spam complaints would
            # land on that domain's reputation rather than its own.
            raise RuntimeError(
                "RESEND_FROM_EMAIL is required, e.g. "
                '\'AI Reset Watch <updates@example.com>\' — it must be an address on a '
                "domain you have verified with your own mail provider"
            )
        return cls(
            api_key=api_key,
            secret=secret,
            db_path=Path(os.environ.get("SUBSCRIPTION_DB", "/var/lib/ai-resets/subscribers.db")),
            # The built site and the links inside its mail have to agree, so
            # the fallback is the same tracked file build.py reads.
            public_url=os.environ.get("PUBLIC_BASE_URL", site_public_url()).rstrip("/"),
            from_email=from_email or "AI Reset Watch <unset@invalid>",
            webhook_secret=webhook_secret,
            host=os.environ.get("SUBSCRIPTION_HOST", "127.0.0.1"),
            port=int(os.environ.get("SUBSCRIPTION_PORT", "8787")),
            data_file=Path(os.environ.get("AI_RESETS_DATA", DEFAULT_DATA_FILE)),
            state_dir=Path(os.environ.get("AI_RESETS_STATE", DEFAULT_STATE_DIR)),
            owner_email=os.environ.get("OWNER_EMAIL", "").strip(),
        )



# ─── Incidents: one vendor action, one email ─────────────────────────────────
#
# Everything used to be keyed by "<vendor>:<post id>". So the probe seeing a
# reset land on this account and the vendor's post about it half an hour later
# were two keys and two emails for one reset, and a row whose status later
# improved could never re-queue, because its key was already known. The unit is
# now the INCIDENT: one vendor action fused from zero or more own-account
# observations and zero or more announcements (docs/robustness-plan.md,
# "Incident lifecycle").

STAGE_NEW = "new"
STAGE_CONFIRMED = "confirmed"
STAGE_RETRACTED = "retracted"
VALID_STAGES = (STAGE_NEW, STAGE_CONFIRMED, STAGE_RETRACTED)
# `new` is not opt-out: a subscriber who wants no first email unsubscribes.
MANDATORY_STAGES = (STAGE_NEW,)
FOLLOW_UP_STAGES = (STAGE_CONFIRMED, STAGE_RETRACTED)
STAGE_LABELS = {
    STAGE_CONFIRMED: "Tell me when an announced reset reaches our own account",
    STAGE_RETRACTED: "Tell me when we withdraw one of our own observations",
}

ORIGIN_ANNOUNCEMENT = "announcement"
ORIGIN_PROBE = "probe"

# What the `new` mail left open, recorded on the delivery when it is sent. A
# follow-up is decided from what the subscriber actually read, not from what
# today's feed says: re-reading the feed would mail a "confirmed" to people who
# were already told the thing had landed.
CLAIM_NONE = ""
CLAIM_PENDING = "pending"
CLAIM_SETTLED = "settled"

# The incident statuses written into site/data.json. The notifier reads them
# and never recomputes one. The mapping is the Email column of the lifecycle
# table in docs/robustness-plan.md, transcribed here and nowhere else.
STATUS_MAILS_NEW = frozenset({"announced", "confirmed", "forecast", "observed"})
STATUS_RETRACTED = "retracted"
STATUS_REVERTED = "reverted_on_our_account"

# The alert conditions this module opens from a live signal and closes itself.
# Anything outside them (a held flood batch, a failed unit, a reversion on our
# own account) stays open until the thing that opened it is over, so a notify
# tick five minutes later cannot mail "recovered" about an outage still running.
MANAGED_ALERT_PREFIXES = ("probe:", "data:")

# One `alert --unit` reminder per unit per this long. ai-resets-probe.service
# carries Restart=always with RestartSec=10, and systemd fires OnFailure= on
# EVERY restart cycle, so an undeduped alert unit is 8,640 emails a day. One
# mail per outage would be the other error: a fresh failure next week must
# still page, so a trigger this long after the last one sends one reminder.
UNIT_ALERT_REPEAT_SECONDS = 6 * 3600


# Follow-up wording. Like the (kind, stage) tables in scripts/incidents.py
# these are looked up, never composed from post text. The retraction sentence
# is the careful one: it is only ever sent for an incident that came from our
# own probe, so it withdraws OUR measurement and says nothing about a vendor.
CONFIRMED_FALLBACK_SUBJECT = "{product}: the announced change landed"
CONFIRMED_HEADLINE = "{product}: what we told you about has now reached our account."
RETRACTION_SUBJECT = "{product}: withdrawing our earlier observation"
RETRACTION_HEADLINE = "{product}: we are withdrawing what our own account reported."
FOLLOW_UP_LEAD = {
    STAGE_CONFIRMED: (
        "This is the single follow-up promised in our earlier email about this "
        "announcement: our own account has now seen it happen."
    ),
    STAGE_RETRACTED: (
        "We are withdrawing our earlier email. The reading came from our own "
        "account's probe, and that probe has taken it back. No vendor has said "
        "anything about it either way."
    ),
}


def event_origin(event: dict[str, Any]) -> str:
    """Who saw this: our own probe, or a vendor announcement.

    An explicit `origin` wins. Otherwise a row carrying `observed_at` and no
    `announced_at` is a probe row — that is the exported observation's shape
    (scripts/quota_probe.observation) and no announcement has ever had it.
    """
    declared = str(event.get("origin") or "").strip().lower()
    if declared in (ORIGIN_ANNOUNCEMENT, ORIGIN_PROBE):
        return declared
    if event.get("observed_at") and not event.get("announced_at"):
        return ORIGIN_PROBE
    return ORIGIN_ANNOUNCEMENT


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(parse_timestamp(value).timestamp())
        except (ValueError, TypeError):
            return None
    return None


def incident_key(event: dict[str, Any]) -> str:
    """The dedupe unit: `<vendor>:<post id>`, or `<vendor>:probe-<slot>-<lo>`.

    A fused row states its own key and it is returned unchanged; deciding which
    observation belongs to which post is the fusion layer's job, not the
    notifier's. The derived probe key is built from the clear's lower bracket
    rather than from the payload on purpose: `flatten_events` used to fall back
    to a sha256 of the whole row, so an export that merely re-rendered its own
    evidence sentence read as a second reset and mailed again.
    """
    stated = str(event.get("incident_key") or "").strip()
    if stated:
        return stated
    vendor = str(event.get("vendor", ""))
    if event_origin(event) == ORIGIN_PROBE:
        slot = str(event.get("slot") or event.get("window") or "window")
        low = _epoch(
            event.get("observed_before")
            or event.get("clear_bracket_lo")
            or event.get("observed_at")
        )
        return f"{vendor}:probe-{slot}-{'unbracketed' if low is None else low}"
    stable_id = str(event.get("id") or event.get("event_id") or "")
    if not stable_id:
        stable_id = hashlib.sha256(
            json.dumps(event, sort_keys=True, default=str).encode()
        ).hexdigest()[:24]
    return f"{vendor}:{stable_id}"


def mail_stage(event: dict[str, Any]) -> str | None:
    """Whether this row may produce a FIRST email, from its incident status.

    No status at all is the pre-fusion feed: an announcement keeps the old
    behaviour (mailable, subject to the age cutoff and the flood cap) and an
    own-account observation does not. A probe clear reaches subscribers only
    once the fusion layer has raised it to `observed`; an unfused measurement
    is a site chip and an owner alert, never seven inboxes.
    """
    status = str(event.get("status") or "").strip().lower()
    if status:
        return STAGE_NEW if status in STATUS_MAILS_NEW else None
    return STAGE_NEW if event_origin(event) == ORIGIN_ANNOUNCEMENT else None


def normalize_stages(raw: Any) -> tuple[str, ...]:
    """Which kinds of email this subscriber accepts, `new` always included."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raw = []
    requested = {str(value).strip().lower() for value in raw}
    requested.update(MANDATORY_STAGES)
    return tuple(stage for stage in VALID_STAGES if stage in requested)


def plan_adoptions(
    events: list[dict[str, Any]],
    stored_by_event: dict[str, str],
    known_incidents: set[str],
) -> tuple[tuple[str, str], ...]:
    """The (old key -> new key) renames this feed implies.

    A probe-first incident that later matches an announcement adopts the
    announcement's key. Without the rename the announcement is a second
    incident and everyone who was mailed about the observation is mailed again
    about the post — the exact duplicate this phase exists to remove.
    """
    renames: dict[str, str] = {}
    for event in events:
        new_key = incident_key(event)
        candidates = (
            str(event.get("merged_from") or ""),
            stored_by_event.get(str(event.get("event_key") or ""), ""),
        )
        for old_key in candidates:
            if old_key and old_key != new_key and old_key in known_incidents:
                renames[old_key] = new_key
    return tuple(sorted(renames.items()))


def settled_claim(reading: dict[str, Any], ground_truth: dict[str, Any]) -> str:
    """What the mail about to be sent leaves open.

    `settled` when it already says our own account saw the thing, so nothing is
    owed. `pending` when it says "not yet landed" or "not observed" for a
    vendor we actually probe — the only case a later confirmation could ever
    resolve. For a vendor with no probe nothing can settle it, and marking it
    pending would leave a row waiting for a confirmation that cannot come.
    """
    if ground_truth.get("landed") or ground_truth.get("observed"):
        return CLAIM_SETTLED
    return CLAIM_PENDING if reading.get("probed") else CLAIM_NONE


def feed_incidents_with_status(
    events: list[dict[str, Any]], status: str
) -> dict[str, dict[str, Any]]:
    """Incident key -> one representative row, for rows carrying `status`."""
    matched: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("status") or "").strip().lower() == status:
            matched.setdefault(incident_key(event), event)
    return matched


# ─── What a feed refresh is allowed to do ────────────────────────────────────


@dataclass(frozen=True)
class DiscoveryPlan:
    """The decision about one feed refresh, separated from applying it.

    Keeping this pure is what makes `notify --dry-run` trustworthy: the preview
    and the real run compute the same plan from the same inputs, so the preview
    cannot flatter the run.
    """

    baseline: bool
    fresh: tuple[dict[str, Any], ...]
    mailable: tuple[dict[str, Any], ...]
    historical: tuple[dict[str, Any], ...]
    undatable: tuple[dict[str, Any], ...]
    flooded: bool
    # Rows already on record that became mailable since. "Recorded" is not
    # "decided": the lifecycle exists so an incident can improve, and without
    # this a row filed while its status was non-mailable could never be mailed
    # at all, which silently removed the observed_pending -> observed path.
    ripened: tuple[dict[str, Any], ...] = ()
    # Recorded, never mailed, because the incident's own status says so: a
    # candidate without a verified post id, an observation still on its
    # corroboration hold, a retraction. Kept apart from `historical` because
    # "too old" and "not mailable at this status" are different facts.
    silent: tuple[dict[str, Any], ...] = ()
    adoptions: tuple[tuple[str, str], ...] = ()

    @property
    def new_count(self) -> int:
        return len(self.fresh)


def event_time(event: dict[str, Any]) -> int | None:
    """When the thing happened, as epoch seconds, or None if undatable."""
    for field in ("observed_at", "announced_at"):
        raw = event.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return int(parse_timestamp(raw).timestamp())
        except (ValueError, TypeError):
            continue
    return None


def plan_discovery(
    events: list[dict[str, Any]],
    known_keys: set[str],
    now: int,
    *,
    silenced_keys: set[str] | None = None,
    max_age_seconds: int = MAX_NOTIFY_AGE_SECONDS,
    flood_cap: int = FLOOD_CAP,
    stored_by_event: dict[str, str] | None = None,
    known_incidents: set[str] | None = None,
) -> DiscoveryPlan:
    """Split a feed into: already known, not mailable at this status, too old,
    and mailable.

    Order matters. The status gate runs before the clock, because an incident
    the lifecycle says we never mail is not a thing whose age we then argue
    about. An event with no readable timestamp counts as historical: guessing
    "it must be recent" is how a re-keyed upstream id turns an April
    announcement into tonight's alarm.
    """
    known_keys = known_keys or set()
    silenced = silenced_keys or set()
    fresh = [event for event in events if event["event_key"] not in known_keys]
    # A row recorded while its status was non-mailable is not settled. The
    # lifecycle's whole point is that an incident IMPROVES — observed_pending
    # becomes observed once a witness turns up, candidate becomes announced
    # once a post id is verified — and "recorded" was being read as "decided",
    # so the row was never fresh again and could never be mailed. Anything
    # filed as silent before, and mailable now, re-enters here.
    ripened = [
        event
        for event in events
        if event["event_key"] in silenced and mail_stage(event) == STAGE_NEW
    ]
    mailable: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    undatable: list[dict[str, Any]] = []
    silent: list[dict[str, Any]] = []
    for event in fresh + ripened:
        if mail_stage(event) != STAGE_NEW:
            silent.append(event)
            continue
        happened = event_time(event)
        if happened is None:
            undatable.append(event)
        elif now - happened > max_age_seconds:
            historical.append(event)
        else:
            mailable.append(event)
    return DiscoveryPlan(
        baseline=not known_keys,
        fresh=tuple(fresh),
        mailable=tuple(mailable),
        historical=tuple(historical),
        undatable=tuple(undatable),
        ripened=tuple(ripened),
        flooded=len(mailable) > flood_cap,
        silent=tuple(silent),
        adoptions=plan_adoptions(
            events, stored_by_event or {}, known_incidents or set()
        ),
    )


class Store:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
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

                CREATE TABLE IF NOT EXISTS rate_limits (
                    bucket TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL,
                    count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS known_events (
                    event_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    discovered_at INTEGER NOT NULL,
                    incident_key TEXT NOT NULL DEFAULT '',
                    merged_from TEXT,
                    origin TEXT NOT NULL DEFAULT 'announcement',
                    -- The mail stage this row had when it was recorded. A row
                    -- filed while its status was non-mailable is not settled:
                    -- the lifecycle exists so an incident can improve, and
                    -- this column is what lets it be mailed when it does.
                    recorded_stage TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    incident_key TEXT NOT NULL,
                    email TEXT NOT NULL REFERENCES subscribers(email),
                    stage TEXT NOT NULL DEFAULT 'new'
                        CHECK (stage IN ('new', 'confirmed', 'retracted')),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held')),
                    claim TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    resend_id TEXT,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (incident_key, email, stage)
                );

                CREATE TABLE IF NOT EXISTS webhook_events (
                    svix_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    received_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS owner_alerts (
                    condition TEXT PRIMARY KEY,
                    opened_at INTEGER NOT NULL,
                    notified_at INTEGER,
                    detail TEXT
                );
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(subscribers)")
            }
            migrations = {
                "topics": "TEXT NOT NULL DEFAULT 'anthropic,openai,google'",
                "consent_version": "TEXT",
                "signup_source": "TEXT",
                "confirmation_token_hash": "TEXT",
                "suppressed_at": "INTEGER",
                "suppression_reason": "TEXT",
                # Which kinds of email this address accepts. Everyone who
                # subscribed before follow-ups existed gets all of them: they
                # asked to be told about resets, and a correction to a mail
                # they already received is part of that, not a new topic.
                "stages": "TEXT NOT NULL DEFAULT 'new,confirmed,retracted'",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE subscribers ADD COLUMN {name} {declaration}"
                    )

        self.migrate_known_events_incidents()
        self.migrate_deliveries_held()
        self.migrate_deliveries_incidents()

    # ─── Migrations ──────────────────────────────────────────────────────────

    def migrate_known_events_incidents(self) -> bool:
        """Give every recorded feed row an incident key and an origin.

        Additive, so an ALTER is enough and the copy-and-rename procedure is
        not. The backfill sets `incident_key = event_key`, which is not a
        guess: every one of the 65 rows in the live database is
        announcement-backed and its stored key already equals
        `<vendor>:<id>` — the value `incident_key()` derives — verified against
        a read-only snapshot before this shipped. A probe row cannot be in
        there because the notifier has never read one.
        """
        with self.connect() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(known_events)")
            }
            if "recorded_stage" not in columns:
                conn.execute(
                    "ALTER TABLE known_events ADD COLUMN recorded_stage TEXT "
                    "NOT NULL DEFAULT ''"
                )
            if "incident_key" in columns:
                return False
            conn.execute(
                "ALTER TABLE known_events ADD COLUMN incident_key TEXT NOT NULL DEFAULT ''"
            )
            conn.execute("ALTER TABLE known_events ADD COLUMN merged_from TEXT")
            conn.execute(
                "ALTER TABLE known_events ADD COLUMN origin TEXT NOT NULL "
                "DEFAULT 'announcement'"
            )
            conn.execute(
                "UPDATE known_events SET incident_key = event_key WHERE incident_key = ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS known_events_incident "
                "ON known_events(incident_key)"
            )
        return True

    def migrate_deliveries_incidents(self) -> bool:
        """Re-key deliveries from (event_key, email) to (incident_key, email, stage).

        The live database holds 83 rows that are the record of what real people
        were actually sent; losing one would mean mailing somebody a second
        time about a reset from last week. So this follows the same procedure
        as `migrate_deliveries_held`: copy into a new table inside one
        transaction, assert the row count matched, swap, foreign-key check,
        commit — and roll the whole thing back if anything raises.

        Two deliberate choices in the copy:

        * `incident_key` comes from the known_events row, falling back to the
          delivery's own `event_key`. For every live row those are the same
          string, and the fallback keeps a delivery whose event row was
          somehow pruned rather than dropping the evidence that it was sent.
        * `claim` stays empty. An empty claim never owes a `confirmed`
          follow-up, so the migration cannot make 83 historical mails sprout
          confirmations the day this lands.

        The known_events foreign key is not carried over: one incident is
        several feed rows once an observation and an announcement are fused, so
        `incident_key` is not unique in known_events and cannot be a key
        target. The subscribers foreign key stays, and PRAGMA foreign_key_check
        still runs before the commit.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'"
            ).fetchone()
        if row is None or "incident_key" in (row["sql"] or ""):
            return False

        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.isolation_level = None  # explicit transaction control
            conn.execute("PRAGMA busy_timeout=10000")
            # Must be set outside a transaction, where it is a no-op.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE deliveries_migrated (
                    incident_key TEXT NOT NULL,
                    email TEXT NOT NULL REFERENCES subscribers(email),
                    stage TEXT NOT NULL DEFAULT 'new'
                        CHECK (stage IN ('new', 'confirmed', 'retracted')),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held')),
                    claim TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    resend_id TEXT,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (incident_key, email, stage)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO deliveries_migrated(
                    incident_key, email, stage, status, claim,
                    attempts, resend_id, last_error, updated_at
                )
                SELECT COALESCE(k.incident_key, d.event_key), d.email, 'new',
                       d.status, '', d.attempts, d.resend_id, d.last_error, d.updated_at
                FROM deliveries d
                LEFT JOIN known_events k ON k.event_key = d.event_key
                """
            )
            copied = conn.execute("SELECT COUNT(*) FROM deliveries_migrated").fetchone()[0]
            original = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            if copied != original:
                raise sqlite3.IntegrityError(
                    f"deliveries migration copied {copied} of {original} rows"
                )
            conn.execute("DROP TABLE deliveries")
            conn.execute("ALTER TABLE deliveries_migrated RENAME TO deliveries")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"deliveries migration would orphan {len(violations)} rows"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()
        return True

    def migrate_deliveries_held(self) -> bool:
        """Widen the deliveries CHECK constraint so a delivery can be held.

        A CHECK constraint cannot be altered in place, so the table is
        redefined by the procedure SQLite documents for this: foreign keys
        off, copy into a new table inside one transaction, swap, verify, and
        commit. The live database holds 83 already-sent rows that must
        survive, so this is idempotent (it inspects the stored schema first)
        and rolls back as a whole if anything raises. Returns True when it
        actually migrated.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'"
            ).fetchone()
        if row is None or "'held'" in (row["sql"] or ""):
            return False

        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.isolation_level = None  # explicit transaction control
            conn.execute("PRAGMA busy_timeout=10000")
            # Must be set outside a transaction, where it is a no-op.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE deliveries_migrated (
                    event_key TEXT NOT NULL REFERENCES known_events(event_key),
                    email TEXT NOT NULL REFERENCES subscribers(email),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    resend_id TEXT,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (event_key, email)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO deliveries_migrated(
                    event_key, email, status, attempts, resend_id, last_error, updated_at
                )
                SELECT event_key, email, status, attempts, resend_id, last_error, updated_at
                FROM deliveries
                """
            )
            copied = conn.execute("SELECT COUNT(*) FROM deliveries_migrated").fetchone()[0]
            original = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            if copied != original:
                raise sqlite3.IntegrityError(
                    f"deliveries migration copied {copied} of {original} rows"
                )
            conn.execute("DROP TABLE deliveries")
            conn.execute("ALTER TABLE deliveries_migrated RENAME TO deliveries")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"deliveries migration would orphan {len(violations)} rows"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()
        return True

    def allow_rate(self, bucket: str, limit: int, window_seconds: int = 3600) -> bool:
        current = now_ts()
        window_start = current - (current % window_seconds)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT window_start, count FROM rate_limits WHERE bucket = ?",
                (bucket,),
            ).fetchone()
            if row is None or row["window_start"] != window_start:
                conn.execute(
                    """
                    INSERT INTO rate_limits(bucket, window_start, count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(bucket) DO UPDATE SET
                        window_start = excluded.window_start,
                        count = 1
                    """,
                    (bucket, window_start),
                )
                return True
            if row["count"] >= limit:
                return False
            conn.execute(
                "UPDATE rate_limits SET count = count + 1 WHERE bucket = ?",
                (bucket,),
            )
            return True

    def request_subscription(
        self,
        email: str,
        topics: tuple[str, ...] = VALID_TOPICS,
        confirmation_token_hash: str | None = None,
        cooldown_seconds: int = 300,
    ) -> bool:
        """Return True when a confirmation email should be sent."""
        current = now_ts()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT status, last_confirmation_sent_at, suppression_reason
                FROM subscribers WHERE email = ?
                """,
                (email,),
            ).fetchone()
            if row and (row["status"] == "active" or row["suppression_reason"]):
                return False
            if (
                row
                and row["status"] == "pending"
                and row["last_confirmation_sent_at"]
                and current - row["last_confirmation_sent_at"] < cooldown_seconds
            ):
                return False
            conn.execute(
                """
                INSERT INTO subscribers(
                    email, status, created_at, updated_at,
                    last_confirmation_sent_at, topics, consent_version,
                    signup_source, confirmation_token_hash
                ) VALUES (?, 'pending', ?, ?, ?, ?, ?, 'website', ?)
                ON CONFLICT(email) DO UPDATE SET
                    status = 'pending',
                    updated_at = excluded.updated_at,
                    unsubscribed_at = NULL,
                    last_confirmation_sent_at = excluded.last_confirmation_sent_at,
                    topics = excluded.topics,
                    consent_version = excluded.consent_version,
                    signup_source = excluded.signup_source,
                    confirmation_token_hash = excluded.confirmation_token_hash
                """,
                (
                    email,
                    current,
                    current,
                    current,
                    ",".join(topics),
                    CONSENT_VERSION,
                    confirmation_token_hash,
                ),
            )
            return True

    def confirmation_is_pending(
        self,
        email: str,
        confirmation_token_hash: str,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM subscribers
                WHERE email = ? AND status = 'pending'
                  AND confirmation_token_hash = ?
                  AND suppression_reason IS NULL
                """,
                (email, confirmation_token_hash),
            ).fetchone()
            return row is not None

    def activate(
        self,
        email: str,
        confirmation_token_hash: str | None = None,
    ) -> bool:
        current = now_ts()
        with self.connect() as conn:
            if confirmation_token_hash is None:
                where = "email = ?"
                params: tuple[Any, ...] = (current, current, email)
            else:
                where = (
                    "email = ? AND status = 'pending' "
                    "AND confirmation_token_hash = ? AND suppression_reason IS NULL"
                )
                params = (current, current, email, confirmation_token_hash)
            changed = conn.execute(
                """
                UPDATE subscribers
                SET status = 'active',
                    confirmed_at = ?,
                    unsubscribed_at = NULL,
                    updated_at = ?,
                    confirmation_token_hash = NULL
                WHERE """ + where,
                params,
            ).rowcount
            return changed == 1

    def unsubscribe(self, email: str) -> None:
        current = now_ts()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE subscribers
                SET status = 'unsubscribed', unsubscribed_at = ?, updated_at = ?,
                    confirmation_token_hash = NULL
                WHERE email = ?
                """,
                (current, current, email),
            )

    def subscriber_topics(self, email: str) -> tuple[str, ...] | None:
        preferences = self.subscriber_preferences(email)
        return None if preferences is None else preferences[0]

    def subscriber_preferences(
        self, email: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        """(topics, stages) for an active subscriber, or None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, topics, stages FROM subscribers WHERE email = ?",
                (email,),
            ).fetchone()
            if row is None or row["status"] != "active":
                return None
            return normalize_topics(row["topics"]), normalize_stages(row["stages"])

    def update_topics(
        self,
        email: str,
        topics: tuple[str, ...],
        stages: tuple[str, ...] | None = None,
    ) -> bool:
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE subscribers
                SET topics = ?, stages = COALESCE(?, stages), updated_at = ?
                WHERE email = ? AND status = 'active'
                  AND suppression_reason IS NULL
                """,
                (
                    ",".join(topics),
                    ",".join(stages) if stages is not None else None,
                    now_ts(),
                    email,
                ),
            ).rowcount
            return changed == 1

    def record_webhook(
        self,
        svix_id: str,
        event_type: str,
        recipients: list[str],
    ) -> bool:
        """Idempotently record a Resend event and apply recipient suppression."""
        current = now_ts()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO webhook_events(svix_id, event_type, received_at)
                VALUES (?, ?, ?)
                """,
                (svix_id, event_type, current),
            ).rowcount
            if not inserted:
                return False
            reason = SUPPRESSION_EVENTS.get(event_type)
            if reason:
                for email in recipients:
                    conn.execute(
                        """
                        UPDATE subscribers
                        SET status = 'unsubscribed', unsubscribed_at = ?,
                            suppressed_at = ?, suppression_reason = ?,
                            confirmation_token_hash = NULL, updated_at = ?
                        WHERE email = ?
                        """,
                        (current, current, reason, current, email),
                    )
            return True

    def known_event_keys(self) -> set[str]:
        with self.connect() as conn:
            return {row["event_key"] for row in conn.execute("SELECT event_key FROM known_events")}

    def silenced_event_keys(self) -> set[str]:
        """Rows recorded while their status was not mailable.

        These are the ones the lifecycle expects to improve. Reading them back
        is what turns `observed_pending -> observed` and `candidate ->
        announced` from a plan into a delivery.
        """
        with self.connect() as conn:
            return {
                row["event_key"]
                for row in conn.execute(
                    "SELECT event_key FROM known_events "
                    "WHERE recorded_stage IS NOT NULL AND recorded_stage != ?",
                    (STAGE_NEW,),
                )
                if row["event_key"]
            }

    def stored_incidents(self) -> dict[str, str]:
        """event_key -> the incident key it is currently filed under."""
        with self.connect() as conn:
            return {
                row["event_key"]: row["incident_key"]
                for row in conn.execute(
                    "SELECT event_key, incident_key FROM known_events"
                )
            }

    def known_incident_keys(self) -> set[str]:
        with self.connect() as conn:
            return {
                row["incident_key"]
                for row in conn.execute("SELECT DISTINCT incident_key FROM known_events")
            }

    def incident_origins(self, incident_key_value: str) -> set[str]:
        """Which kinds of evidence this incident is made of."""
        with self.connect() as conn:
            return {
                row["origin"]
                for row in conn.execute(
                    "SELECT DISTINCT origin FROM known_events WHERE incident_key = ?",
                    (incident_key_value,),
                )
            }

    def active_subscribers(self) -> list[tuple[str, set[str]]]:
        with self.connect() as conn:
            return [
                (row["email"], set(normalize_topics(row["topics"])))
                for row in conn.execute(
                    """
                    SELECT email, topics FROM subscribers
                    WHERE status = 'active' AND suppression_reason IS NULL
                    """
                )
            ]

    def adopt_incident(self, old_key: str, new_key: str) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._adopt(conn, old_key, new_key)

    @staticmethod
    def _adopt(conn: sqlite3.Connection, old_key: str, new_key: str) -> int:
        """Move everything filed under `old_key` onto `new_key`.

        Returns the number of deliveries re-filed. A delivery already sent
        under the old key wins over an unsent one under the new key: it is the
        record of an email a person actually received, and the whole point of
        the rename is that they are not sent a second one.
        """
        conn.execute(
            """
            DELETE FROM deliveries WHERE incident_key = :new AND EXISTS (
                SELECT 1 FROM deliveries older
                WHERE older.incident_key = :old
                  AND older.email = deliveries.email
                  AND older.stage = deliveries.stage
            )
            """,
            {"new": new_key, "old": old_key},
        )
        moved = conn.execute(
            "UPDATE deliveries SET incident_key = ? WHERE incident_key = ?",
            (new_key, old_key),
        ).rowcount
        conn.execute(
            """
            UPDATE known_events
            SET incident_key = ?, merged_from = COALESCE(merged_from, ?)
            WHERE incident_key = ?
            """,
            (new_key, old_key, old_key),
        )
        return moved

    def discover_events(self, events: list[dict[str, Any]], *, now: int | None = None) -> "DiscoveryPlan":
        """Record new events and queue the ones that may be mailed.

        The first run records the current feed as a baseline and queues no
        historical mail. Afterwards, two guards stand between a feed and seven
        inboxes: anything first seen more than MAX_NOTIFY_AGE_SECONDS after it
        happened is recorded but never delivered, and a run that turns up more
        than FLOOD_CAP mailable events queues them as 'held' instead of
        'pending' so a person decides.
        """
        current = now_ts() if now is None else now
        with self.connect() as conn:
            # One write transaction for read, decide, rename and queue: two
            # notify ticks can overlap when one hangs, and deciding what is
            # fresh outside the lock would let both of them decide it.
            conn.execute("BEGIN IMMEDIATE")
            known: set[str] = set()
            stored: dict[str, str] = {}
            incidents: set[str] = set()
            for row in conn.execute(
                "SELECT event_key, incident_key FROM known_events"
            ):
                known.add(row["event_key"])
                stored[row["event_key"]] = row["incident_key"]
                incidents.add(row["incident_key"])
            active = [
                (row["email"], set(normalize_topics(row["topics"])))
                for row in conn.execute(
                    """
                    SELECT email, topics FROM subscribers
                    WHERE status = 'active' AND suppression_reason IS NULL
                    """
                )
            ]
            silenced = {
                row["event_key"]
                for row in conn.execute(
                    "SELECT event_key FROM known_events WHERE recorded_stage != ?",
                    (STAGE_NEW,),
                )
            }
            plan = plan_discovery(
                events,
                known,
                current,
                silenced_keys=silenced,
                stored_by_event=stored,
                known_incidents=incidents,
            )
            # Adoption first: a probe-first incident has to be wearing the
            # announcement's key BEFORE that announcement queues anything, or
            # the INSERT OR IGNORE below has nothing to collide with and
            # everybody is mailed a second time.
            for old_key, new_key in plan.adoptions:
                self._adopt(conn, old_key, new_key)
            for event in plan.ripened:
                # It re-entered because its status improved; record the new
                # stage so it cannot re-enter again on every later refresh.
                conn.execute(
                    "UPDATE known_events SET recorded_stage = ?, payload_json = ? "
                    "WHERE event_key = ?",
                    (
                        mail_stage(event) or "",
                        json.dumps(event, separators=(",", ":"), sort_keys=True),
                        event["event_key"],
                    ),
                )
            for event in plan.fresh:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO known_events(
                        event_key, payload_json, discovered_at,
                        incident_key, merged_from, origin, recorded_stage
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_key"],
                        json.dumps(event, separators=(",", ":"), sort_keys=True),
                        current,
                        incident_key(event),
                        event.get("merged_from"),
                        event_origin(event),
                        mail_stage(event) or "",
                    ),
                )
            if not plan.baseline:
                status = "held" if plan.flooded else "pending"
                for event in plan.mailable:
                    key = incident_key(event)
                    for email, topics in active:
                        if event.get("vendor") not in topics:
                            continue
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO deliveries(
                                incident_key, email, stage, status, claim,
                                attempts, updated_at
                            ) VALUES (?, ?, 'new', ?, '', 0, ?)
                            """,
                            (key, email, status, current),
                        )
        return plan

    def preview_events(
        self, events: list[dict[str, Any]], *, now: int | None = None
    ) -> tuple["DiscoveryPlan", list[tuple[str, str]]]:
        """What discover_events WOULD do, computed without writing anything.

        A dry run must be safe to point at the live database, so it takes no
        write lock at all rather than writing and rolling back.
        """
        current = now_ts() if now is None else now
        plan = plan_discovery(
            events,
            self.known_event_keys(),
            current,
            silenced_keys=self.silenced_event_keys(),
            stored_by_event=self.stored_incidents(),
            known_incidents=self.known_incident_keys(),
        )
        queued: list[tuple[str, str]] = []
        if not plan.baseline:
            existing = self.delivered_incidents(STAGE_NEW)
            for event in plan.mailable:
                key = incident_key(event)
                for email, topics in self.active_subscribers():
                    if event.get("vendor") in topics and (key, email) not in existing:
                        queued.append((key, email))
        return plan, queued

    def delivered_incidents(self, stage: str) -> set[tuple[str, str]]:
        """(incident_key, email) pairs that already have a delivery at `stage`."""
        with self.connect() as conn:
            return {
                (row["incident_key"], row["email"])
                for row in conn.execute(
                    "SELECT incident_key, email FROM deliveries WHERE stage = ?",
                    (stage,),
                )
            }

    def held_deliveries(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT incident_key, email, stage FROM deliveries
                    WHERE status = 'held' ORDER BY incident_key, email, stage
                    """
                )
            )

    def release_held(self, *, now: int | None = None) -> int:
        """Move held deliveries into the send queue. Returns how many moved."""
        current = now_ts() if now is None else now
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE deliveries SET status = 'pending', updated_at = ? WHERE status = 'held'",
                (current,),
            )
            moved = cursor.rowcount
        if moved:
            # The condition is over because the operator ended it. Nothing else
            # closes it: dispatch_owner_alerts only auto-closes what it opens.
            self.close_alert("deliveries:flood-held")
        return moved

    def open_alert(self, condition: str, detail: str, *, now: int | None = None) -> bool:
        """Open an owner-alert condition. True only the first time it opens.

        Deduped by condition so a probe that stays blind for six hours produces
        one message, not seventy-two.
        """
        current = now_ts() if now is None else now
        with self.connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO owner_alerts(condition, opened_at, detail)
                VALUES (?, ?, ?)
                """,
                (condition, current, detail[:500]),
            ).rowcount
            return bool(inserted)

    def mark_alert_notified(self, condition: str, *, now: int | None = None) -> None:
        current = now_ts() if now is None else now
        with self.connect() as conn:
            conn.execute(
                "UPDATE owner_alerts SET notified_at = ? WHERE condition = ?",
                (current, condition),
            )

    def close_alert(self, condition: str) -> bool:
        """Close a condition. True if it had been open, so recovery is mailed once."""
        with self.connect() as conn:
            deleted = conn.execute(
                "DELETE FROM owner_alerts WHERE condition = ?", (condition,)
            ).rowcount
            return bool(deleted)

    def open_alert_conditions(self) -> set[str]:
        with self.connect() as conn:
            return {row["condition"] for row in conn.execute("SELECT condition FROM owner_alerts")}

    def alert_row(self, condition: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT condition, opened_at, notified_at, detail FROM owner_alerts "
                "WHERE condition = ?",
                (condition,),
            ).fetchone()

    def reopen_alert(self, condition: str, detail: str, *, now: int | None = None) -> None:
        """Restart the clock on an already-open condition, for a reminder."""
        current = now_ts() if now is None else now
        with self.connect() as conn:
            conn.execute(
                "UPDATE owner_alerts SET opened_at = ?, detail = ? WHERE condition = ?",
                (current, detail[:500], condition),
            )

    # ─── The send queue ──────────────────────────────────────────────────────

    # One incident is several feed rows once an observation and an announcement
    # are fused, so the payload an email is rendered from has to be CHOSEN, not
    # joined: the announcement if there is one (it carries the vendor's own
    # words, which the email quotes verbatim), else the earliest row.
    _INCIDENT_PAYLOAD = """
        SELECT k.payload_json FROM known_events k
        WHERE k.incident_key = d.incident_key
        ORDER BY (k.origin = 'announcement') DESC, k.discovered_at, k.event_key
        LIMIT 1
    """
    _INCIDENT_DISCOVERED = """
        SELECT MIN(k2.discovered_at) FROM known_events k2
        WHERE k2.incident_key = d.incident_key
    """

    def pending_deliveries(self, max_attempts: int = 5) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT d.incident_key, d.email, d.stage, d.claim, d.attempts,
                           ({self._INCIDENT_PAYLOAD}) AS payload_json
                    FROM deliveries d
                    JOIN subscribers s USING(email)
                    WHERE d.status IN ('pending', 'failed')
                      AND d.attempts < ?
                      AND s.status = 'active'
                      AND ({self._INCIDENT_PAYLOAD}) IS NOT NULL
                    ORDER BY ({self._INCIDENT_DISCOVERED}), d.email, d.stage
                    """,
                    (max_attempts,),
                )
            )

    def confirmation_candidates(self) -> list[sqlite3.Row]:
        """Sent `new` mails that left something open and have no follow-up yet.

        `claim = 'pending'` is the mail's own record that it said "not yet
        landed" or "not observed on our account". Reading today's feed instead
        would mail a confirmation to people who were already told it had
        landed. An empty claim — every row that predates this phase — is never
        a candidate.
        """
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT DISTINCT d.incident_key, d.email, s.stages,
                           ({self._INCIDENT_PAYLOAD}) AS payload_json
                    FROM deliveries d
                    JOIN subscribers s USING(email)
                    WHERE d.stage = 'new' AND d.status = 'sent' AND d.claim = ?
                      AND s.status = 'active' AND s.suppression_reason IS NULL
                      AND ({self._INCIDENT_PAYLOAD}) IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM deliveries done
                          WHERE done.incident_key = d.incident_key
                            AND done.email = d.email
                            AND done.stage = 'confirmed'
                      )
                    ORDER BY d.incident_key, d.email
                    """,
                    (CLAIM_PENDING,),
                )
            )

    def retraction_candidates(self, incident_key_value: str) -> list[sqlite3.Row]:
        """Recipients of a sent `new` for this incident, with no retraction yet."""
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT d.email, s.stages FROM deliveries d
                    JOIN subscribers s USING(email)
                    WHERE d.incident_key = ? AND d.stage = 'new' AND d.status = 'sent'
                      AND s.status = 'active' AND s.suppression_reason IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM deliveries done
                          WHERE done.incident_key = d.incident_key
                            AND done.email = d.email
                            AND done.stage = 'retracted'
                      )
                    ORDER BY d.email
                    """,
                    (incident_key_value,),
                )
            )

    def queue_follow_up(
        self,
        incident_key_value: str,
        email: str,
        stage: str,
        *,
        now: int | None = None,
    ) -> bool:
        """Queue one follow-up. False when one already exists (at most one)."""
        current = now_ts() if now is None else now
        with self.connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO deliveries(
                    incident_key, email, stage, status, claim, attempts, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, 0, ?)
                """,
                (incident_key_value, email, stage, CLAIM_SETTLED, current),
            ).rowcount
            return bool(inserted)

    def mark_sent(
        self,
        incident_key_value: str,
        email: str,
        resend_id: str,
        *,
        stage: str = STAGE_NEW,
        claim: str = CLAIM_NONE,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'sent', attempts = attempts + 1, resend_id = ?,
                    claim = ?, last_error = NULL, updated_at = ?
                WHERE incident_key = ? AND email = ? AND stage = ?
                """,
                (resend_id, claim, now_ts(), incident_key_value, email, stage),
            )

    def mark_failed(
        self,
        incident_key_value: str,
        email: str,
        error: str,
        *,
        stage: str = STAGE_NEW,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'failed', attempts = attempts + 1,
                    last_error = ?, updated_at = ?
                WHERE incident_key = ? AND email = ? AND stage = ?
                """,
                (error[:240], now_ts(), incident_key_value, email, stage),
            )


class ResendClient:
    def __init__(self, api_key: str, from_email: str):
        self.api_key = api_key
        self.from_email = from_email

    def send(
        self,
        *,
        to: str,
        subject: str,
        html_body: str,
        text_body: str,
        idempotency_key: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "from": self.from_email,
            "to": [to],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        if headers:
            payload["headers"] = headers
        request = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key[:256],
                "User-Agent": "ai-reset-watch/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = "unknown"
            try:
                body = json.loads(exc.read(4096))
                detail = str(body.get("name") or body.get("message") or "unknown")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            raise RuntimeError(f"Resend HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Resend network error: {exc.reason}") from exc
        message_id = result.get("id")
        if not message_id:
            raise RuntimeError("Resend response did not include a message id")
        return str(message_id)


class SubscriptionApp:
    def __init__(self, config: Config, store: Store, mailer: ResendClient):
        self.config = config
        self.store = store
        self.mailer = mailer

    @property
    def mailer_is_live(self) -> bool:
        """False when Config was built without real secrets (a dry run)."""
        return self.config.api_key != "dry-run-no-key"

    def delivery_idempotency_key(
        self, incident_key_value: str, email: str, stage: str = STAGE_NEW
    ) -> str:
        """Resend's dedupe key for one delivery.

        A `new` mail's input is byte-identical to the pre-P4 one —
        "<key>:<email>", and an announcement's incident key IS the event key it
        used to be filed under — so every row already sent keeps the guarantee
        it was sent under. A follow-up must not collide with the mail it
        follows, so it carries its stage.
        """
        suffix = "" if stage == STAGE_NEW else f":{stage}"
        return self.opaque_bucket("notify", f"{incident_key_value}:{email}{suffix}")

    def opaque_bucket(self, kind: str, value: str) -> str:
        digest = hmac.new(
            self.config.secret.encode(),
            f"{kind}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{kind}:{digest}"

    def make_token(self, email: str, purpose: str, ttl_seconds: int) -> str:
        payload = json.dumps(
            {"email": email, "purpose": purpose, "exp": now_ts() + ttl_seconds},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = b64url_encode(payload)
        signature = hmac.new(
            self.config.secret.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).digest()
        return f"{encoded}.{b64url_encode(signature)}"

    def parse_token(self, token: str, purpose: str) -> str:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected = hmac.new(
                self.config.secret.encode(),
                encoded.encode(),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(expected, b64url_decode(supplied_signature)):
                raise ValueError("invalid signature")
            payload = json.loads(b64url_decode(encoded))
            if payload.get("purpose") != purpose or int(payload.get("exp", 0)) < now_ts():
                raise ValueError("expired or wrong-purpose token")
            return normalize_email(str(payload["email"]))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("This link is invalid or has expired.") from exc

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def subscribe(
        self,
        raw_email: str,
        remote_ip: str,
        raw_topics: Any = VALID_TOPICS,
    ) -> None:
        email = normalize_email(raw_email)
        topics = normalize_topics(raw_topics)
        if not self.store.allow_rate(self.opaque_bucket("ip", remote_ip), limit=10):
            raise RuntimeError("Too many requests. Please try again later.")
        if not self.store.allow_rate(self.opaque_bucket("email", email), limit=4):
            raise RuntimeError("Too many requests. Please try again later.")
        token = self.make_token(email, "confirm", 24 * 3600)
        if not self.store.request_subscription(
            email,
            topics,
            self.token_hash(token),
        ):
            return
        confirm_url = f"{self.config.public_url}/api/confirm?{urllib.parse.urlencode({'token': token})}"
        safe_url = html.escape(confirm_url, quote=True)
        topic_names = ", ".join(VENDOR_LABELS[topic] for topic in topics)
        html_body = email_shell(
            "Confirm reset alerts",
            (
                "<p>Confirm the providers you chose for AI Reset Watch alerts:</p>"
                f"<p><strong>{html.escape(topic_names)}</strong></p>"
                f'<p><a class="button" href="{safe_url}">Review and confirm</a></p>'
                "<p class=\"muted\">The link opens a confirmation page and expires "
                "in 24 hours. If you didn’t request it, ignore this email.</p>"
            ),
        )
        text_body = (
            "Confirm your AI Reset Watch subscription:\n\n"
            f"{confirm_url}\n\n"
            "This link expires in 24 hours. If you did not request it, ignore this email."
        )
        bucket = now_ts() // 300
        idem = self.opaque_bucket("confirm", f"{email}:{bucket}")
        self.mailer.send(
            to=email,
            subject="Confirm your AI Reset Watch alerts",
            html_body=html_body,
            text_body=text_body,
            idempotency_key=idem,
        )

    def validate_confirmation(self, token: str) -> str:
        email = self.parse_token(token, "confirm")
        if not self.store.confirmation_is_pending(email, self.token_hash(token)):
            raise ValueError("This link has already been used or replaced.")
        return email

    def confirm(self, token: str) -> None:
        email = self.validate_confirmation(token)
        if not self.store.activate(email, self.token_hash(token)):
            raise ValueError("This link has already been used or replaced.")

    def unsubscribe(self, token: str) -> None:
        self.store.unsubscribe(self.parse_token(token, "unsubscribe"))

    def preferences(self, token: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        email = self.parse_token(token, "preferences")
        preferences = self.store.subscriber_preferences(email)
        if preferences is None:
            raise ValueError("This subscription is no longer active.")
        return email, preferences[0], preferences[1]

    def update_preferences(
        self, token: str, raw_topics: Any, raw_stages: Any = None
    ) -> None:
        email, _, current_stages = self.preferences(token)
        stages = current_stages if raw_stages is None else normalize_stages(raw_stages)
        if not self.store.update_topics(email, normalize_topics(raw_topics), stages):
            raise ValueError("This subscription is no longer active.")

    def handle_webhook(
        self,
        headers: Any,
        body: bytes,
    ) -> bool:
        svix_id = str(headers.get("svix-id", ""))
        if not verify_svix_signature(
            self.config.webhook_secret,
            svix_id,
            str(headers.get("svix-timestamp", "")),
            str(headers.get("svix-signature", "")),
            body,
        ):
            raise ValueError("Invalid webhook signature.")
        try:
            payload = json.loads(body)
            event_type = str(payload["type"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid webhook payload.") from exc
        recipients: list[str] = []
        raw_recipients = payload.get("data", {}).get("to", [])
        if isinstance(raw_recipients, str):
            raw_recipients = [raw_recipients]
        if isinstance(raw_recipients, list):
            for value in raw_recipients:
                try:
                    recipients.append(normalize_email(str(value)))
                except ValueError:
                    continue
        return self.store.record_webhook(svix_id, event_type, recipients)

    def notification_message(
        self,
        email: str,
        event: dict[str, Any],
        *,
        ground_truth: dict[str, Any] | None = None,
        now: int | None = None,
        stage: str = STAGE_NEW,
    ) -> tuple[str, str, str, str, dict[str, str]]:
        """Six blocks: eyebrow, headline, meta, the post verbatim, ground truth, footer.

        The vendor's own words are quoted rather than summarised, because a
        paraphrase of "lands end of day" and a paraphrase of "we have reset"
        look identical once they are both called a "reset update". Everything
        around the quote is generated from fields, so a hostile or malformed
        announcement can never write the subject line.

        A `confirmed` or `retracted` follow-up is the same six blocks with its
        own eyebrow, headline and opening line, so the reader sees the same
        evidence they saw the first time and can compare it themselves.
        """
        reading = describe_event(event)
        subject = reading["subject"]
        if ground_truth is None:
            ground_truth = ground_truth_line(
                reading["vendor"],
                is_forecast=reading["is_forecast"],
                now=now_ts() if now is None else now,
                state_dir=self.config.state_dir,
                kind=reading["kind"],
                announced_at=event_time(event),
            )

        if ground_truth.get("landed"):
            # The post forecast something; our own account has since seen it
            # happen. A subject reading "not yet landed" over a body reading
            # "landed at 9:20 PM" is worse than either line alone.
            subject = landed_subject(reading["kind"], reading["product"], subject)

        product = reading["product"]
        eyebrow = reading["eyebrow"]
        headline = reading["headline"]
        if stage == STAGE_CONFIRMED:
            subject = landed_subject(
                reading["kind"],
                product,
                CONFIRMED_FALLBACK_SUBJECT.format(product=product),
            )
            headline = CONFIRMED_HEADLINE.format(product=product)
            eyebrow = f"{eyebrow} · Confirmed"
        elif stage == STAGE_RETRACTED:
            subject = RETRACTION_SUBJECT.format(product=product)
            headline = RETRACTION_HEADLINE.format(product=product)
            eyebrow = f"{eyebrow} · Withdrawn"
        lead = FOLLOW_UP_LEAD.get(stage, "")

        unsubscribe_token = self.make_token(email, "unsubscribe", 3650 * 24 * 3600)
        unsubscribe_url = (
            f"{self.config.public_url}/api/unsubscribe?"
            f"{urllib.parse.urlencode({'token': unsubscribe_token})}"
        )
        preferences_token = self.make_token(email, "preferences", 3650 * 24 * 3600)
        preferences_url = (
            f"{self.config.public_url}/api/preferences?"
            f"{urllib.parse.urlencode({'token': preferences_token})}"
        )
        source_url = safe_http_url(str(event.get("url") or ""), self.config.public_url)
        method_url = f"{self.config.public_url}/methodology.html"

        announcer = str(event.get("announcer") or "").strip()
        when = reading["announced_at"]
        try:
            posted = format_pacific_with_utc(str(when)) if when else ""
        except (ValueError, TypeError):
            posted = str(when or "")
        meta_parts = [f"Posted by @{announcer.lstrip('@')}"] if announcer else ["Posted"]
        if posted:
            meta_parts.append(posted)
        meta = " · ".join(meta_parts)

        quoted = paragraphs(reading["text"])
        if not quoted and event_origin(event) == ORIGIN_PROBE:
            # A probe-first incident has no post to quote — that is the whole
            # point of it. Quote the probe's own two sentences instead, which
            # are what the site shows for the same observation, rather than
            # telling the reader that a post they were never promised is empty.
            quoted = [
                str(event.get(field) or "").strip()
                for field in ("headline", "evidence")
                if str(event.get(field) or "").strip()
            ]
        if quoted:
            quote_html = "".join(f"<p>{html.escape(block)}</p>" for block in quoted)
        else:
            # A row can reach us with no text: the fetcher treats an empty text
            # as a cosmetic upstream quirk rather than freezing the column. Say
            # that plainly instead of shipping an empty quote box.
            quote_html = (
                "<p><em>The source post carried no text. "
                "Open the original to read it.</em></p>"
            )
        gt_line = str(ground_truth.get("line", ""))
        if stage == STAGE_RETRACTED:
            # A withdrawal must not re-assert what it is withdrawing. The
            # ground-truth block is recomputed live at send time, so on a
            # retraction it would print the CURRENT reading under the heading
            # "What our own account shows" — next to a headline saying the
            # observation has been withdrawn. Nothing guarantees the two agree,
            # and the reader has no way to tell which one is being retracted.
            # State the withdrawal itself and nothing else.
            gt_label = "What we told you, and why it no longer stands"
            gt_line = (
                "Our probe recorded this clear and later withdrew it: the window "
                "went back to the level it held before, on the anchor it had "
                "before, so the clear did not happen. We are correcting our own "
                "observation here, not the vendor's announcement."
            )
        else:
            gt_label = "What our own account shows"

        html_body = email_shell(
            html.escape(headline),
            (
                f'<p class="eyebrow">{html.escape(eyebrow)}</p>'
                + (f"<p><strong>{html.escape(lead)}</strong></p>" if lead else "")
                + f'<p class="muted">{html.escape(meta)}</p>'
                f"<blockquote>{quote_html}</blockquote>"
                f'<p><a class="button" href="{html.escape(source_url, quote=True)}">'
                f"View the original post</a></p>"
                f'<p class="ground"><strong>{html.escape(gt_label)}.</strong> '
                f"{html.escape(gt_line)}</p>"
                f'<p class="muted">AI Reset Watch checks announcements against a '
                f'quota probe running on a real paid account. '
                f'<a href="{html.escape(method_url, quote=True)}">How we verify resets</a> · '
                f'<a href="{html.escape(preferences_url, quote=True)}">Manage providers</a> · '
                f'<a href="{html.escape(unsubscribe_url, quote=True)}">Unsubscribe</a>.</p>'
            ),
        )
        text_body = "\n".join(
            [
                eyebrow,
                "",
                headline,
                *([lead] if lead else []),
                meta,
                "",
                *([f"> {block}" for block in quoted]
                  or ["> (the source post carried no text)"]),
                "",
                f"{gt_label}: {gt_line}",
                "",
                f"Original post: {source_url}",
                f"How we verify resets: {method_url}",
                "",
                f"Manage providers: {preferences_url}",
                f"Unsubscribe: {unsubscribe_url}",
            ]
        )
        headers = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        return subject, html_body, text_body, unsubscribe_url, headers

    def owner_message(self, subject: str, body: str) -> tuple[str, str, str]:
        """An operational alert to the owner, never to subscribers."""
        full_subject = f"[ai-resets alert] {subject}"
        html_body = email_shell(
            html.escape(subject),
            f"<p>{html.escape(body)}</p>"
            f'<p class="muted">Sent to the AI Reset Watch operator only. '
            f"This address is not a subscriber list.</p>",
        )
        return full_subject, html_body, f"{subject}\n\n{body}\n"

def email_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<style>
body{{margin:0;background:#f2f2f7;color:#1d1d1f;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.card{{max-width:560px;margin:32px auto;padding:32px;background:#fff;border-radius:18px}}
h1{{margin:0 0 16px;font-size:25px;letter-spacing:-.02em}}p{{margin:0 0 18px}}
.button{{display:inline-block;padding:11px 18px;border-radius:10px;background:#0071e3;color:#fff!important;text-decoration:none;font-weight:600}}
.muted{{color:#6e6e73;font-size:13px}}.muted a{{color:#6e6e73}}.eyebrow{{color:#6e6e73;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
blockquote{{margin:0 0 18px;padding:14px 18px;border-left:3px solid #d2d2d7;background:#fafafc;color:#1d1d1f}}blockquote p:last-child{{margin:0}}.ground{{padding:14px 18px;border-radius:10px;background:#f5f5f7;font-size:15px}}
</style></head><body><div class="card"><h1>{title}</h1>{body}</div></body></html>"""


def action_shell(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<meta name="robots" content="noindex">
<title>{html.escape(title)} · AI Reset Watch</title>
<style>
:root{{--paper:#f2efe7;--ink:#11120f;--lime:#d9ff43;--line:#c9c6bd}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:520px;margin:10vh auto;padding:38px;background:#fff;border:1px solid var(--line);box-shadow:10px 10px 0 var(--ink)}}
.eyebrow{{margin:0 0 14px;font:600 12px/1.2 ui-monospace,monospace;text-transform:uppercase;letter-spacing:.12em}}
h1{{margin:0 0 12px;font:600 34px/1.05 Georgia,serif;letter-spacing:-.03em}}p{{color:#55584f}}
fieldset{{border:0;padding:0;margin:24px 0}}legend{{font-weight:700;margin-bottom:10px}}
label{{display:block;padding:9px 0}}input{{margin-right:9px;accent-color:#11120f}}
button{{border:1px solid var(--ink);padding:12px 18px;background:var(--lime);color:var(--ink);font:inherit;font-weight:700;cursor:pointer;box-shadow:4px 4px 0 var(--ink)}}
.danger{{background:#11120f;color:#fff}}a{{color:inherit}}
@media(max-width:600px){{main{{margin:24px 18px;padding:28px;box-shadow:6px 6px 0 var(--ink)}}}}
</style></head><body><main><p class="eyebrow">AI Reset Watch</p>{body}</main></body></html>"""


def flatten_events(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row of the published feed the notifier may reason about.

    Own-account observations are read as well as announcements, because a reset
    this account SEES before anyone posts about it is the case this whole
    tracker exists for — the 2026-08-30 clear landed here 2.5 minutes before
    @thsottiaux posted it. They are recorded, and `mail_stage` decides whether
    they may ever be mailed; an unfused observation may not.

    A row the probe marked non-public is dropped outright. Those say what the
    OWNER did with their own account (spending a banked credit), and build.py
    already filters them; this is the second lock. The one exception is a row
    whose status is `retracted`: withdrawing a reading is precisely the thing
    we must still see, and it can only ever reach people who were sent the
    reading — which a never-public row was not.
    """
    flattened: list[dict[str, Any]] = []
    for vendor, vendor_data in data.get("vendors", {}).items():
        rows = [(ORIGIN_ANNOUNCEMENT, raw) for raw in vendor_data.get("events", [])]
        rows += [(ORIGIN_PROBE, raw) for raw in vendor_data.get("observations", [])]
        for default_origin, raw in rows:
            if not isinstance(raw, dict):
                continue
            if raw.get("public") is False and raw.get("status") != STATUS_RETRACTED:
                continue
            event = dict(raw)
            event["vendor"] = vendor
            event.setdefault("origin", default_origin)
            stable_id = str(event.get("id") or event.get("event_id") or "")
            if stable_id:
                event["event_key"] = f"{vendor}:{stable_id}"
            else:
                # The incident key is derived from the clear's bracket, so a
                # re-export that only re-renders its own evidence sentence
                # keeps the same key. Hashing the payload — what this did
                # before — would have made that a second reset and a second
                # email.
                event["event_key"] = incident_key(event)
            flattened.append(event)
    return flattened


def health_conditions(
    config: Config,
    health: dict[str, Any],
    data: dict[str, Any],
    now: int,
) -> dict[str, str]:
    """Everything currently wrong that the owner should hear about, once.

    This runs on every notify tick, including ticks with no new events —
    which is the whole point. On 2026-09-03 the probe failed 35 polls in a
    row and the only trace was one journal line nobody reads.
    """
    conditions: dict[str, str] = {}

    status = probe_status(health, "codex", now)
    age = stale_age_line(status)
    if status["status"] == STATUS_ABSENT:
        conditions["probe:codex:absent"] = (
            "The Codex probe has written no health file. It is either not "
            "running or running an older build."
        )
    elif status["status"] == "blind":
        conditions["probe:codex:blind"] = (
            f"The Codex probe has not recorded a successful read for {age} "
            f"({status.get('consecutive_failures') or 0} consecutive failures). "
            "Announcements arriving now cannot be checked against a real account."
        )
    elif status["status"] == "throttled":
        conditions["probe:codex:throttled"] = f"The Codex probe is throttled ({age} since its last read)."
    elif status["status"] == "token_stale":
        conditions["probe:codex:token"] = "The Codex probe's credential is stale; it cannot read the quota."

    generated_at = data.get("generated_at")
    if isinstance(generated_at, str) and generated_at:
        try:
            built = int(parse_timestamp(generated_at).timestamp())
        except (ValueError, TypeError):
            built = None
        if built is not None and now - built > DATA_STALE_SECONDS:
            conditions["data:stale"] = (
                f"The published feed is {describe_age(now - built)} old; the "
                "five-minute publish pipeline has stopped."
            )
    return conditions


def ground_truth_for(
    config: Config, event: dict[str, Any], now: int
) -> dict[str, Any]:
    """The one sentence about our own accounts for this event, at this instant."""
    reading = describe_event(event)
    return ground_truth_line(
        reading["vendor"],
        is_forecast=reading["is_forecast"],
        now=now,
        state_dir=config.state_dir,
        kind=reading["kind"],
        announced_at=event_time(event),
    )


def plan_confirmations(
    config: Config,
    candidates: list[sqlite3.Row],
    now: int,
) -> list[tuple[str, str]]:
    """The (incident, recipient) pairs owed the one promised confirmation.

    A `new` mail that said "not yet landed" or "not observed on our account"
    is a promise: we said we would not write again unless it landed. This is
    the only thing that discharges it, and the primary key on
    (incident_key, email, stage) is what makes "at most one" structural rather
    than a matter of getting this loop right.
    """
    settled: dict[str, bool] = {}
    owed: list[tuple[str, str]] = []
    for row in candidates:
        if STAGE_CONFIRMED not in normalize_stages(row["stages"]):
            continue
        key = row["incident_key"]
        if key not in settled:
            truth = ground_truth_for(config, json.loads(row["payload_json"]), now)
            settled[key] = bool(truth.get("landed") or truth.get("observed"))
        if settled[key]:
            owed.append((key, row["email"]))
    return owed


def plan_retractions(
    store: "Store",
    events: list[dict[str, Any]],
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Who must be told we withdrew our own reading, and what the owner hears.

    The hard rule: a probe retraction may never "correct" a vendor's own
    announcement. If the incident has an announcement in it, the vendor said a
    reset happened and our account has stopped showing it — that is
    `reverted_on_our_account`, a fact about this machine, and it goes to the
    owner. Telling seven strangers that a vendor's post was wrong on the
    strength of one account's counter is exactly the over-claim this whole
    phase removes.
    """
    queued: list[tuple[str, str]] = []
    conditions: dict[str, str] = {}
    for key, event in feed_incidents_with_status(events, STATUS_RETRACTED).items():
        if ORIGIN_ANNOUNCEMENT in store.incident_origins(key):
            conditions[f"incident:reverted:{key}"] = (
                f"{key} was retracted on our own account, but the incident is "
                "announcement-backed. No subscriber correction was sent; the "
                "vendor's own post stands."
            )
            continue
        for row in store.retraction_candidates(key):
            if STAGE_RETRACTED in normalize_stages(row["stages"]):
                queued.append((key, row["email"]))
    for key in feed_incidents_with_status(events, STATUS_REVERTED):
        conditions[f"incident:reverted:{key}"] = (
            f"{key} reverted on our own account after the vendor announced it. "
            "Site footnote only; no subscriber was written to."
        )
    return queued, conditions


def dispatch_owner_alerts(
    app: "SubscriptionApp",
    conditions: dict[str, str],
    *,
    dry_run: bool = False,
) -> int:
    """Mail the owner once when a condition opens and once when it clears.

    Only conditions this function's own inputs can prove are over get closed
    here — the probe and feed health checks, which are recomputed from live
    signals on every tick. A held flood batch, a failed unit or a reversion is
    opened elsewhere and stays open until the thing that opened it says
    otherwise; auto-closing those mailed "recovered: deliveries:flood-held"
    five minutes after the hold, with the batch still sitting unsent.
    """
    owner = app.config.owner_email
    already_open = app.store.open_alert_conditions()
    messages: list[tuple[str, str, str]] = []

    for condition, detail in sorted(conditions.items()):
        newly_open = (
            condition not in already_open
            if dry_run
            else app.store.open_alert(condition, detail)
        )
        if newly_open:
            messages.append((condition, condition, detail))
    managed = {
        condition
        for condition in already_open
        if condition.startswith(MANAGED_ALERT_PREFIXES)
    }
    for condition in sorted(managed - set(conditions)):
        cleared = True if dry_run else app.store.close_alert(condition)
        if cleared:
            messages.append((condition, f"recovered: {condition}", "This condition has cleared."))

    if not messages:
        return 0
    if not owner:
        for _, subject, detail in messages:
            print(f"ALERT (no OWNER_EMAIL set, not mailed): {subject} — {detail}")
        return 0
    if dry_run:
        for _, subject, detail in messages:
            print(f"would alert {owner}: [ai-resets alert] {subject} — {detail}")
        return len(messages)

    sent = 0
    for condition, subject, detail in messages:
        full_subject, html_body, text_body = app.owner_message(subject, detail)
        try:
            app.mailer.send(
                to=owner,
                subject=full_subject,
                html_body=html_body,
                text_body=text_body,
                idempotency_key=app.opaque_bucket("alert", f"{condition}:{subject}"),
                headers={},
            )
        except RuntimeError as exc:
            # Never silent: if the alert channel is down, the unit must fail so
            # `systemctl --failed` shows it.
            print(f"ALERT UNDELIVERED {subject}: {exc}", file=sys.stderr)
        else:
            app.store.mark_alert_notified(condition)
            sent += 1
    return sent


def run_notifier(
    config: Config,
    app: "SubscriptionApp",
    *,
    dry_run: bool = False,
    release: bool = False,
    now: int | None = None,
) -> int:
    """One clock for the whole run: the plan, the ground-truth line and the
    health check must all describe the same instant, or an email can claim a
    reading it did not make."""
    with config.data_file.open() as handle:
        data = json.load(handle)
    events = flatten_events(data)
    now = now_ts() if now is None else now
    health = load_probe_health(config.state_dir)

    if release:
        moved = app.store.release_held(now=now)
        print(f"released {moved} held deliveries")

    if dry_run:
        plan, queued = app.store.preview_events(events, now=now)
        confirmations = plan_confirmations(
            config, app.store.confirmation_candidates(), now
        )
        retractions, reversions = plan_retractions(app.store, events)
        print(
            f"DRY RUN: feed={len(events)} new={plan.new_count} "
            f"historical={len(plan.historical)} silent={len(plan.silent)} "
            f"mailable={len(plan.mailable)} flooded={plan.flooded} "
            f"queued={len(queued)} confirmed={len(confirmations)} "
            f"retracted={len(retractions)}"
        )
        if plan.baseline:
            print("  (baseline run: nothing would be delivered)")
        for old_key, new_key in plan.adoptions:
            print(f"  would adopt {old_key} -> {new_key}")
        for event in plan.historical:
            print(f"  historical, not queued: {event['event_key']} {event.get('announced_at', '')}")
        for event in plan.undatable:
            print(f"  undatable, not queued: {event['event_key']}")
        for event in plan.silent:
            print(
                f"  status {event.get('status') or 'unfused'}, not queued: "
                f"{event['event_key']}"
            )
        for event in plan.mailable:
            reading = describe_event(event)
            truth = ground_truth_for(config, event, now)
            key = incident_key(event)
            recipients = sum(1 for queued_key, _ in queued if queued_key == key)
            verb = "HELD" if plan.flooded else "would send"
            print(f"  {verb} to {recipients}: {reading['subject']}")
            print(f"      ground truth: {truth['line']}")
        for key, email in confirmations:
            print(f"  would confirm to {email}: {key}")
        for key, email in retractions:
            print(f"  would retract to {email}: {key}")
        dispatch_owner_alerts(
            app,
            {**health_conditions(config, health, data, now), **reversions},
            dry_run=True,
        )
        if not app.mailer_is_live:
            print("  (no Resend credential in this shell: nothing could be sent from here)")
        return 0

    plan = app.store.discover_events(events, now=now)
    for old_key, new_key in plan.adoptions:
        print(f"ADOPTED {old_key} -> {new_key}")
    if plan.baseline:
        print(f"initialized notification baseline with {plan.new_count} existing events")
        dispatch_owner_alerts(app, health_conditions(config, health, data, now), dry_run=False)
        return 0

    if plan.historical:
        print(
            f"HISTORICAL SKIPPED n={len(plan.historical)}: "
            + ", ".join(event["event_key"] for event in plan.historical[:10])
        )
    if plan.undatable:
        # Separated from "too old" on purpose: an undatable event means a
        # source is emitting rows this pipeline cannot reason about, which is
        # a bug to fix, not a quiet age skip.
        print(
            f"UNDATABLE SKIPPED n={len(plan.undatable)}: "
            + ", ".join(event["event_key"] for event in plan.undatable[:10])
        )
    if plan.flooded:
        print(
            f"FLOOD HELD n={len(plan.mailable)} (cap {FLOOD_CAP}): "
            + ", ".join(event["event_key"] for event in plan.mailable)
            + " — release with `subscriptions.py notify --release`"
        )
        app.store.open_alert(
            "deliveries:flood-held",
            f"{len(plan.mailable)} events arrived in one refresh and are held unsent.",
        )

    # A follow-up is decided before the send loop so it can go out on the same
    # tick it becomes owed, and both kinds are queued as ordinary deliveries:
    # one send path, one retry path, one idempotency key shape.
    confirmations = plan_confirmations(config, app.store.confirmation_candidates(), now)
    for key, email in confirmations:
        app.store.queue_follow_up(key, email, STAGE_CONFIRMED, now=now)
    retractions, reversions = plan_retractions(app.store, events)
    for key, email in retractions:
        app.store.queue_follow_up(key, email, STAGE_RETRACTED, now=now)

    truths: dict[str, dict[str, Any]] = {}
    sent = 0
    failed = 0
    for row in app.store.pending_deliveries():
        event = json.loads(row["payload_json"])
        reading = describe_event(event)
        announced = event_time(event)
        stage = row["stage"]
        cache_key = f"{reading['vendor']}:{reading['kind']}:{reading['is_forecast']}:{announced}"
        if cache_key not in truths:
            truths[cache_key] = ground_truth_for(config, event, now)
        truth = truths[cache_key]
        subject, html_body, text_body, _, headers = app.notification_message(
            row["email"], event, ground_truth=truth, now=now, stage=stage
        )
        idem = app.delivery_idempotency_key(row["incident_key"], row["email"], stage)
        try:
            message_id = app.mailer.send(
                to=row["email"],
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                idempotency_key=idem,
                headers=headers,
            )
        except RuntimeError as exc:
            app.store.mark_failed(
                row["incident_key"], row["email"], str(exc), stage=stage
            )
            failed += 1
        else:
            # The claim is recorded from the message that was actually sent, so
            # a later run decides the follow-up from what this person read.
            claim = (
                settled_claim(reading, truth) if stage == STAGE_NEW else CLAIM_SETTLED
            )
            app.store.mark_sent(
                row["incident_key"],
                row["email"],
                message_id,
                stage=stage,
                claim=claim,
            )
            sent += 1
        time.sleep(0.22)  # Resend's default team limit is five requests/second.

    alerts = dispatch_owner_alerts(
        app,
        {**health_conditions(config, health, data, now), **reversions},
        dry_run=False,
    )
    print(
        f"discovered={plan.new_count} historical={len(plan.historical)} "
        f"silent={len(plan.silent)} sent={sent} failed={failed} "
        f"confirmed={len(confirmations)} retracted={len(retractions)} alerts={alerts}"
    )
    return 1 if failed else 0


def run_unit_alert(
    app: "SubscriptionApp",
    unit: str,
    *,
    detail: str = "",
    clear: bool = False,
    now: int | None = None,
) -> int:
    """Tell the owner that a systemd unit failed, at most once per outage.

    This is what `infra/ai-resets-alert@.service` runs. The whole design
    problem is that systemd fires `OnFailure=` on EVERY restart cycle, and
    ai-resets-probe.service carries `Restart=always` with `RestartSec=10`: an
    undeduped alert unit is 8,640 emails a day. So the trigger is deduped
    through the owner_alerts table that already exists for probe blindness,
    keyed by unit — and, because one mail per outage forever would mean a
    fresh failure next week never pages, a trigger more than
    UNIT_ALERT_REPEAT_SECONDS after the last one sends one reminder.

    Exit status is 0 even when nothing is mailed: this unit is named in another
    unit's OnFailure=, and a failing alert unit is noise on top of an outage.
    """
    current = now_ts() if now is None else now
    condition = f"unit:{unit}:failed"
    if clear:
        if not app.store.close_alert(condition):
            print(f"no open alert for {unit}")
            return 0
        subject, body = f"recovered: {condition}", f"{unit} is running again."
    else:
        opened = app.store.open_alert(
            condition,
            detail or f"systemd reports {unit} failed.",
            now=current,
        )
        if not opened:
            row = app.store.alert_row(condition)
            last = (row["notified_at"] or row["opened_at"]) if row else None
            if isinstance(last, int) and current - last < UNIT_ALERT_REPEAT_SECONDS:
                print(f"{unit} already reported {describe_age(current - last)} ago")
                return 0
            app.store.reopen_alert(condition, detail or f"{unit} is still failing.", now=current)
        subject = condition
        body = detail or (
            f"systemd reports {unit} failed. Ground truth for this vendor stops "
            "here until it is running again."
        )

    owner = app.config.owner_email
    if not owner:
        print(f"ALERT (no OWNER_EMAIL set, not mailed): {subject} — {body}")
        return 0
    full_subject, html_body, text_body = app.owner_message(subject, body)
    try:
        app.mailer.send(
            to=owner,
            subject=full_subject,
            html_body=html_body,
            text_body=text_body,
            idempotency_key=app.opaque_bucket("alert", f"{condition}:{current // 3600}"),
            headers={},
        )
    except RuntimeError as exc:
        print(f"ALERT UNDELIVERED {subject}: {exc}", file=sys.stderr)
        return 0
    if not clear:
        app.store.mark_alert_notified(condition, now=current)
    print(f"alerted {owner}: {full_subject}")
    return 0


def make_handler(app: SubscriptionApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AIResetSubscriptions/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            route = urllib.parse.urlsplit(self.path).path
            sys.stderr.write(f"{self.command} {route} from={self.client_address[0]}\n")

        def security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def json_response(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.security_headers()
            self.end_headers()
            self.wfile.write(body)

        def empty_response(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()

        def html_response(self, status: int, body: str) -> None:
            encoded = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'",
            )
            self.security_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def redirect(self, state: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header(
                "Location",
                f"{app.config.public_url}/?{urllib.parse.urlencode({'subscription': state})}",
            )
            self.security_headers()
            self.end_headers()

        def token_from_query(self) -> str:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return query.get("token", [""])[0]

        def read_body(self, max_bytes: int = 4096) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > max_bytes:
                raise ValueError("Invalid request.")
            return self.rfile.read(length)

        def valid_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            expected = urllib.parse.urlsplit(app.config.public_url)
            supplied = urllib.parse.urlsplit(origin)
            return supplied.scheme == expected.scheme and supplied.netloc == expected.netloc

        def do_GET(self) -> None:
            route = urllib.parse.urlsplit(self.path).path
            if route == "/api/healthz":
                self.json_response(HTTPStatus.OK, {"ok": True})
                return
            if route == "/api/confirm":
                token = self.token_from_query()
                try:
                    app.validate_confirmation(token)
                except ValueError:
                    self.redirect("invalid")
                else:
                    safe_action = html.escape(
                        "/api/confirm?"
                        + urllib.parse.urlencode(
                            {"token": token, "redirect": "1"}
                        ),
                        quote=True,
                    )
                    self.html_response(
                        HTTPStatus.OK,
                        action_shell(
                            "Confirm subscription",
                            (
                                "<h1>Confirm your alerts</h1>"
                                "<p>This final step confirms that you control this "
                                "email address. Your selected provider alerts begin "
                                "only after you press the button.</p>"
                                f'<form action="{safe_action}" method="post">'
                                '<button type="submit">Confirm subscription</button>'
                                "</form>"
                            ),
                        ),
                    )
                return
            if route == "/api/preferences":
                token = self.token_from_query()
                try:
                    _, selected, stages = app.preferences(token)
                except ValueError:
                    self.redirect("invalid")
                else:
                    safe_action = html.escape(
                        "/api/preferences?"
                        + urllib.parse.urlencode(
                            {"token": token, "redirect": "1"}
                        ),
                        quote=True,
                    )
                    choices = "".join(
                        (
                            '<label><input type="checkbox" name="topics" '
                            f'value="{topic}"'
                            f'{" checked" if topic in selected else ""}>'
                            f"{html.escape(VENDOR_LABELS[topic])}</label>"
                        )
                        for topic in VALID_TOPICS
                    )
                    follow_ups = "".join(
                        (
                            '<label><input type="checkbox" name="stages" '
                            f'value="{stage}"'
                            f'{" checked" if stage in stages else ""}>'
                            f"{html.escape(STAGE_LABELS[stage])}</label>"
                        )
                        for stage in FOLLOW_UP_STAGES
                    )
                    self.html_response(
                        HTTPStatus.OK,
                        action_shell(
                            "Manage alerts",
                            (
                                "<h1>Choose your signals</h1>"
                                "<p>Only new announcements from checked providers "
                                "will reach your inbox.</p>"
                                f'<form action="{safe_action}" method="post">'
                                f"<fieldset><legend>Alert providers</legend>{choices}</fieldset>"
                                "<fieldset><legend>Follow-ups</legend>"
                                "<p>At most one of each, per reset, and only when "
                                "our own account changes the answer we already "
                                f"gave you.</p>{follow_ups}</fieldset>"
                                '<button type="submit">Save preferences</button></form>'
                            ),
                        ),
                    )
                return
            if route == "/api/unsubscribe":
                token = self.token_from_query()
                try:
                    app.parse_token(token, "unsubscribe")
                except ValueError:
                    self.redirect("invalid")
                else:
                    safe_action = html.escape(
                        "/api/unsubscribe?"
                        + urllib.parse.urlencode(
                            {"token": token, "redirect": "1"}
                        ),
                        quote=True,
                    )
                    self.html_response(
                        HTTPStatus.OK,
                        action_shell(
                            "Unsubscribe",
                            (
                                "<h1>Stop all alerts?</h1>"
                                "<p>You won’t receive future AI Reset Watch email "
                                "notifications. You can subscribe again later.</p>"
                                f'<form action="{safe_action}" method="post">'
                                '<button class="danger" type="submit">Unsubscribe</button>'
                                "</form>"
                            ),
                        ),
                    )
                return
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Not found."})

        def do_POST(self) -> None:
            route = urllib.parse.urlsplit(self.path).path
            if route == "/api/webhooks/resend":
                if not app.config.webhook_secret:
                    self.json_response(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "Webhook is not configured."},
                    )
                    return
                try:
                    raw = self.read_body(max_bytes=262144)
                    app.handle_webhook(self.headers, raw)
                except ValueError:
                    self.json_response(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "Invalid webhook."},
                    )
                else:
                    self.empty_response(HTTPStatus.OK)
                return
            if route == "/api/confirm":
                try:
                    app.confirm(self.token_from_query())
                except ValueError:
                    self.redirect("invalid")
                else:
                    self.redirect("confirmed")
                return
            if route == "/api/preferences":
                try:
                    raw = self.read_body()
                    form = urllib.parse.parse_qs(raw.decode())
                    app.update_preferences(
                        self.token_from_query(),
                        form.get("topics", []),
                        form.get("stages", []),
                    )
                except (ValueError, UnicodeDecodeError):
                    self.redirect("invalid")
                else:
                    self.redirect("preferences")
                return
            if route == "/api/unsubscribe":
                try:
                    app.unsubscribe(self.token_from_query())
                except ValueError:
                    self.json_response(HTTPStatus.BAD_REQUEST, {"error": "Invalid link."})
                else:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    if query.get("redirect") == ["1"]:
                        self.redirect("unsubscribed")
                    else:
                        self.empty_response(HTTPStatus.OK)
                return
            if route != "/api/subscribe":
                self.json_response(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            if not self.valid_origin():
                self.json_response(HTTPStatus.FORBIDDEN, {"error": "Invalid origin."})
                return
            try:
                raw = self.read_body()
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    payload = json.loads(raw)
                else:
                    payload = {
                        key: values[0]
                        for key, values in urllib.parse.parse_qs(raw.decode()).items()
                    }
                if payload.get("website"):
                    self.json_response(
                        HTTPStatus.ACCEPTED,
                        {"message": "Check your inbox to confirm your subscription."},
                    )
                    return
                remote_ip = self.headers.get("X-Real-IP", self.client_address[0])
                app.subscribe(
                    str(payload.get("email", "")),
                    remote_ip,
                    payload.get("topics", VALID_TOPICS),
                )
            except ValueError as exc:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": "Invalid request."})
            except RuntimeError as exc:
                message = str(exc)
                if message.startswith("Too many requests"):
                    self.json_response(HTTPStatus.TOO_MANY_REQUESTS, {"error": message})
                else:
                    sys.stderr.write(f"subscription email failed: {message}\n")
                    self.json_response(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "Email delivery is temporarily unavailable. Please try again."},
                    )
            else:
                self.json_response(
                    HTTPStatus.ACCEPTED,
                    {"message": "Check your inbox to confirm your subscription."},
                )

    return Handler


def build_app(config: Config) -> SubscriptionApp:
    store = Store(config.db_path)
    store.init()
    return SubscriptionApp(
        config,
        store,
        ResendClient(config.api_key, config.from_email),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "notify", "init-db", "alert"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="notify: print what would be delivered and send nothing",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="notify: move deliveries held by the flood cap into the send queue",
    )
    parser.add_argument(
        "--unit",
        default="",
        help="alert: the systemd unit that failed (systemd's %%i in the template)",
    )
    parser.add_argument(
        "--detail",
        default="",
        help="alert: one line of context for the owner",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="alert: the unit recovered; close the condition and say so once",
    )
    args = parser.parse_args()
    # A dry run neither sends nor signs anything, so it must not require the
    # encrypted credentials that only the systemd unit can read.
    # `alert` and `notify --dry-run` neither send nor sign anything they
    # cannot recover from, and both are documented as exiting 0 without a
    # credential — the alert unit says so in its own header. Requiring the
    # encrypted secrets made both of them die with a traceback instead.
    relaxed = args.command == "alert" or (args.command == "notify" and args.dry_run)
    # `init-db` creates a table and exits. It has no From address to get wrong,
    # so demanding one would make setting up a new host require a mail identity
    # before the database it stores subscribers in can even exist.
    config = Config.from_env(
        require_secrets=not relaxed,
        require_sender=not relaxed and args.command != "init-db",
    )
    app = build_app(config)
    if args.command == "init-db":
        print(f"initialized {config.db_path}")
        return 0
    if args.command == "alert":
        if not args.unit:
            parser.error("alert requires --unit")
        return run_unit_alert(
            app, args.unit, detail=args.detail, clear=args.clear
        )
    if args.command == "notify":
        return run_notifier(config, app, dry_run=args.dry_run, release=args.release)

    server = ThreadingHTTPServer((config.host, config.port), make_handler(app))
    print(f"subscription API listening on {config.host}:{config.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
