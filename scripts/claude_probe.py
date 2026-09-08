#!/usr/bin/env python3
"""Ground-truth reset detector for Claude Code: watch our own weekly windows.

The same question `scripts/quota_probe.py` answers for Codex — did this window
expire on its own schedule, or did something force it — asked of Anthropic.
Two Claude resets (2026-09-01 and 2026-09-04) were missed entirely because
`data/anthropic.json` is a hand-seeded file with no discovery job and no probe
behind it; seven of eight Anthropic subscribers have never received a mail.

The STATE MACHINE is not reimplemented here. `quota_probe.Detector` is imported
and fed rows in its own shape, because its `is_revert()` already handles both
window contracts through the `reanchored` flag recorded at detection time, and
a second copy of that logic would drift from the one that has live evidence
behind it.

What differs from Codex, all measured on this host on 2026-09-05/06:

1. TRANSPORT. `GET https://api.anthropic.com/api/oauth/usage` with
   `Authorization: Bearer <claudeAiOauth.accessToken>` and
   `anthropic-beta: oauth-2025-04-20`. The credential file is read on EVERY
   poll and the token is never cached, never logged, and never refreshed by
   this process: refresh tokens rotate, so a competing refresh would log the
   owner out of their own editor. When `expiresAt` has passed we skip the
   request outright rather than spend a request on a certain 401.

2. RATE LIMIT. The endpoint throttles PER TOKEN and the 429 persists (see
   MIN_POLL_SECONDS for the measurement and the harm). The poll interval has a
   hard floor no environment variable can lower; a 429 backs off x2 from 10
   minutes to a 60-minute cap, honours Retry-After, and the backoff SURVIVES A
   RESTART, resumed from the health file, so systemd cannot walk a process
   through the throttle by restarting it.

3. WINDOWS ARE FIXED-ANCHOR, not rolling. The 5-hour window re-anchors at
   first use on a 10-minute grid (recorded: 04:40:00 exactly) and the weekly
   window sits on a per-account 7-day lattice (recorded: 10:00:00 exactly),
   with about a second of jitter on `resets_at` — which is why anchors are
   compared through `quota_probe.same_anchor` and never with `==`. There is no
   banked-credit bank, so classification cannot use credits at all: it rests
   entirely on `early_by` against the anchor that was in force before the
   clear. A vendor reset most likely zeroes the counter and KEEPS the anchor
   (n=1, inferred from the Sep 7 anchor surviving both missed resets), which
   is exactly the contract `is_revert()` handles with `reanchored=False`: the
   anchor says nothing, and only a snap-back to the pre-clear level retracts.

4. THE AUTHORITY IS `limits[]`, not the legacy top-level `five_hour` /
   `seven_day` keys. Its entries carry `kind` in {session, weekly_all,
   weekly_scoped}, a `group` of session|weekly, an integer `percent`, an ISO-8601
   `resets_at`, and for `weekly_scoped` a per-model `scope`. A payload with no
   `limits[]` is a failed poll, not an empty one — falling back to the legacy
   keys would let the contract drift silently under us.

5. ONLY A WEEKLY CLEAR MAY BE VENDOR EVIDENCE. A 5-hour clear is produced both
   by ordinary expiry and by the feature-flagged `/limit-reset` command, so it
   is recorded with the classification `session_only` and can never be read as
   anything else. That string is deliberately absent from every verdict map in
   this repository (`groundtruth._INTERNAL_TO_VERDICT`, `build.PUBLIC_VERDICTS`,
   the export below), so a session clear cannot become public by being
   forgotten about in one of them.

6. TWO ACCOUNTS, labelled `a` and `b` and described only as two Max 20x
   accounts on one operator machine. Never an organisation name, never an
   email, in any file this writes. Both clearing within two polls is recorded
   as `concordance: agreeing`; one clearing while the other's weekly window is
   readable and does NOT clear is `disagree`, which is an owner alert and is
   withheld from the public export rather than published as a vendor reset.

The strongest claim this file will ever make is "our weekly window cleared
early and nothing this account did explains it". Never "the vendor reset
everyone"; never "no reset happened".
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # Run by systemd as an absolute path, which puts scripts/ on sys.path
    # rather than the repository root.
    sys.path.insert(0, str(ROOT))

from scripts import quota_probe as qp  # noqa: E402
from scripts.timefmt import format_epoch_pacific  # noqa: E402

# ─── Configuration ───────────────────────────────────────────────────────────

STATE_DIR = Path(os.environ.get("AI_RESETS_STATE", "/var/lib/ai-resets"))
SAMPLES_FILE = STATE_DIR / "claude_samples.jsonl"
EVENTS_FILE = STATE_DIR / "claude_events.jsonl"
CURSOR_FILE = STATE_DIR / "claude_cursor.json"
# The SHARED heartbeat file, one top-level block per label. Bound to
# quota_probe's own path object rather than recomputed, so there is exactly one
# definition of where it lives and one implementation of the read-modify-write
# that keeps the codex block intact. Clobbering that block would blind the
# Codex alerting, which is the failure this file must not cause.
HEALTH_FILE = qp.HEALTH_FILE

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
USER_AGENT = "Mozilla/5.0 (compatible; ai-resets/1.0)"
HTTP_TIMEOUT = float(os.environ.get("AI_RESETS_CLAUDE_TIMEOUT", "20"))

# The hard floor, in seconds, between two requests on ONE token. Measured:
# /api/oauth/usage throttles per token at roughly 25-30 requests per 15
# minutes and the 429 persists; a probe polling every 20s locked both of the
# owner's accounts out of their own /usage page for the rest of the window.
# The environment variable can only make the interval LONGER — a config
# mistake must not be able to reproduce that outage.
MIN_POLL_SECONDS = 300


def poll_interval(raw: str | None) -> int:
    """The configured interval, floored — and never a crash on a typo.

    An unparseable value falls back to the floor rather than raising out of
    module import, which under systemd is a restart loop that never polls at
    all and never writes the health file that says so.
    """
    try:
        requested = int(str(raw).strip())
    except (TypeError, ValueError):
        return MIN_POLL_SECONDS
    return max(MIN_POLL_SECONDS, requested)


POLL_SECONDS = poll_interval(os.environ.get("AI_RESETS_CLAUDE_POLL_SECONDS"))

# A clear landing within this margin after the prior anchor is a natural
# expiry: two poll intervals plus a minute of slack, the same rule the Codex
# probe uses at its own cadence. At 300s that is 660s. The Codex module's own
# constant is 180s, and applying THAT to a 300s cadence would call a weekly
# expiry seen one poll late a vendor reset.
NATURAL_GRACE_SECONDS = 2 * POLL_SECONDS + 60

# Anthropic 429 backoff: x2 from ten minutes, capped at an hour.
THROTTLE_BASE_SECONDS = 600
THROTTLE_MAX_SECONDS = 3600

# Two accounts clearing within this of each other are one vendor action.
CONCORDANCE_WINDOW_SECONDS = 2 * POLL_SECONDS

SESSION_MINUTES = 300
WEEKLY_MINUTES = 10080
WINDOW_MINUTES_BY_GROUP = {"session": SESSION_MINUTES, "weekly": WEEKLY_MINUTES}

# The two Max 20x accounts, in the only names that are ever written down.
LABELS = ("a", "b")
# Where the editor keeps each profile's OAuth token, relative to the home
# directory of whoever the probe runs as. Derived rather than written out so
# the source carries no particular machine's paths; the systemd unit sets both
# explicitly anyway, and these are what `--show` reports on a laptop.
DEFAULT_CREDENTIALS = {
    "a": str(Path.home() / ".claude" / ".credentials.json"),
    "b": str(Path.home() / ".claude-profiles" / "main1" / ".credentials.json"),
}
HEALTH_LABELS = {label: f"claude:{label}" for label in LABELS}

# Ten consecutive polls where every account was ATTEMPTED and every attempt
# failed. At 300s that is 50 minutes of blindness before systemd is handed the
# problem. Throttled and stale-token skips are excluded on purpose: neither is
# fixed by a restart, and exiting on them would only add process churn to an
# outage a human has to clear.
BLIND_EXIT_FAILURES = 10
BLIND_LOG_EVERY = 5

# Internal classification for a clear on the 5-hour window. Never a verdict,
# never published, and absent from every verdict map in the repository so that
# forgetting it in one place cannot make it public.
CLASS_SESSION_ONLY = "session_only"

CONCORDANCE_AGREEING = "accounts_agreeing"
CONCORDANCE_DISAGREE = "accounts_disagree"
CONCORDANCE_UNKNOWN = "unknown"

EXPORT_VENDOR = "anthropic"
PRODUCT = "Claude Code"
# The only description of the population that may be published.
ACCOUNTS_PHRASE = "Two Max 20x accounts on one operator machine"

# What may be shown to anyone but the owner. An allowlist: a classification
# this build does not recognise must not become publishable by default.
PUBLIC_CLASSIFICATIONS = (qp.CLASS_NATURAL, qp.CLASS_GLOBAL, qp.CLASS_UNRESOLVED)


class ThrottledError(qp.ProbeError):
    """HTTP 429. Carries the server's Retry-After when it sent one."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(qp.ProbeError):
    """HTTP 401/403. A human must re-login; this process never refreshes."""


class CredentialError(qp.ProbeError):
    """The credential file is missing, unreadable, or not in the known shape."""


# ─── Secrets hygiene ─────────────────────────────────────────────────────────

# An access token is 108 unbroken URL-safe characters. Anything that long in a
# message we are about to LOG or STORE is treated as a secret, whatever it
# actually is: the health file's `last_error` is copied into owner alerts, and
# `grep -c accessToken` over the state directory has to stay 0.
_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{40,}")


def redact(text: str) -> str:
    return _SECRETISH.sub("<redacted>", text)


def log(message: str) -> None:
    qp.log(redact(message))


# ─── Credentials ─────────────────────────────────────────────────────────────


def credential_paths() -> dict[str, Path]:
    """Where each account's OAuth credential file lives.

    Read from the environment at call time rather than at import so the systemd
    unit, a test, and `--show` on a second machine all say it in one place.
    """
    return {
        label: Path(
            os.environ.get(f"AI_RESETS_CLAUDE_CREDS_{label.upper()}", default)
        )
        for label, default in DEFAULT_CREDENTIALS.items()
    }


def _expiry_epoch(value: Any) -> int | None:
    """`expiresAt` as epoch SECONDS.

    Measured: the file carries milliseconds (1788766910046). Reading that as
    seconds would place the expiry in the year 58000 and the "is the token
    stale" check would never fire, so the unit is detected from the magnitude
    rather than assumed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    moment = int(value)
    if moment > 10_000_000_000:  # past 2286 in seconds: it is milliseconds
        moment //= 1000
    return moment if 0 < moment <= 4102444800 else None


def read_credentials(path: Path) -> tuple[str, int | None, str | None]:
    """(access token, expiry epoch, subscription type) read fresh from disk.

    Called on EVERY poll and the result is never stored on an object, never
    logged, and never written to any file: the token exists only as a local in
    the caller's frame for the length of one request. The refresh token in the
    same file is never touched — refresh tokens rotate, and a refresh from here
    would invalidate the one Claude Code itself is holding.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CredentialError(f"no credential file at {path}") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CredentialError(f"cannot read the credential file at {path}") from exc
    block = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise CredentialError(f"credential file at {path} has no claudeAiOauth block")
    token = block.get("accessToken")
    if not isinstance(token, str) or not token:
        raise CredentialError(f"credential file at {path} carries no access token")
    subscription = block.get("subscriptionType")
    return (
        token,
        _expiry_epoch(block.get("expiresAt")),
        subscription if isinstance(subscription, str) else None,
    )


# ─── Transport ───────────────────────────────────────────────────────────────


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The server's own words for a failure, bounded and never the whole body."""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a body we cannot read is not the story
        return exc.reason if isinstance(exc.reason, str) else ""
    try:
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            body = f"{error.get('type')}: {error.get('message')}"
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    return body[:160]


def retry_after_seconds(headers: Any, now: int) -> int | None:
    """Retry-After as whole seconds, from either of the two legal spellings.

    Clamped to the backoff cap: a server telling us to wait a day is answered
    with an hour and a log line, not with a probe that silently stops for a day.
    """
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    seconds: int | None = None
    if text.isdigit():
        seconds = int(text)
    else:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = int(moment.timestamp()) - now
    if seconds is None:
        return None
    return max(1, min(seconds, THROTTLE_MAX_SECONDS))


def fetch_usage(token: str, *, timeout: float = HTTP_TIMEOUT, now: int | None = None) -> dict[str, Any]:
    """One authenticated read of the usage endpoint.

    Raises ThrottledError on 429, AuthError on 401/403, ProbeError on anything
    else. Nothing raised from here carries the token: every message goes
    through redact() before it can reach a log line or the health file.
    """
    now = int(time.time()) if now is None else now
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "accept": "application/json",
            "user-agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = redact(_error_detail(exc))
        if exc.code == 429:
            raise ThrottledError(
                f"usage endpoint throttled this token: {detail}",
                retry_after_seconds(getattr(exc, "headers", None), now),
            ) from exc
        if exc.code in (401, 403):
            raise AuthError(
                f"usage endpoint rejected the credential (HTTP {exc.code}): {detail}"
            ) from exc
        raise qp.ProbeError(f"usage endpoint returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise qp.ProbeError(f"usage endpoint unreachable: {redact(str(exc))}") from exc

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise qp.ProbeError("usage endpoint returned a body that is not JSON") from exc
    if not isinstance(payload, dict):
        raise qp.ProbeError("usage endpoint returned a payload that is not an object")
    return payload


# ─── Snapshot normalisation ──────────────────────────────────────────────────


def parse_moment(value: Any) -> int | None:
    """`resets_at` as epoch seconds, from an ISO-8601 string or a number.

    Recorded shape: "2026-09-07T10:00:00.179608+00:00". The bound on plausible
    epochs is quota_probe's, so one implausible timestamp costs a clause rather
    than the export.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return qp._epoch(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return qp._epoch(moment.timestamp())
    return None


def window_minutes_of_entry(entry: dict[str, Any]) -> int | None:
    """300 or 10080 for one `limits[]` entry, or None if it is neither.

    `group` is read first and `kind` only as a fallback: a kind this build has
    never seen (`weekly_something`) still maps correctly as long as the vendor
    keeps grouping it, and a shape that maps to neither is dropped rather than
    guessed into a window it might not be.
    """
    group = entry.get("group")
    if isinstance(group, str) and group in WINDOW_MINUTES_BY_GROUP:
        return WINDOW_MINUTES_BY_GROUP[group]
    kind = entry.get("kind")
    if isinstance(kind, str):
        if kind == "session":
            return SESSION_MINUTES
        if kind.startswith("weekly"):
            return WEEKLY_MINUTES
    return None


def model_of_scope(scope: Any) -> tuple[str | None, str | None]:
    """(slug, display name) for a `weekly_scoped` entry's model.

    The slug is part of the slot id, so two model-scoped windows stay separate
    state machines. The display name is kept for the OWNER's records only: the
    recorded payload carried an unreleased model codename, and the public site
    has no reason to be the place that publishes one.
    """
    if not isinstance(scope, dict):
        return None, None
    model = scope.get("model")
    if not isinstance(model, dict):
        return None, None
    name = model.get("display_name") or model.get("id")
    if not isinstance(name, str) or not name.strip():
        return None, None
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40]
    return (slug or None), name.strip()


def slot_id(label: str, kind: str, model: str | None) -> str:
    """`claude:<label>:<kind>[:<model>]` — the shared vocabulary's slot id."""
    return f"claude:{label}:{kind}" + (f":{model}" if model else "")


def account_of_slot(slot: Any) -> str | None:
    """The account label carried inside a slot key, or None.

    Slot keys are `quota_probe.slot_key()` output: `claude:a:weekly_all/10080`.
    """
    if not isinstance(slot, str) or not slot.startswith("claude:"):
        return None
    parts = slot.split("/", 1)[0].split(":")
    return parts[1] if len(parts) >= 3 and parts[1] else None


def normalise_usage(
    payload: dict[str, Any],
    label: str,
    now: int,
    plan_type: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten one usage payload into quota_probe's row shape, one row per slot.

    `limits[]` is the authority. The legacy top-level `five_hour`/`seven_day`
    keys are deliberately NOT read as a fallback: they carry no per-model
    window, and silently falling back to them would hide the day the contract
    changes underneath us behind rows that look fine.
    """
    limits = payload.get("limits")
    if not isinstance(limits, list) or not limits:
        raise qp.ProbeError("usage payload carried no limits[] list")

    rows: list[dict[str, Any]] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        minutes = window_minutes_of_entry(entry)
        if minutes is None:
            log(f"UNKNOWN WINDOW {HEALTH_LABELS.get(label, label)} kind={kind!r} — skipped")
            continue
        percent = entry.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        model, model_name = model_of_scope(entry.get("scope"))
        rows.append(
            {
                "t": now,
                "limit_id": slot_id(label, kind, model),
                "slot": kind,
                "window_minutes": minutes,
                "used_percent": float(percent),
                "resets_at": parse_moment(entry.get("resets_at")),
                "plan_type": plan_type,
                # Anthropic has no banked reset credit to spend, so this field
                # is structurally None and classify() can never reach its
                # self_applied branch. The column stays so the row is exactly
                # the shape quota_probe's helpers already read.
                "credits_available": None,
                "account": label,
                "limit_kind": kind,
                "model": model,
                "model_name": model_name,
            }
        )
    if not rows:
        raise qp.ProbeError("usage payload carried no readable limit windows")
    return rows


# ─── Detection ───────────────────────────────────────────────────────────────


@contextlib.contextmanager
def anthropic_contract() -> Iterator[None]:
    """Run quota_probe's detector at Anthropic's cadence, then put it back.

    Two of the detector's thresholds are module constants derived from the
    CODEX poll interval: NATURAL_GRACE_SECONDS (2*60+60 = 180s) decides
    natural expiry inside classify(), and POLL_SECONDS decides what counts as
    a blind gap inside is_anchor_jump_clear(). At a 300s cadence a weekly
    expiry seen one poll late is 300s "early" by that 180s rule — a natural
    expiry published as a vendor reset, the exact over-claim this project
    exists to remove.

    Swapping them for the duration of the call, rather than at import, is what
    keeps the Codex probe's own tests and any same-process reader of that
    module seeing its own contract. The restore is in a finally: an exception
    out of observe() must not leave the Codex constants rewritten.
    """
    saved = (qp.POLL_SECONDS, qp.NATURAL_GRACE_SECONDS)
    qp.POLL_SECONDS, qp.NATURAL_GRACE_SECONDS = POLL_SECONDS, NATURAL_GRACE_SECONDS
    try:
        yield
    finally:
        qp.POLL_SECONDS, qp.NATURAL_GRACE_SECONDS = saved


def window_key(row: dict[str, Any]) -> str:
    """The slot id with the account stripped off: `weekly_all`, `weekly_scoped:opus`.

    Concordance compares like with like. Account a's `weekly_all` clearing
    while account b's per-MODEL window sits at 55% is not a disagreement about
    anything — they are different windows — so the comparison is keyed on the
    window, not merely on the account.
    """
    kind = str(row.get("limit_kind") or "")
    model = row.get("model")
    return f"{kind}:{model}" if model else kind


def weekly_detectable(detector: qp.Detector, label: str, key: str) -> bool:
    """Would a clear on the same window, on that account, have been visible?

    A window sitting under the 10-point drop a clear needs cannot produce one,
    so "the other account did not clear" is only evidence when the other
    account's matching window was actually readable. Without this test every
    clear on a busy account would be reported as a disagreement with an idle
    one.
    """
    state = detector.slots.get(f"claude:{label}:{key}/{WEEKLY_MINUTES}")
    last = state.get("last") if isinstance(state, dict) else None
    used = last.get("used_percent") if isinstance(last, dict) else None
    return isinstance(used, (int, float)) and float(used) >= qp.CLEAR_DROP_MIN


def record_concordance(
    claude_state: dict[str, Any],
    detector: qp.Detector,
    label: str,
    key: str,
    moment: int,
) -> str:
    """Did the other account clear its weekly window too?

    `accounts_agreeing` is corroboration: one vendor action seen twice. A
    `accounts_disagree` is the opposite — one account's weekly window cleared
    while another readable one did not — and is an owner alert rather than
    evidence, so the export withholds it instead of calling it a vendor reset.
    `unknown` is the honest third answer: the other account was idle, blind, or
    has not been sampled yet, and silence there proves nothing.

    Only the NEW event carries the answer. The log is append-only, so the
    earlier of a pair cannot be edited to say `agreeing`; the pair is joined by
    their timestamps, which is what a reader would check anyway.
    """
    by_window = claude_state.setdefault("weekly_clears", {})
    clears = by_window.setdefault(key, {})
    others = [other for other in LABELS if other != label]
    verdict = CONCORDANCE_UNKNOWN
    for other in others:
        last = clears.get(other)
        if isinstance(last, int) and abs(moment - last) <= CONCORDANCE_WINDOW_SECONDS:
            verdict = CONCORDANCE_AGREEING
            break
    else:
        if any(weekly_detectable(detector, other, key) for other in others):
            verdict = CONCORDANCE_DISAGREE
    clears[label] = moment
    return verdict


def annotate(
    event: dict[str, Any],
    row: dict[str, Any],
    claude_state: dict[str, Any],
    detector: qp.Detector,
) -> dict[str, Any]:
    """Add the Anthropic-specific fields to one detector event.

    Two things happen here that the shared detector cannot do:

    1. A clear on the 5-hour window is re-classified `session_only`. Natural
       expiry and the feature-flagged `/limit-reset` command produce exactly
       the same shape, so a 5-hour clear is never evidence about the vendor.
       The detector's own verdict is preserved under `gated_classification`
       for the audit trail, but the field every reader in this repository maps
       to a public verdict now holds a string that appears in none of those
       maps.
    2. A weekly clear records what the OTHER account's weekly window was doing.
    """
    annotated = dict(event)
    annotated["vendor"] = EXPORT_VENDOR
    annotated["account"] = row.get("account")
    annotated["limit_kind"] = row.get("limit_kind")
    annotated["model"] = row.get("model")
    annotated["model_name"] = row.get("model_name")

    minutes = row.get("window_minutes")
    if minutes == SESSION_MINUTES and annotated.get("classification"):
        annotated["gated_classification"] = annotated["classification"]
        annotated["classification"] = CLASS_SESSION_ONLY
        annotated["reason"] = (
            "5-hour window: ordinary expiry and the /limit-reset command produce "
            "the same clear, so this is never evidence about the vendor "
            f"(the detector alone read it as {event.get('classification')}: "
            f"{event.get('reason')})"
        )
    elif minutes == WEEKLY_MINUTES:
        moment = annotated.get("clear_bracket_hi") or annotated.get("detected_at")
        if (
            annotated.get("kind") == qp.EVENT_CLEAR
            and isinstance(moment, int)
            and annotated.get("classification") == qp.CLASS_GLOBAL
        ):
            # Only an EARLY clear nothing the account did explains can disagree
            # about anything. The two accounts sit on different per-account
            # 7-day lattices, so one's ordinary weekly expiry always lands
            # while the other's window is busy — the steady state. Recording
            # that as `accounts_disagree` would page the owner roughly weekly
            # for a probe working exactly as designed.
            annotated["concordance"] = record_concordance(
                claude_state, detector, str(row.get("account")), window_key(row), moment
            )
        if annotated.get("classification") == qp.CLASS_GLOBAL:
            # The shared classifier explains an early clear as "the credit bank
            # unchanged", which is the Codex evidence and is meaningless here:
            # Anthropic has no bank to read. Restating it in the terms that
            # actually apply keeps the owner's own event log from carrying a
            # sentence nothing measured.
            annotated["detector_reason"] = annotated.get("reason")
            annotated["reason"] = (
                f"cleared {annotated.get('early_by_seconds')}s before its scheduled "
                "expiry; there is no banked reset credit on this vendor to spend and "
                "/limit-reset clears only the 5-hour window, so nothing this account "
                "did explains it"
            )
    return annotated


def observe_rows(
    detector: qp.Detector,
    rows: list[dict[str, Any]],
    claude_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Feed one poll's rows through the shared state machine."""
    events: list[dict[str, Any]] = []
    with anthropic_contract():
        for row in rows:
            for event in detector.observe(row):
                events.append(annotate(event, row, claude_state, detector))
    return events


def log_events(events: list[dict[str, Any]]) -> None:
    """One journal line per event, in the vocabulary the health file uses."""
    for event in events:
        label = HEALTH_LABELS.get(str(event.get("account")), event.get("account"))
        if event.get("kind") == qp.EVENT_RETRACTION:
            log(
                f"RETRACTED {label} {event.get('event_id')} "
                f"{event.get('used_before')}%->{event.get('used_after')}% "
                f"usage back at {event.get('used_at_revert')}% "
                f"at {event.get('reverted_at')} — not a reset"
            )
            continue
        line = (
            f"DETECTED [{event.get('classification')}] {label} "
            f"{event.get('slot')} "
            f"{event.get('used_before')}%->{event.get('used_after')}% "
            f"bracket [{event.get('clear_bracket_lo')},{event.get('clear_bracket_hi')}] "
            f"— {event.get('reason')}"
        )
        log(line)
        if event.get("concordance") == CONCORDANCE_DISAGREE:
            # An owner alert, not evidence: subscriptions.py reads the
            # `concordance` field off this same record. The journal line is
            # what a human tailing the unit sees in the meantime.
            log(
                f"ACCOUNTS DISAGREE {label} cleared its weekly window while another "
                "account's weekly window was readable and did not — withheld from "
                "the public export"
            )


# ─── Persistence ─────────────────────────────────────────────────────────────


def load_cursor() -> dict[str, Any]:
    try:
        loaded = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_cursor(cursor: dict[str, Any]) -> None:
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CURSOR_FILE)


# ─── Probe health ────────────────────────────────────────────────────────────


def health_block(
    detector: qp.Detector,
    label: str,
    now: int,
    *,
    last_ok_at: int | None,
    consecutive_failures: int,
    blind_since: int | None,
    throttled_until: int | None,
    token_stale: bool,
    last_error: str | None,
) -> dict[str, Any]:
    """One account's health, in the exact keys every reader expects.

    Per ACCOUNT, not per probe: with two accounts a blind half must not be
    reportable as a clean look, and the only way a reader can apply that rule
    is to see both blocks.
    """
    slots = {
        key: value
        for key, value in detector.detectable_now().items()
        if account_of_slot(key) == label
    }
    times = [
        state["last"]["t"]
        for key, state in detector.slots.items()
        if account_of_slot(key) == label
        and isinstance(state.get("last"), dict)
        and isinstance(state["last"].get("t"), int)
    ]
    return {
        "label": HEALTH_LABELS.get(label, label),
        "updated_at": now,
        "last_ok_at": last_ok_at,
        "last_sample_t": max(times) if times else None,
        "consecutive_failures": consecutive_failures,
        "blind_since": blind_since,
        "throttled_until": throttled_until,
        "token_stale": token_stale,
        "detectable_now": slots,
        "last_error": None if last_error is None else redact(last_error)[:200],
    }


def resume_health(label: str, now: int) -> dict[str, Any]:
    """What a restarted probe inherits from its own last health block.

    `throttled_until` is the one that matters. systemd restarts this unit on
    any non-zero exit, and a backoff that lived only in memory would mean a
    crash-loop walks straight through a 429 the endpoint is still enforcing —
    which is how both of the owner's accounts were locked out of their own
    /usage page once already. A future timestamp is honoured up to the backoff
    cap; anything beyond that is a clock problem, not a throttle.
    """
    entry = qp.load_health().get(HEALTH_LABELS.get(label, label))
    if not isinstance(entry, dict):
        return {}
    last_ok_at = entry.get("last_ok_at")
    updated_at = entry.get("updated_at")
    blind_since = entry.get("blind_since")
    throttled_until = entry.get("throttled_until")
    fresh = isinstance(updated_at, int) and now - updated_at <= qp.HEALTH_CARRY_SECONDS
    return {
        "last_ok_at": last_ok_at if isinstance(last_ok_at, int) else None,
        "blind_since": blind_since if (fresh and isinstance(blind_since, int)) else None,
        "throttled_until": (
            throttled_until
            if isinstance(throttled_until, int)
            and now < throttled_until <= now + THROTTLE_MAX_SECONDS
            else None
        ),
    }


# ─── Publishable words ───────────────────────────────────────────────────────


def verdict_of(event: dict[str, Any]) -> str | None:
    """The published verdict for one record, or None when there is none.

    `session_only` deliberately maps to nothing: quota_probe.VERDICT_OF_CLASS
    has no entry for it, so a 5-hour clear cannot acquire a verdict by
    accident. A retraction gets no verdict either — a withdrawn clear must not
    carry a publishable string at all.
    """
    if event.get("kind") == qp.EVENT_RETRACTION:
        return None
    classification = event.get("classification")
    if not isinstance(classification, str):
        return None
    return qp.VERDICT_OF_CLASS.get(classification)


def window_reference(event: dict[str, Any]) -> str:
    """`our Claude Code weekly window on account a`, mid-sentence.

    A `weekly_scoped` window is named by its SHAPE, never by the model: the
    payload recorded on 2026-09-06 carried an unreleased model codename in
    `scope.model.display_name`, and the site has no reason to be the place
    that publishes one. The name stays in the owner's own event log.
    """
    minutes = qp.window_minutes_of(event)
    if event.get("limit_kind") == "weekly_scoped":
        window = "weekly window scoped to one model"
    else:
        window = f"{qp.window_name(minutes)} window"
    account = event.get("account")
    suffix = f" on account {account}" if isinstance(account, str) and account else ""
    return f"our {PRODUCT} {window}{suffix}"


def concordance_clause(event: dict[str, Any]) -> str:
    """What the other Max 20x account's weekly window was doing, in words."""
    answer = event.get("concordance")
    if answer == CONCORDANCE_AGREEING:
        return (
            "Our other Max 20x account's weekly window cleared inside the same two "
            "polls, so two accounts saw it."
        )
    if answer == CONCORDANCE_DISAGREE:
        return (
            "Our other Max 20x account's weekly window was readable and did not "
            "clear, so this is one account's window and nothing wider."
        )
    # `unknown` covers several causes at once — the other account was idle,
    # blind, not yet sampled, or had already cleared and so was under the
    # detection floor when this event was written. Naming one of them would be
    # inventing a reason, so the sentence claims only the absence of support.
    return (
        "We cannot say what our other Max 20x account's weekly window was doing at "
        "that moment, so nothing corroborates this."
    )


def observation_sentences(event: dict[str, Any], verdict: str | None) -> tuple[str, str]:
    """The headline and the evidence line, built from fields and nothing else.

    Every clause prints a value that is on the record. The phrase "no reset
    happened" is never written: two correlated accounts on one plan tier cannot
    say that, and the strongest claim available is that nothing this account
    did explains what it saw.
    """
    reference = window_reference(event)
    subject = reference[0].upper() + reference[1:]
    change = (
        f"{qp._percent(event.get('used_before'))} to "
        f"{qp._percent(event.get('used_after'))}"
    )
    lo = qp.pacific(event.get("clear_bracket_lo"))
    hi = qp.pacific(event.get("clear_bracket_hi")) or qp.pacific(event.get("detected_at"))
    anchor = qp.pacific(event.get("prior_active_resets_at")) or "a time we never recorded"
    early = qp.describe_early(event.get("early_by_seconds")) or "an unmeasured amount"
    hours = qp.early_by_hours(event.get("early_by_seconds"))

    if lo and hi and lo != hi:
        moment = f"between {lo} and {hi}"
    elif hi:
        moment = f"by {hi}"
    else:
        moment = "at a time we never recorded"

    if event.get("kind") == qp.EVENT_RETRACTION:
        headline = f"A clear we recorded on {reference} did not hold; we withdrew it."
        returned = event.get("used_at_revert")
        when = qp.pacific(event.get("reverted_at"))
        if isinstance(returned, (int, float)) and not isinstance(returned, bool) and when:
            # Anthropic keeps the anchor across a vendor reset, so the anchor
            # cannot withdraw a clear here; only usage snapping back to the
            # level it held before the clear can, which is the sentence.
            return (
                headline,
                f"Usage returned to {qp._percent(returned)} at {when}, back at the "
                f"{qp._percent(event.get('used_before'))} it held before the clear, so "
                "the window had not actually reset.",
            )
        return (
            headline,
            "This record does not carry the usage the detector saw when it withdrew "
            "the clear, so nothing more is claimed about it.",
        )

    if event.get("classification") == CLASS_SESSION_ONLY:
        return (
            f"{subject} cleared.",
            f"{change} {moment}. Ordinary expiry and the feature-flagged /limit-reset "
            "command produce the same clear on a 5-hour window, so this is never "
            "evidence about the vendor and is not published.",
        )

    if verdict == qp.VERDICT_NATURAL_EXPIRY:
        return (
            f"{subject} reached its scheduled expiry.",
            f"{change} {moment}. The expiry time already on record was {anchor}, so this "
            "is our own window reaching the end it was scheduled for, not an extra reset.",
        )

    gap = f"before its scheduled expiry of {anchor}"
    if hours is not None:
        # The clear is bracketed between two polls, so the true gap is at least
        # this: measured from the later end, which is the conservative one.
        gap = f"at least {hours} hours {gap}"

    if verdict == qp.VERDICT_VENDOR_RESET:
        return (
            f"{subject} cleared {early} early, and nothing this account did explains it.",
            f"{change} {moment}, {gap}. Claude Code has no banked reset credit for an "
            "account holder to spend, and the /limit-reset command clears only the "
            "5-hour window, so nothing this account could have done produces this. "
            f"{concordance_clause(event)}",
        )

    if verdict == qp.VERDICT_UNRESOLVED:
        return (
            f"{subject} cleared, but we cannot say whether it was a reset.",
            f"{change} {moment}. No scheduled-expiry time was on record for this window, "
            "so a natural expiry cannot be ruled out.",
        )

    # No verdict this exporter recognises. Say that, rather than reaching for
    # the nearest one: every verdict is a claim.
    return (
        f"{subject} recorded something this exporter cannot classify.",
        "The probe wrote a record in a shape this export does not recognise, so nothing "
        "is claimed about it.",
    )


def is_public(event: dict[str, Any], retracted_ids: set[str]) -> bool:
    """May this observation be shown to anyone but the owner?

    Four independent gates, because the cost of a wrong yes is publishing a
    claim about a vendor we cannot defend:

    1. Only a confirmed CLEAR. A retraction is something we have withdrawn.
    2. Only a WEEKLY window. `session_only` is not in the allowlist, and the
       window length is checked again here so that a future record whose
       classification was written before the gate existed still cannot pass.
    3. Only a classification in the allowlist, with an event id to check
       against the retraction log.
    4. A vendor_reset that another readable account did NOT see is an owner
       alert, not evidence. It stays in the owner's file.
    """
    if event.get("kind") != qp.EVENT_CLEAR or not event.get("confirmed"):
        return False
    if qp.window_minutes_of(event) != WEEKLY_MINUTES:
        return False
    if event.get("classification") not in PUBLIC_CLASSIFICATIONS:
        return False
    if not event.get("event_id") or event.get("event_id") in retracted_ids:
        return False
    if (
        event.get("classification") == qp.CLASS_GLOBAL
        and event.get("concordance") == CONCORDANCE_DISAGREE
    ):
        return False
    return True


def observation(event: dict[str, Any], retracted_ids: set[str]) -> dict[str, Any]:
    """One event record in the shared contract's shape.

    The raw record is carried underneath as the audit trail for every sentence
    above; build.py copies only whitelisted keys onto the site, so an internal
    field (the model display name, a reason string) cannot leak by being here.
    """
    verdict = verdict_of(event)
    if event.get("kind") == qp.EVENT_RETRACTION:
        observed_at = event.get("reverted_at")
    else:
        observed_at = event.get("clear_bracket_hi") or event.get("detected_at")
    headline, evidence = observation_sentences(event, verdict)
    row = {
        **event,
        "observed_at": qp.iso_utc(observed_at),
        "window": qp.window_name(qp.window_minutes_of(event)),
        "verdict": verdict,
        "public": is_public(event, retracted_ids),
        "headline": headline,
        "evidence": evidence,
        "observed_before": qp.iso_utc(event.get("clear_bracket_lo")),
        "observed_after": qp.iso_utc(event.get("clear_bracket_hi")),
        "early_by_hours": qp.early_by_hours(event.get("early_by_seconds")),
    }
    if event.get("kind") == qp.EVENT_CLEAR and event.get("event_id") in retracted_ids:
        row["retracted"] = True
    return row


def coverage_sentence(
    peaks: dict[str, float],
    first_sample_t: int | None,
    counts: dict[str, int] | None = None,
) -> str:
    """One honest sentence naming the population this probe speaks for.

    Never "we saw no reset". What it says is which accounts carry enough weekly
    usage that a reset would have been visible at all, and which do not, so a
    reader can tell silence from evidence. Measured from the sample log rather
    than written down, because "this account has never been busy" stops being
    true the first week it is.
    """
    counts = counts or {}
    weekly_peak: dict[str, float] = {}
    weekly_samples: dict[str, int] = {}
    for key, peak in peaks.items():
        label = account_of_slot(key)
        if label is None or not key.endswith(f"/{WEEKLY_MINUTES}"):
            continue
        weekly_peak[label] = max(weekly_peak.get(label, 0.0), peak)
        weekly_samples[label] = weekly_samples.get(label, 0) + counts.get(key, 0)

    session_note = (
        f"A 5-hour clear is never published: ordinary expiry and the /limit-reset "
        f"command produce the same shape."
    )
    if not weekly_peak:
        return (
            f"{ACCOUNTS_PHRASE}. No weekly samples are on record on this host, so "
            f"nothing can be observed yet. {session_note}"
        )

    since = f" since {format_epoch_pacific(first_sample_t)}" if first_sample_t else ""
    active = sorted(l for l, peak in weekly_peak.items() if peak >= qp.CLEAR_DROP_MIN)
    quiet = sorted(l for l, peak in weekly_peak.items() if peak < qp.CLEAR_DROP_MIN)
    missing = sorted(l for l in LABELS if l not in weekly_peak)

    if not active:
        return (
            f"{ACCOUNTS_PHRASE}, weekly windows only. No weekly window has carried more "
            f"than {max(weekly_peak.values()):g}% of its limit{since}, under the "
            f"{qp.CLEAR_DROP_MIN:g}-point drop a clear needs, so nothing can be observed "
            f"yet. {session_note}"
        )

    parts = [
        f"{ACCOUNTS_PHRASE}, weekly windows only; "
        f"{'account ' + ' and '.join(active) if len(active) == 1 else 'accounts ' + ' and '.join(active)}"
        f" {'has' if len(active) == 1 else 'have'} carried enough usage for a clear to show."
    ]
    for label in quiet:
        samples = weekly_samples.get(label, 0)
        seen = f" on all {samples} readings taken{since}" if samples else since
        parts.append(
            f"Account {label}'s weekly window has never read above "
            f"{weekly_peak[label]:g}%{seen}, under the {qp.CLEAR_DROP_MIN:g}-point drop a "
            f"clear needs, so nothing can be observed on it."
        )
    for label in missing:
        parts.append(
            f"Account {label} has no weekly samples on record, so nothing can be "
            f"observed on it."
        )
    parts.append(session_note)
    return " ".join(parts)


def export_status(health: dict[str, Any], now: int) -> tuple[str, int | None]:
    """(status, last_verified_at) across BOTH accounts.

    With more than one account the probe is only healthy when they all are: a
    blind half cannot be rendered as a clean look, and `last_verified_at` is
    the OLDER of the two successes so the site's freshness badge can never be
    fresher than the stalest account behind it.
    """
    statuses = []
    verified: list[int] = []
    for label in LABELS:
        entry = health.get(HEALTH_LABELS[label])
        entry = entry if isinstance(entry, dict) else {}
        statuses.append(qp.probe_status(entry, now))
        moment = entry.get("last_ok_at")
        if isinstance(moment, int):
            verified.append(moment)
    if all(status == qp.PROBE_ABSENT for status in statuses):
        return qp.PROBE_ABSENT, None
    if any(status != qp.PROBE_OK for status in statuses):
        return qp.PROBE_BLIND, (min(verified) if len(verified) == len(LABELS) else None)
    return qp.PROBE_OK, (min(verified) if verified else None)


def export_observations(path: Path, now: int | None = None) -> int:
    """Write the probe's observations for the site and the notifier to consume.

    On-disk state only: no network, nothing that can hang. The file lands in
    the gitignored data/observed/ directory, never beside the tracked
    data/anthropic.json seed, which build.py would clobber.
    """
    now = int(time.time()) if now is None else now
    events = qp.read_events(EVENTS_FILE)
    retracted = {
        event["event_id"]
        for event in events
        if event.get("kind") == qp.EVENT_RETRACTION and event.get("event_id")
    }
    seen: set[str] = set()
    observations = []
    for event in events:
        key = qp.collapse_key(event)
        if key in seen:
            continue
        seen.add(key)
        observations.append(observation(event, retracted))

    health = qp.load_health()
    status, verified_at = export_status(health, now)
    peaks, first_sample_t, sample_counts = qp.slot_history(SAMPLES_FILE)
    detectable: dict[str, bool] = {}
    accounts: dict[str, Any] = {}
    for label in LABELS:
        entry = health.get(HEALTH_LABELS[label])
        entry = entry if isinstance(entry, dict) else {}
        detectable.update(entry.get("detectable_now") or {})
        accounts[HEALTH_LABELS[label]] = {
            "status": qp.probe_status(entry, now),
            "last_verified_at": qp.iso_utc(entry.get("last_ok_at")),
            "throttled_until": qp.iso_utc(entry.get("throttled_until")),
            "token_stale": bool(entry.get("token_stale")),
            "last_error": entry.get("last_error"),
        }
    payload = {
        "vendor": EXPORT_VENDOR,
        "exported_at": now,
        "probe": {
            "status": status,
            "last_verified_at": qp.iso_utc(verified_at),
            "detectable_now": detectable,
            "coverage": coverage_sentence(peaks, first_sample_t, sample_counts),
            # Outside the shared contract and not copied onto the site by
            # build.py's whitelist. Kept because this file is also what the
            # owner reads when the probe itself is the thing that is wrong.
            "label": "claude",
            "accounts": accounts,
        },
        "observations": observations,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    public = sum(1 for row in observations if row["public"])
    log(f"exported {len(observations)} observations ({public} public) to {path}")
    return 0


# ─── Runner ──────────────────────────────────────────────────────────────────


class AccountProbe:
    """One account's transport, throttle ladder and health, in one place.

    Everything per-account lives here because the two accounts fail
    independently: one credential can expire, or one token can be throttled,
    while the other keeps reading. Sharing a single failure counter between
    them would report a working account as blind, and a blind account as
    working — the second of which is the one that would put a false "not
    observed" in front of a subscriber.
    """

    def __init__(self, label: str, path: Path) -> None:
        self.label = label
        self.path = path
        self.health_label = HEALTH_LABELS.get(label, label)
        self.last_ok_at: int | None = None
        self.blind_since: int | None = None
        self.consecutive_failures = 0
        self.throttled_until: int | None = None
        self.token_stale = False
        self.last_error: str | None = None
        self.backoff = THROTTLE_BASE_SECONDS
        # Whether the LAST poll actually sent a request. A skipped poll is not
        # a failed one: neither a stale credential nor an active backoff is
        # fixed by systemd restarting the process.
        self.attempted = False

    # ─── health carried across a restart ─────────────────────────────────
    def resume(self, now: int) -> None:
        carried = resume_health(self.label, now)
        self.last_ok_at = carried.get("last_ok_at")
        self.blind_since = carried.get("blind_since")
        self.throttled_until = carried.get("throttled_until")
        if self.throttled_until:
            # Resume the ladder at least where it was, not back at ten
            # minutes: a crash loop must not be able to walk this probe
            # through a 429 the endpoint is still enforcing.
            self.backoff = min(
                THROTTLE_MAX_SECONDS,
                max(THROTTLE_BASE_SECONDS, self.throttled_until - now),
            )
            log(
                f"RESUMED THROTTLE {self.health_label}: no request before "
                f"{qp.iso_utc(self.throttled_until)}"
            )

    # ─── one poll ────────────────────────────────────────────────────────
    def poll(self, now: int) -> list[dict[str, Any]] | None:
        """Rows from one read, or None when the poll was skipped or failed."""
        self.attempted = False
        if self.throttled_until is not None and now < self.throttled_until:
            log(
                f"THROTTLED {self.health_label}: skipping this poll, "
                f"{self.throttled_until - now}s left on the backoff"
            )
            return None
        self.throttled_until = None

        try:
            token, expires_at, subscription = read_credentials(self.path)
        except CredentialError as exc:
            self._fail(now, exc, blind=True)
            return None

        if expires_at is not None and expires_at <= now:
            # A request on an expired token is a certain 401 and still costs
            # one of the ~25-30 requests per 15 minutes this token is allowed.
            self.token_stale = True
            self.consecutive_failures += 1
            self.last_error = f"credential expired at {qp.iso_utc(expires_at)}"
            log(
                f"TOKEN STALE {self.health_label}: expiresAt passed at "
                f"{qp.iso_utc(expires_at)} — not spending a request on a certain 401"
            )
            return None

        self.attempted = True
        try:
            payload = fetch_usage(token, now=now)
            rows = normalise_usage(payload, self.label, now, plan_type=subscription)
        except ThrottledError as exc:
            self._throttle(now, exc)
            return None
        except AuthError as exc:
            self.token_stale = True
            self._fail(now, exc, blind=False)
            log(
                f"AUTH FAILED {self.health_label}: the credential is no longer "
                "accepted. A human must log in again; this probe never refreshes "
                "tokens, because a refresh here would rotate the one the editor holds."
            )
            return None
        except qp.ProbeError as exc:
            self._fail(now, exc, blind=True)
            return None

        self.consecutive_failures = 0
        self.token_stale = False
        self.blind_since = None
        self.last_error = None
        self.backoff = THROTTLE_BASE_SECONDS
        self.last_ok_at = now
        return rows

    def _fail(self, now: int, exc: Exception, *, blind: bool) -> None:
        self.consecutive_failures += 1
        self.last_error = redact(str(exc))[:200]
        if blind and self.blind_since is None:
            self.blind_since = now
        log(
            f"PROBE FAILED {self.health_label} "
            f"({self.consecutive_failures} in a row): {self.last_error}"
        )
        if self.consecutive_failures % BLIND_LOG_EVERY == 0:
            log(
                f"PROBE BLIND {self.health_label}: {self.consecutive_failures} "
                "consecutive failures"
            )

    def _throttle(self, now: int, exc: ThrottledError) -> None:
        """Back off, and say for how long and on whose authority.

        `blind_since` is deliberately NOT set: groundtruth.probe_status checks
        it before `throttled_until`, and "throttled" is the more useful of the
        two for the owner. The failure counter is still incremented, so every
        reader that only knows the generic rule (quota_probe.probe_status)
        still reports this account as blind rather than as a clean look.
        """
        wait = exc.retry_after if exc.retry_after is not None else self.backoff
        wait = max(1, min(int(wait), THROTTLE_MAX_SECONDS))
        self.backoff = min(self.backoff * 2, THROTTLE_MAX_SECONDS)
        self.throttled_until = now + wait
        self.consecutive_failures += 1
        self.last_error = redact(str(exc))[:200]
        source = "Retry-After" if exc.retry_after is not None else "our own backoff"
        log(
            f"THROTTLED {self.health_label}: HTTP 429, waiting {wait}s ({source}); "
            f"next attempt no earlier than {qp.iso_utc(self.throttled_until)}"
        )

    def health(self, detector: qp.Detector, now: int) -> dict[str, Any]:
        return health_block(
            detector,
            self.label,
            now,
            last_ok_at=self.last_ok_at,
            consecutive_failures=self.consecutive_failures,
            blind_since=self.blind_since,
            throttled_until=self.throttled_until,
            token_stale=self.token_stale,
            last_error=self.last_error,
        )


def poll_all(
    probes: list[AccountProbe],
    detector: qp.Detector,
    claude_state: dict[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    """One round across every account: read, detect, persist, publish health.

    Samples and events are appended per account as they are produced, so a
    failure on the second account cannot lose the first account's reading.
    """
    written = 0
    events_written = 0
    attempted = 0
    succeeded = 0
    for probe in probes:
        moment = int(time.time()) if now is None else now
        rows = probe.poll(moment)
        if probe.attempted:
            attempted += 1
        if rows is None:
            continue
        succeeded += 1
        # Before observe(): changed_rows compares against the detector's
        # previous sample, which observe() is about to overwrite.
        to_write = qp.changed_rows(rows, detector)
        events = observe_rows(detector, rows, claude_state)
        written += qp.append_jsonl(SAMPLES_FILE, to_write)
        events_written += qp.append_jsonl(EVENTS_FILE, events)
        log_events(events)

    save_cursor({**detector.cursor(), "claude": claude_state})
    stamp = int(time.time()) if now is None else now
    for probe in probes:
        # Every iteration, success or failure: this file IS the heartbeat, and
        # write_health merges into the shared file so the codex block survives.
        qp.write_health(probe.health(detector, stamp), probe.health_label)

    if succeeded:
        log(
            f"poll ok accounts={succeeded}/{len(probes)} "
            f"written={written} events={events_written}"
        )
    return {
        "t": stamp,
        "attempted": attempted,
        "succeeded": succeeded,
        "written": written,
        "events": events_written,
    }


def run(once: bool) -> int:
    cursor = load_cursor()
    detector = qp.Detector(cursor)
    claude_state = cursor.get("claude")
    if not isinstance(claude_state, dict):
        claude_state = {}
    probes = [
        AccountProbe(label, path) for label, path in sorted(credential_paths().items())
    ]
    started_at = int(time.time())
    for probe in probes:
        probe.resume(started_at)

    all_failed_streak = 0
    while True:
        started = time.time()
        try:
            summary = poll_all(probes, detector, claude_state)
        except OSError as exc:
            # A full or read-only state directory raises out of append_jsonl /
            # save_cursor. Letting it escape kills the process with a traceback
            # and exit 1 — never the designed exit 2 — and systemd then
            # restarts it at RestartSec, so the loop polls a per-token
            # rate-limited endpoint every 60 seconds until someone notices.
            # Count it as a failed round and keep the ladder.
            log(f"POLL FAILED: {exc}")
            summary = {"succeeded": 0, "attempted": len(probes)}
        if summary["succeeded"]:
            all_failed_streak = 0
        elif summary["attempted"]:
            all_failed_streak += 1

        if all_failed_streak >= BLIND_EXIT_FAILURES:
            # Staying up while blind is what let a 35-poll Codex outage run
            # unnoticed. Exiting hands the problem to systemd, whose
            # Restart=always/RestartSec=60 retries with a clean process — and
            # resume() reads the backoff back off the health file, so the
            # restart cannot punch through a throttle.
            log(
                f"PROBE EXITING: {all_failed_streak} consecutive polls where every "
                "account was tried and every account failed — restarting under systemd"
            )
            sys.exit(2)

        if once:
            # Non-zero when nothing was read, so a smoke test cannot pass on a
            # probe that never actually saw anything.
            return 0 if summary["succeeded"] else 1
        time.sleep(max(1.0, POLL_SECONDS - (time.time() - started)))


def show() -> int:
    """Print what each account's usage endpoint says right now, and record nothing.

    One request per account, which is the whole point of the mode: it is the
    cheapest way to confirm the contract still holds. No token is printed, and
    an account whose credential has already expired is reported without a
    request being spent on it.
    """
    now = int(time.time())
    snapshot: dict[str, Any] = {}
    read_any = False
    for label, path in sorted(credential_paths().items()):
        entry: dict[str, Any] = {}
        try:
            token, expires_at, subscription = read_credentials(path)
        except CredentialError as exc:
            snapshot[HEALTH_LABELS[label]] = {"error": redact(str(exc))}
            continue
        entry["credential_expires_at"] = qp.iso_utc(expires_at)
        entry["subscription_type"] = subscription
        if expires_at is not None and expires_at <= now:
            entry["error"] = "credential expiresAt has passed; no request was sent"
        else:
            try:
                entry["rows"] = normalise_usage(
                    fetch_usage(token, now=now), label, now, plan_type=subscription
                )
                read_any = True
            except qp.ProbeError as exc:
                entry["error"] = redact(str(exc))
        snapshot[HEALTH_LABELS[label]] = entry
    print(json.dumps(snapshot, indent=2))
    return 0 if read_any else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current snapshot as JSON and exit without recording",
    )
    parser.add_argument(
        "--export",
        metavar="PATH",
        help="write the observation export from on-disk state only (no network)",
    )
    args = parser.parse_args(argv)

    if args.export:
        try:
            return export_observations(Path(args.export))
        except qp.ProbeError as exc:
            # publish.sh keeps the previous export on a non-zero exit, so an
            # unreadable state directory costs a stale observation column
            # rather than an empty one that reads as "no resets".
            log(f"EXPORT FAILED: {exc}")
            return 1

    if args.show:
        return show()

    return run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
