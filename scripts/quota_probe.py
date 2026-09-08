#!/usr/bin/env python3
"""Ground-truth reset detector: watch this account's real Codex quota.

The tweet feed is a single, semantically unreliable source — 12 of 48 tracked
announcements are forecasts ("lands in the next hour", "in the next few
hours"), 3 are banked-credit grants rather than resets, and upstream has been
observed 4h47m late on a real post. This probe reads the account's own
authenticated quota through Codex's app-server RPC so a reset can be timed
directly instead of inferred from a tweet.

Phase 1 is observation only: it records samples and classifies clears, but
never emails. `subscriptions.py notify` is untouched.

Detection rests on four measured facts about the Codex weekly window:

1. The window is ROLLING and anchored by OBSERVATION, not scheduled. While
   used_percent > 0 the anchor is frozen; the moment usage reads 0, every
   subsequent read pushes resets_at to now + window. Verified live with two
   probes 50s apart: codex/primary (used=14) held resets_at exactly, while
   both slots of the account's second limit id (used=0) advanced by
   exactly the probe gap.
   Consequence: the poller itself re-anchors idle windows, so the comparison
   below must use the last anchor seen while the window was ACTIVE.

2. Therefore the working discriminator is the clear time against the anchor
   that was in force before the clear. A clear at or after that anchor is the
   window simply expiring; a clear materially before it means something forced
   it. Over 22 historical clears, 21 were early (by 33.6-164.0 hours) and 1
   was a natural expiry.

3. A forced clear is still ambiguous between "the account holder spent a
   banked reset credit" and "the vendor reset everyone". The live RPC exposes
   rateLimitResetCredits.availableCount, which decrements only in the former
   case, so the two are separable locally. (Session rollout logs do NOT carry
   this field, which is why an earlier pass concluded it was unavailable.)

4. That credit field is not always readable. It flapped between an int and
   None on 159 of 368 sample rows between 2026-09-03 21:49 and 2026-09-04
   02:37 EDT — 43%. None means "unreadable", never "empty", so the last int
   reading is carried forward for a bounded number of polls; without that, a
   clear landing on a None read would be published as a vendor reset with the
   note "credit bank unchanged" even when the owner had just spent their own
   banked credit.

The probe can also stop seeing anything at all: the RPC returned HTTP 404 for
35 consecutive polls on 2026-09-03 (10:43-11:17 EDT) and nothing alerted. So
every iteration writes probe_health.json — the file, not a cursor mtime, is
the heartbeat — and a sustained outage exits non-zero for systemd to restart
and alert on. A clear that happened inside such a gap is still recoverable
when the window is active on both sides of it: see is_anchor_jump_clear.

A clear must also persist across CONFIRM_POLLS reads before it is trusted.
That guards against a single glitched read only; it cannot cover the
2026-08-03 shape, where the weekly window went 91% -> 0% in 3.7s, held for
12h41m, then reverted to 92% ON ITS ORIGINAL ANCHOR. That shape is caught
after the fact by the retraction path: a confirmed clear stays under watch
while the window reads idle, and usage returning on the ORIGINAL anchor (or
snapping back to its pre-clear level) retracts it. Usage returning on a NEW
anchor is the opposite signal — the window really was re-anchored — and
closes the watch. Before this distinction existed the detector retracted
the 2026-09-02 clear 79 polls after confirming it, because the account
simply started using the cleared window (0%, 1%, ... 6%).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # Run by systemd and by publish.sh as an absolute path, which puts
    # scripts/ on sys.path rather than the repository root.
    sys.path.insert(0, str(ROOT))

from scripts.timefmt import format_epoch_pacific  # noqa: E402

# ─── Configuration ───────────────────────────────────────────────────────────

STATE_DIR = Path(os.environ.get("AI_RESETS_STATE", "/var/lib/ai-resets"))
SAMPLES_FILE = STATE_DIR / "quota_samples.jsonl"
EVENTS_FILE = STATE_DIR / "quota_events.jsonl"
UPSTREAM_FILE = STATE_DIR / "upstream_snapshots.jsonl"
CURSOR_FILE = STATE_DIR / "quota_cursor.json"
# Shared with every other probe: one top-level key per label. Read by
# scripts/subscriptions.py, which treats a missing, malformed or stale block as
# "the probe is blind" and alerts the owner.
HEALTH_FILE = STATE_DIR / "probe_health.json"
PROBE_LABEL = "codex"

POLL_SECONDS = int(os.environ.get("AI_RESETS_POLL_SECONDS", "60"))
APP_SERVER_TIMEOUT = float(os.environ.get("AI_RESETS_RPC_TIMEOUT", "25"))

# `codex` on this box's PATH is a statusline wrapper, not the CLI, and PATH
# under systemd does not include ~/.local/bin at all. Point at the real
# binary explicitly so the service does not depend on either.
CODEX_BIN = os.environ.get("AI_RESETS_CODEX_BIN", "codex")

# A clear is a drop of at least this many points that lands at or below the
# floor. Both bounds matter: the floor alone would fire on a window that was
# barely used, and the drop alone would fire on the 100% -> 55% limit-increase
# transitions seen on 2026-08-16.
CLEAR_DROP_MIN = 10.0
SNAPBACK_TOLERANCE = 10.0
CLEAR_FLOOR = 5.0

# A clear landing within this margin after the prior anchor is a natural
# expiry. The margin absorbs the poll interval plus server-side rounding
# (measured at roughly -0.5s on the anchor itself).
NATURAL_GRACE_SECONDS = 2 * POLL_SECONDS + 60

# Consecutive confirming reads required before a clear is trusted.
CONFIRM_POLLS = 2

# The server rounds resets_at; the same frozen anchor has been read as
# 1788649350 and 1788649351 on consecutive polls. Two anchors closer than
# this are the same anchor.
ANCHOR_JITTER_SECONDS = 10

# The loop shouts every fifth failure and gives up at the tenth. Measured need:
# on 2026-09-03 the RPC answered HTTP 404 for 35 consecutive polls (10:43-11:17
# EDT) and nothing alerted — run() logged "PROBE BLIND" exactly once, never
# exited, so systemd's Restart=always never engaged and an OnFailure= unit
# would have had nothing to fire on. Exiting non-zero is what turns a silent
# outage into a restart plus an alert.
BLIND_LOG_EVERY = 5
BLIND_EXIT_FAILURES = 10

# rateLimitResetCredits.availableCount flapped between an int and None on 159
# of 368 sample rows between 2026-09-03 21:49 and 2026-09-04 02:37 EDT. A None
# reading is the field being unreadable, never a bank of zero, so the last int
# reading still describes the bank for this many poll intervals — after that
# the bank could have moved unseen and "unknown" is the honest answer.
CREDITS_MAX_AGE_POLLS = 10
# Say so once when a streak of absent readings passes this many polls.
CREDITS_ABSENT_LOG_AFTER = 10

# A restarted probe inherits the previous block's outage start only while that
# block is still fresh; a month-old one would make the alert claim a month-long
# blindness. Matches the reader's staleness cutoff in scripts/groundtruth.py.
HEALTH_CARRY_SECONDS = 15 * 60

EVENT_CLEAR = "clear"
EVENT_RETRACTION = "retraction"
# An observation about THIS account's banked credits, not about a window: the
# bank went 0 -> 1 at 2026-09-03 22:39:09 EDT, about 3h27m after a post saying
# a banked reset would land. Never a clear, never classified.
EVENT_CREDIT_GRANTED = "credit_granted"
# Reserved: no phase emits a limit change yet, but the export already has to
# rank one, and the string belongs in exactly one place when it arrives.
EVENT_LIMIT_CHANGE = "limit_change"

# Vendor this probe speaks for, in the export envelope.
EXPORT_VENDOR = "openai"

UPSTREAM_SOURCES = {
    "codex-resets.com": "https://codex-resets.com/api/resets",
    "codex-reset.com": "https://codex-reset.com/api/feed",
}
UPSTREAM_EVERY_SECONDS = int(os.environ.get("AI_RESETS_UPSTREAM_SECONDS", "300"))

CLASS_NATURAL = "natural_expiry"
CLASS_SELF = "self_applied_credit"
CLASS_GLOBAL = "global_candidate"
# No active anchor on record: natural expiry cannot be ruled out, so this
# must never be read as a global reset.
CLASS_UNRESOLVED = "unresolved"

# ─── Public vocabulary ───────────────────────────────────────────────────────

# What a reader may be told, in the exact strings the site and the email use.
# Deliberately not the CLASS_* names above: those record what the detector
# computed, these record what may be published. `global_candidate` exports as
# `vendor_reset` because that is the claim the evidence supports — this account
# saw its window cleared early and nothing this account did explains it — while
# the internal name keeps the bound that one Pro account cannot see a vendor.
VERDICT_NATURAL_EXPIRY = "natural_expiry"
VERDICT_SELF_APPLIED = "self_applied"
VERDICT_VENDOR_RESET = "vendor_reset"
VERDICT_UNRESOLVED = "unresolved"
VERDICT_LIMIT_CHANGE = "limit_change"
VERDICT_CREDIT_GRANTED = "credit_granted"

VERDICT_OF_CLASS: dict[str, str] = {
    CLASS_NATURAL: VERDICT_NATURAL_EXPIRY,
    CLASS_SELF: VERDICT_SELF_APPLIED,
    CLASS_GLOBAL: VERDICT_VENDOR_RESET,
    CLASS_UNRESOLVED: VERDICT_UNRESOLVED,
}

# The product every generated sentence is about. The site tracks three vendors,
# so "our weekly window" alone would not say whose.
PRODUCT = "Codex"

# Window durations in the words a reader already uses for them.
WINDOW_NAMES = {300: "5-hour", 1440: "daily", 10080: "weekly"}

PROBE_OK = "ok"
PROBE_BLIND = "blind"
PROBE_ABSENT = "absent"


class ProbeError(RuntimeError):
    """A probe failure that the caller should log and retry, not crash on."""


# ─── Codex app-server RPC ────────────────────────────────────────────────────


def _rpc_messages() -> list[dict[str, Any]]:
    return [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "ai_reset_watch",
                    "title": "AI Reset Watch probe",
                    "version": "1.0.0",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 2},
    ]


def read_rate_limits(timeout: float = APP_SERVER_TIMEOUT) -> dict[str, Any]:
    """Read an authenticated quota snapshot via `codex app-server --stdio`.

    Credentials stay inside the Codex binary; nothing here reads auth.json.
    The call is metadata-only: it consumes no model tokens and, per Codex's
    own tooling contract, no rate-limit reset credit.

    stdin is deliberately held open — closing it makes the server exit before
    it answers.
    """
    try:
        process = subprocess.Popen(
            [CODEX_BIN, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise ProbeError(f"cannot execute the Codex CLI at {CODEX_BIN!r}") from exc

    if process.stdin is None or process.stdout is None:
        process.kill()
        raise ProbeError("could not open the Codex app-server transport")

    try:
        for message in _rpc_messages():
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not selector.select(max(0.0, deadline - time.monotonic())):
                    break
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != 2:
                    continue
                if "error" in response:
                    detail = response["error"].get("message", "unknown error")
                    raise ProbeError(f"app-server rejected the read: {detail}")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise ProbeError("app-server returned a malformed result")
                return result
        raise ProbeError("timed out reading account rate limits")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


# ─── Snapshot normalisation ──────────────────────────────────────────────────


def credits_available(result: dict[str, Any]) -> int | None:
    """Count of unspent banked reset credits, or None when absent.

    None and 0 must stay distinct: an older Codex build that omits the field
    entirely would otherwise look like "the credit was just spent".
    """
    bank = result.get("rateLimitResetCredits")
    if not isinstance(bank, dict):
        return None
    count = bank.get("availableCount")
    return count if isinstance(count, int) else None


def normalise(result: dict[str, Any], now: int) -> list[dict[str, Any]]:
    """Flatten the RPC result into one row per (limit_id, window) slot."""
    by_id = result.get("rateLimitsByLimitId")
    if not isinstance(by_id, dict):
        single = result.get("rateLimits")
        by_id = {single.get("limitId", "codex"): single} if isinstance(single, dict) else {}

    bank = credits_available(result)
    rows: list[dict[str, Any]] = []
    for limit_id, limit in sorted(by_id.items()):
        if not isinstance(limit, dict):
            continue
        for slot in ("primary", "secondary"):
            window = limit.get(slot)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            minutes = window.get("windowDurationMins")
            if used is None or minutes is None:
                continue
            rows.append(
                {
                    "t": now,
                    "limit_id": limit_id,
                    "slot": slot,
                    "window_minutes": minutes,
                    "used_percent": float(used),
                    "resets_at": window.get("resetsAt"),
                    "plan_type": limit.get("planType"),
                    "credits_available": bank,
                }
            )
    return rows


def slot_key(row: dict[str, Any]) -> str:
    return f"{row['limit_id']}/{row['window_minutes']}"


# ─── Detection ───────────────────────────────────────────────────────────────


LIMIT_STEP_RATIOS = (2.0, 3.0, 4.0, 5.0)
LIMIT_STEP_SLACK = 0.15


def is_limit_step(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """A limit increase rescales the percent (100->55 on 2026-08-16; a 3x at
    15% reads 15->5) with the anchor and credit bank untouched. A real clear
    lands at ~0 or moves the anchor (Codex)."""
    before, after = previous["used_percent"], current["used_percent"]
    if after <= CLEAR_FLOOR / 2 or before <= after:
        return False
    if not same_anchor(previous.get("resets_at"), current.get("resets_at")):
        return False
    if previous.get("credits_available") != current.get("credits_available"):
        return False
    ratio = before / after
    return any(abs(ratio - r) <= r * LIMIT_STEP_SLACK for r in LIMIT_STEP_RATIOS)


def is_clear(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    drop = previous["used_percent"] - current["used_percent"]
    if is_limit_step(previous, current):
        return False
    return drop >= CLEAR_DROP_MIN and current["used_percent"] <= CLEAR_FLOOR


def is_anchor_jump_clear(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """A clear that happened between two ACTIVE reads, while we were blind.

    is_clear() compares consecutive polls and needs the cleared read itself to
    land at or below the floor. The probe was blind for 35 consecutive polls on
    2026-09-03, and a reset inside a gap like that is invisible to it once
    usage climbs back past the floor before we recover (100% -> [blind] -> 8%).
    What survives the gap is the anchor: Codex re-anchors resets_at to about
    now+window on a real clear, so a large drop whose anchor ALSO moved forward
    past the natural-expiry grace is that same event, seen late. A limit
    rescale (2026-08-16, 100% -> 55%) keeps its anchor, so it is excluded
    twice: by is_limit_step and by the anchor test itself.
    """
    if is_limit_step(previous, current):
        return False
    if current["t"] - previous["t"] <= 1.5 * POLL_SECONDS:
        # This predicate exists for a GAP. It emits a clear that is already
        # confirmed and opens no revert watch, so it can never be withdrawn —
        # on two ordinary consecutive polls that would be a single-sample,
        # unretractable clear, exactly what the 2026-09-02 fix removed
        # everywhere else. Without a gap there is nothing here to recover.
        return False
    if previous["used_percent"] <= CLEAR_FLOOR or current["used_percent"] <= CLEAR_FLOOR:
        return False
    if previous["used_percent"] - current["used_percent"] < CLEAR_DROP_MIN:
        return False
    before_anchor, after_anchor = previous.get("resets_at"), current.get("resets_at")
    if before_anchor is None or after_anchor is None:
        return False
    return after_anchor - before_anchor > NATURAL_GRACE_SECONDS


def resolve_credits(
    reading: Any,
    known: dict[str, Any] | None,
    row_t: int,
    poll_seconds: int = POLL_SECONDS,
) -> tuple[int | None, bool]:
    """(count, carried) for one reading of the banked-credit field.

    availableCount was unreadable on 43% of rows during the 2026-09-03/04 flap,
    and a clear landing on such a row would otherwise read as "credit bank
    unchanged" — publishing the owner's own banked credit as a vendor-wide
    reset. So a None reading falls back to the last int seen, and only for
    CREDITS_MAX_AGE_POLLS intervals. Ages are measured on ROW timestamps, never
    wall clock, so this holds identically when the sample log is replayed.
    """
    if isinstance(reading, int):
        return reading, False
    if not isinstance(known, dict) or not isinstance(known.get("value"), int):
        return None, False
    if not isinstance(known.get("t"), int):
        return None, False
    if row_t - known["t"] > CREDITS_MAX_AGE_POLLS * poll_seconds:
        return None, False
    return known["value"], True


def same_anchor(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= ANCHOR_JITTER_SECONDS


def is_revert(pending: dict[str, Any], row: dict[str, Any]) -> bool:
    """Did this clear un-happen (2026-08-03), or did usage merely resume?

    Two window contracts exist. Codex RE-ANCHORS on a real clear (resets_at
    jumps to ~now+window), so a revert is the ORIGINAL anchor coming back.
    Anthropic KEEPS the anchor on a vendor reset (counter zeroed, resets_at on
    its fixed lattice), so there the anchor says nothing and only an
    unambiguous snap-back to the pre-clear level can be a revert. Which
    contract applies is recorded on the pending record at detection time
    (`reanchored`), not configured per vendor.
    """
    if row["used_percent"] <= CLEAR_FLOOR:
        return False
    original = pending.get("prior_active_resets_at")
    current = row.get("resets_at")
    snapped_back = (
        pending["used_before"] >= 2 * CLEAR_DROP_MIN
        and row["used_percent"] >= pending["used_before"] - SNAPBACK_TOLERANCE
    )
    if pending.get("reanchored"):
        return same_anchor(original, current) or snapped_back
    return snapped_back


def classify(
    previous: dict[str, Any],
    current: dict[str, Any],
    active_anchor: int | None,
    credits_known: dict[str, Any] | None = None,
    poll_seconds: int = POLL_SECONDS,
) -> dict[str, Any]:
    """Apply the decision table to one observed clear.

    `active_anchor` is the last resets_at seen while this window still had
    usage on it. Using the immediately preceding sample's anchor instead would
    be wrong: once usage reads 0 the anchor tracks our own polling.

    `credits_known` is the slot's last int reading of the banked-credit field,
    `{"value": n, "t": epoch}`, used only where the immediate reading is None.
    """
    early_by = None
    if active_anchor is not None:
        # `current["t"]` is the first observation of the cleared state, so it
        # is an upper bound on the true clear moment. A value below the anchor
        # therefore proves the window cleared before its declared expiry.
        early_by = active_anchor - current["t"]

    before, before_carried = resolve_credits(
        previous.get("credits_available"), credits_known, previous["t"], poll_seconds
    )
    after, after_carried = resolve_credits(
        current.get("credits_available"), credits_known, current["t"], poll_seconds
    )
    carried = before_carried or after_carried
    credit_spent = (
        isinstance(before, int) and isinstance(after, int) and after < before
    )

    if early_by is None:
        verdict = CLASS_UNRESOLVED
        reason = "no active anchor on record yet; cannot rule out natural expiry"
    elif early_by <= NATURAL_GRACE_SECONDS:
        verdict = CLASS_NATURAL
        reason = (
            f"cleared within {NATURAL_GRACE_SECONDS}s of its declared expiry "
            f"(early_by={early_by}s)"
        )
    elif credit_spent:
        verdict = CLASS_SELF
        reason = f"banked credit count fell {before} -> {after}"
    else:
        verdict = CLASS_GLOBAL
        reason = f"cleared {early_by}s early with the credit bank unchanged"
        if carried:
            # Say which reading was inferred: "unchanged" is weaker evidence
            # when the field itself was absent on the row that showed the clear.
            reason += " (bank taken from the last readable count)"

    return {
        "classification": verdict,
        "reason": reason,
        "early_by_seconds": early_by,
        "prior_active_resets_at": active_anchor,
        "credits_before": before,
        "credits_after": after,
        "credits_carried": carried,
    }


class Detector:
    """Per-slot state machine over the sample stream."""

    def __init__(self, cursor: dict[str, Any] | None = None) -> None:
        self.slots: dict[str, dict[str, Any]] = (cursor or {}).get("slots", {})

    def cursor(self) -> dict[str, Any]:
        return {"slots": self.slots}

    def last_sample_t(self) -> int | None:
        """Timestamp of the newest normalised row across every slot."""
        times = [
            state["last"]["t"]
            for state in self.slots.values()
            if isinstance(state.get("last"), dict)
            and isinstance(state["last"].get("t"), int)
        ]
        return max(times) if times else None

    def detectable_now(self) -> dict[str, bool]:
        """Which slots are used enough that a clear on them would be visible.

        A window sitting at or under CLEAR_DROP_MIN cannot produce a drop this
        detector would call a clear, so "we did not see a reset" on it is not
        evidence of anything. The email has to be able to say "our window was
        too empty to show a reset" instead of "not seen".
        """
        readable: dict[str, bool] = {}
        for key, state in self.slots.items():
            last = state.get("last")
            used = last.get("used_percent") if isinstance(last, dict) else None
            # `>=`, matching is_clear (`drop >= CLEAR_DROP_MIN`) and
            # groundtruth.DETECTION_FLOOR_PERCENT. usedPercent arrives from the
            # RPC as an integer, so exactly 10 is reachable, and at 10 a drop to
            # zero really is a clear — the health file must not tell the
            # notifier the window was "too empty to show a reset".
            readable[key] = isinstance(used, (int, float)) and float(used) >= CLEAR_DROP_MIN
        return readable

    def observe(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Feed one sample in; return any events it produced."""
        key = slot_key(row)
        state = self.slots.setdefault(key, {})
        events: list[dict[str, Any]] = []
        previous = state.get("last")

        if previous is not None and is_clear(previous, row):
            anchor_before = state.get("active_anchor")
            state["pending"] = {
                **self._clear_record(state, key, previous, row),
                "reanchored": (
                    anchor_before is not None
                    and row.get("resets_at") is not None
                    and not same_anchor(anchor_before, row["resets_at"])
                ),
            }
        elif state.get("pending") is not None:
            pending = state["pending"]
            # A credit reading that arrives after the clear can still correct
            # it, as long as the clear has not been published yet.
            self._refresh_pending_credits(state, pending, row)
            if row["used_percent"] <= CLEAR_FLOOR:
                # Still idle: keep counting, and keep the watch open even
                # after confirmation so a 2026-08-03 style revert hours
                # later is still attributed to this clear.
                pending["confirmations"] += 1
                if pending["confirmations"] >= CONFIRM_POLLS and not pending.get("confirmed"):
                    pending["confirmed"] = True
                    pending["confirmed_at"] = row["t"]
                    pending["confirmed_by"] = "held_at_floor"
                    events.append(dict(pending))
            elif is_revert(pending, row):
                # The 2026-08-03 shape: a clear that un-happened. Emitted
                # whether or not the clear was already confirmed; the shared
                # event_id lets a consumer supersede the earlier record.
                events.append(
                    {
                        **pending,
                        "kind": EVENT_RETRACTION,
                        "confirmed": False,
                        "reverted_at": row["t"],
                        "used_at_revert": row["used_percent"],
                        "resets_at_at_revert": row.get("resets_at"),
                    }
                )
                state["pending"] = None
            else:
                # Usage resumed without a revert. On a re-anchoring contract
                # (Codex) that is stronger evidence than a second idle read:
                # confirm if the floor reads never got there. On a fixed
                # anchor a single 0% sample followed by usage proves nothing:
                # drop it, loudly.
                if not pending.get("confirmed"):
                    if pending.get("reanchored"):
                        pending["confirmed"] = True
                        pending["confirmed_at"] = row["t"]
                        pending["confirmed_by"] = "usage_resumed_on_new_anchor"
                        events.append(dict(pending))
                    else:
                        log(
                            f"UNCONFIRMED clear dropped {key} "
                            f"{pending['used_before']:.0f}%->{pending['used_after']:.0f}% "
                            f"(single idle read, anchor unchanged)"
                        )
                state["pending"] = None
        elif previous is not None and is_anchor_jump_clear(previous, row):
            # Emitted already confirmed, and deliberately WITHOUT opening a
            # watch: the window is active again, so is_revert would read
            # ordinary usage climbing back toward the pre-clear level as a
            # snap-back and retract a real reset — the same false-retraction
            # shape that cost us the 2026-09-02 event.
            events.append(
                {
                    **self._clear_record(state, key, previous, row),
                    "reanchored": True,
                    "confirmed": True,
                    "confirmed_at": row["t"],
                    "confirmed_by": "anchor_jump",
                }
            )

        if row["used_percent"] > CLEAR_FLOOR and row.get("resets_at") is not None:
            # Only an active window carries a trustworthy anchor. (The old
            # unconditional `pending = None` here is what made ordinary usage
            # look like a revert — pending is now closed only above, where the
            # revert/resume distinction is made.)
            state["active_anchor"] = row["resets_at"]

        events.extend(self._track_credits(state, key, row))
        state["last"] = row
        return events

    @staticmethod
    def _clear_record(
        state: dict[str, Any],
        key: str,
        previous: dict[str, Any],
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """The fields every clear carries, whatever signature found it.

        `clear_bracket_*` is the window the clear happened inside: for the
        consecutive-poll signature that is one poll interval, for an
        anchor jump it is the whole blind gap, which may be hours.
        """
        return {
            "event_id": f"{key}:{row['t']}",
            "kind": EVENT_CLEAR,
            "detected_at": row["t"],
            "slot": key,
            "limit_id": row["limit_id"],
            "window_minutes": row["window_minutes"],
            "used_before": previous["used_percent"],
            "used_after": row["used_percent"],
            "clear_bracket_lo": previous["t"],
            "clear_bracket_hi": row["t"],
            "confirmations": 1,
            **classify(previous, row, state.get("active_anchor"), state.get("credits_known")),
        }

    @staticmethod
    def _refresh_pending_credits(
        state: dict[str, Any], pending: dict[str, Any], row: dict[str, Any]
    ) -> None:
        """Let a late credit reading correct a clear we have not published yet.

        classify() runs on the very row that shows the clear, and that row's
        credit field was unreadable on 43% of samples during the 2026-09-03/04
        flap. If it read None and a following row reads a LOWER count, the
        spend was ours: publishing the first verdict (global_candidate, "credit
        bank unchanged") would put the owner's own banked credit out as a
        vendor-wide reset. Only that direction is corrected — nothing here can
        turn a self-applied clear into a global one.
        """
        if pending.get("confirmed") or pending.get("classification") != CLASS_GLOBAL:
            return
        before = pending.get("credits_before")
        after, carried = resolve_credits(
            row.get("credits_available"), state.get("credits_known"), row["t"]
        )
        if not isinstance(before, int) or not isinstance(after, int) or after >= before:
            return
        pending["classification"] = CLASS_SELF
        pending["credits_after"] = after
        pending["credits_carried"] = bool(pending.get("credits_carried")) or carried
        pending["reason"] = (
            f"banked credit count fell {before} -> {after}, read "
            f"{row['t'] - pending['detected_at']}s after the clear"
        )

    @staticmethod
    def _track_credits(
        state: dict[str, Any], key: str, row: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Follow the banked-credit count across the field's None flaps."""
        credit = row.get("credits_available")
        if not isinstance(credit, int):
            state["credits_absent"] = state.get("credits_absent", 0) + 1
            if state["credits_absent"] == CREDITS_ABSENT_LOG_AFTER + 1:
                log(
                    f"CREDIT FIELD ABSENT n={state['credits_absent']} on {key} — "
                    "classifying against the last readable count"
                )
            return []

        state["credits_absent"] = 0
        known = state.get("credits_known")
        previous_count = known.get("value") if isinstance(known, dict) else None
        state["credits_known"] = {"value": credit, "t": row["t"]}
        if not isinstance(previous_count, int) or credit <= previous_count:
            return []
        # A grant says something happened to THIS account's bank, not to any
        # window: no usage moved, so it is an observation only — never a clear,
        # never classified, and it must not open a watch.
        return [
            {
                "event_id": f"{key}:credit:{row['t']}",
                "kind": EVENT_CREDIT_GRANTED,
                "slot": key,
                "limit_id": row["limit_id"],
                "t": row["t"],
                "credits_before": previous_count,
                "credits_after": credit,
            }
        ]


# ─── Persistence ─────────────────────────────────────────────────────────────


def append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    records = list(records)
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return len(records)


def changed_rows(
    rows: list[dict[str, Any]], detector: Detector
) -> list[dict[str, Any]]:
    """Keep only rows whose meaningful fields moved since the last sample.

    At one read a minute an unfiltered log would run to tens of megabytes a
    year while saying nothing. resets_at is excluded from the comparison for
    idle windows because our own polling advances it every read.
    """
    keep = []
    for row in rows:
        state = detector.slots.get(slot_key(row), {})
        previous = state.get("last")
        if previous is None:
            keep.append(row)
            continue
        # A credit field that blinked out is "unknown, unchanged", not a
        # transition: the raw comparison wrote a row on each of the 159 flapped
        # readings on 2026-09-03/04, twice per flap. Comparing the RESOLVED
        # counts still logs a real move that happens to be seen across a gap
        # (1 -> None -> 0), which is the one that matters.
        known = state.get("credits_known")
        credits_before, _ = resolve_credits(
            previous.get("credits_available"), known, previous["t"]
        )
        credits_now, _ = resolve_credits(row.get("credits_available"), known, row["t"])
        moved = (
            previous["used_percent"] != row["used_percent"]
            or credits_before != credits_now
            or previous.get("plan_type") != row.get("plan_type")
        )
        anchor_moved = (
            row["used_percent"] > CLEAR_FLOOR
            and not same_anchor(previous.get("resets_at"), row.get("resets_at"))
            and not (previous.get("resets_at") is None and row.get("resets_at") is None)
        )
        if moved or anchor_moved:
            keep.append(row)
    return keep


def load_cursor() -> dict[str, Any]:
    try:
        return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cursor(cursor: dict[str, Any]) -> None:
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CURSOR_FILE)


# ─── Probe health ────────────────────────────────────────────────────────────


def load_health() -> dict[str, Any]:
    """Every probe's health block, or {} when the file is missing or corrupt."""
    try:
        loaded = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_health(entry: dict[str, Any], label: str = PROBE_LABEL) -> None:
    """Publish one probe's health block atomically, keeping the others'.

    This file is the ONLY thing that tells the notifier the probe went dark.
    quota_cursor.json's mtime cannot serve as a heartbeat: run() saves the
    cursor from the upstream-snapshot branch as well, and did so 8 times DURING
    the 35-poll blind spell on 2026-09-03, so its mtime stayed fresh while the
    detector saw nothing. Written on every iteration, failing ones included.

    The temp file is named per label so a second probe writing its own block
    concurrently cannot land on ours mid-write.
    """
    try:
        health = load_health()
        health[label] = entry
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_FILE.with_name(f"{HEALTH_FILE.name}.{label}.tmp")
        tmp.write_text(json.dumps(health, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, HEALTH_FILE)
    except OSError as exc:
        # A full disk or a read-only remount must not kill the poll loop. This
        # call sits outside the ProbeError handler, so an unguarded raise here
        # would exit 1 on the first iteration — never reaching the designed
        # exit 2, never logging PROBE EXITING, and turning Restart=always into
        # a restart storm. A heartbeat that cannot be written degrades to a
        # stale file, which every reader already treats as blind.
        log(f"HEALTH WRITE FAILED: {exc}")


def health_block(
    detector: Detector,
    now: int,
    *,
    last_ok_at: int | None = None,
    consecutive_failures: int = 0,
    blind_since: int | None = None,
    last_error: str | None = None,
    label: str = PROBE_LABEL,
) -> dict[str, Any]:
    """One probe's health, in the shape every reader of the file expects."""
    return {
        "label": label,
        "updated_at": now,
        "last_ok_at": last_ok_at,
        "last_sample_t": detector.last_sample_t(),
        "consecutive_failures": consecutive_failures,
        "blind_since": blind_since,
        # Reserved for the Claude probe (P2), which throttles per token at
        # roughly 25-30 requests per 15 minutes and reads an OAuth credential
        # that expires. Neither can happen to a local `codex app-server` call.
        "throttled_until": None,
        "token_stale": False,
        "detectable_now": detector.detectable_now(),
        "last_error": last_error,
    }


def resume_health(label: str = PROBE_LABEL) -> tuple[int | None, int | None]:
    """(last_ok_at, blind_since) carried over from the block on disk.

    A probe that exits on a sustained outage is restarted by systemd seconds
    later. Starting from nothing would make the owner alert say the outage
    began at the restart, so a FRESH previous block hands both timestamps
    forward; a stale one is ignored rather than made to claim a month of
    blindness. The first successful poll clears blind_since either way.
    """
    entry = load_health().get(label)
    if not isinstance(entry, dict):
        return None, None
    last_ok_at = entry.get("last_ok_at")
    last_ok_at = last_ok_at if isinstance(last_ok_at, int) else None

    updated_at = entry.get("updated_at")
    blind_since = entry.get("blind_since")
    fresh = (
        isinstance(updated_at, int)
        and int(time.time()) - updated_at <= HEALTH_CARRY_SECONDS
    )
    if not (fresh and isinstance(blind_since, int)):
        blind_since = None
    return last_ok_at, blind_since


# ─── Upstream corroboration snapshots ────────────────────────────────────────


def upstream_signature(record: dict[str, Any]) -> str:
    """Content hash of a snapshot, ignoring when it was taken."""
    body = {k: v for k, v in record.items() if k != "t"}
    return hashlib.md5(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()


def dedupe_upstream(
    records: list[dict[str, Any]], signatures: dict[str, str]
) -> list[dict[str, Any]]:
    """Keep only snapshots whose content moved since the last stored one.

    These feeds are polled far faster than they change — measured over the
    first 29h, codex-reset.com repeated itself on 98.3% of writes and
    codex-resets.com on 40.3%, together projecting to 331 MB/year of pure
    redundancy. Appending only on change gives change-log semantics: a record
    holds from its own `t` until the next record for that source.

    `signatures` is mutated in place so the caller can persist it.
    """
    keep = []
    for record in records:
        # An errored fetch says nothing about upstream content; recording it
        # would also corrupt the "this state held until the next row" reading.
        if record.get("error"):
            keep.append(record)
            continue
        signature = upstream_signature(record)
        if signatures.get(record["source"]) == signature:
            continue
        signatures[record["source"]] = signature
        keep.append(record)
    return keep


def snapshot_upstream(now: int) -> list[dict[str, Any]]:
    """Archive the corroborating feeds so they become backtestable.

    codex-resets.com's `watch` object carries a crowd forecast (reset_chance,
    forecast_window) and has been seen carrying corrections to an already
    published event; codex-reset.com carries a reset_kind of hard vs banked.
    Neither is stored anywhere today, so neither can be evaluated after the
    fact. This only records them — nothing consumes them yet.
    """
    records = []
    for name, url in UPSTREAM_SOURCES.items():
        record: dict[str, Any] = {"t": now, "source": name}
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "accept": "application/json",
                    "user-agent": "Mozilla/5.0 (compatible; ai-resets/1.0)",
                },
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
            record["status"] = response.status
            if name == "codex-resets.com":
                record["watch"] = payload.get("watch")
                record["stats"] = payload.get("stats")
                record["event_count"] = len(payload.get("events", []))
            else:
                events = payload.get("events", [])
                record["event_count"] = len(events)
                record["classified"] = [
                    {
                        "id": e.get("id"),
                        "announced_at": e.get("announced_at"),
                        "effective_at": e.get("effective_at"),
                        "reset_kind": e.get("reset_kind"),
                        "scope": e.get("scope"),
                        "confidence": e.get("confidence"),
                        "announcement_state": e.get("announcement_state"),
                        "reset_verification_status": e.get("reset_verification_status"),
                    }
                    for e in events
                    if e.get("reset_kind")
                ]
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)
    return records


# ─── Export ──────────────────────────────────────────────────────────────────


def normalise_event(record: dict[str, Any]) -> dict[str, Any]:
    """Fill in the fields that predate the current event schema.

    Both records in the live log were written on 2026-09-02, before commit
    d1015b0 added `kind` and `event_id`, and neither is guessed: only clears
    and retractions existed then, a retraction is the record carrying
    `reverted_at`, and the id was always "<slot>:<detected_at>". Reconstructing
    them on read keeps the export uniform without rewriting an append-only log.
    """
    if record.get("kind") and record.get("event_id"):
        return record
    filled = dict(record)
    filled.setdefault(
        "kind", EVENT_RETRACTION if record.get("reverted_at") else EVENT_CLEAR
    )
    if not filled.get("event_id") and record.get("slot") and record.get("detected_at"):
        filled["event_id"] = f"{record['slot']}:{record['detected_at']}"
    return filled


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every event record on disk, skipping anything unparseable.

    A half-written final line (the log is appended to by a live process) must
    cost one record, not the whole export.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        # A file that is PRESENT but unreadable is not an empty log. Reporting
        # it as zero observations would overwrite a good export and make "no
        # observations" look like "no resets", which is the one thing this
        # export exists to prevent.
        raise ProbeError(f"cannot read {path.name}: {exc}") from exc
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(normalise_event(record))
    return records


# An ALLOWLIST, not a denylist: a classification this build does not recognise
# must not become publishable by default. What is missing from it is deliberate
# — CLASS_SELF says how the account holder spends their own banked credit, and
# that is the owner's business, not the vendor's behaviour.
PUBLIC_CLASSIFICATIONS = (CLASS_NATURAL, CLASS_GLOBAL, CLASS_UNRESOLVED)


def is_public(event: dict[str, Any], retracted_ids: set[str]) -> bool:
    """May this observation be shown to anyone but the owner?

    The question this exporter exists to answer is whether an observed clear
    was the window expiring on schedule or the vendor pushing a reset out, so
    BOTH answers have to be publishable. A natural expiry and an unresolved
    clear are facts about our own window that name no vendor action, and
    saying "this one was just our window running out" is exactly as useful to
    a reader as saying "this one was not".

    What stays private is what describes the OWNER: a self-applied clear and a
    credit grant reveal how the account holder spends their own banked credit.
    A clear that was later retracted drops out too — publishing something we
    have already withdrawn and then correcting it is worse than staying quiet.
    """
    kind = event.get("kind")
    if kind == EVENT_LIMIT_CHANGE:
        return True
    if kind != EVENT_CLEAR:
        return False
    classification = event.get("classification")
    if classification not in PUBLIC_CLASSIFICATIONS:
        return False
    if not event.get("event_id"):
        # No identity means the retraction check below cannot apply, and a
        # publish decision must not resolve missing data toward publishing.
        return False
    if classification == CLASS_GLOBAL and (
        not isinstance(event.get("credits_before"), int)
        or not isinstance(event.get("credits_after"), int)
        or event.get("credits_carried")
    ):
        # global_candidate rests on ONE piece of evidence: the banked-credit
        # count did not fall, so the owner did not spend their own credit. When
        # that field was unreadable on the clear row — it was unreadable on 43%
        # of rows during the 2026-09-03/04 flap — the verdict's reason still
        # reads "the credit bank unchanged" while nothing was actually read.
        # Publishing that would announce the owner's own spend as a vendor-wide
        # reset, which is the single worst thing this exporter could do.
        return False
    return event.get("event_id") not in retracted_ids


def health_for_export(label: str = PROBE_LABEL) -> dict[str, Any]:
    """This label's health block, or a blind one when the file is absent.

    --export also runs where the probe does not — a worktree, a build host, or
    simply before the probe has ever written the file — so a missing file must
    not fail the export. It is reported as a probe with no updated_at, which
    every reader already treats as blind. What the cursor can still answer (the
    newest sample, which slots were usable) is filled in from there: the same
    process writes it, so it says what the probe last actually saw.
    """
    entry = load_health().get(label)
    if isinstance(entry, dict):
        return entry
    detector = Detector(load_cursor())
    return {
        "label": label,
        "updated_at": None,
        "last_ok_at": None,
        "last_sample_t": detector.last_sample_t(),
        "consecutive_failures": None,
        "blind_since": None,
        "throttled_until": None,
        "token_stale": False,
        "detectable_now": detector.detectable_now(),
        "last_error": f"no {HEALTH_FILE.name} on disk; state read from the cursor",
    }


# ─── Publishable words ───────────────────────────────────────────────────────


def _epoch(value: Any) -> int | None:
    """An epoch field as an int, or None when it is missing or not a number.

    `bool` is excluded on purpose: `True` is an `int` in Python and would
    otherwise render as 1970.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    moment = int(value)
    if not 0 <= moment <= 4102444800:  # 1970 .. 2100
        # The log is append-only and read whole on every tick, so one
        # implausible value would otherwise freeze the observation column
        # forever the first time a vendor sends milliseconds. A row loses a
        # clause; the export keeps working.
        return None
    return moment


def iso_utc(value: Any) -> str | None:
    """An epoch as ISO-8601 Z — the machine timestamp consumers sort on.

    This is not a second clock: every HUMAN-readable time below comes from
    scripts/timefmt, which owns the one Pacific clock. Splitting those two jobs
    is what stopped one reset reading as two different moments on the site and
    in the email.
    """
    moment = _epoch(value)
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pacific(value: Any) -> str | None:
    moment = _epoch(value)
    return None if moment is None else format_epoch_pacific(moment)


def window_minutes_of(event: dict[str, Any]) -> int | None:
    """The window length a record is about, from the field or from the slot.

    `credit_granted` records carry no `window_minutes`, but every record
    carries the slot key `slot_key()` built, which ends in that number.
    """
    minutes = event.get("window_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool):
        return minutes
    slot = event.get("slot")
    if isinstance(slot, str) and "/" in slot:
        tail = slot.rsplit("/", 1)[1]
        if tail.isdigit():
            return int(tail)
    return None


def window_name(minutes: int | None) -> str:
    """`weekly` / `5-hour`, or an honest rendering of anything else.

    An unrecognised duration is described from its own number rather than
    guessed at, so a sentence can still be written about a window this code has
    never seen without naming it wrongly.
    """
    if minutes is None:
        return "quota"
    if minutes in WINDOW_NAMES:
        return WINDOW_NAMES[minutes]
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def early_by_hours(seconds: Any) -> float | None:
    moment = _epoch(seconds)
    return None if moment is None or moment <= 0 else round(moment / 3600, 1)


def describe_early(seconds: Any) -> str | None:
    """`14.2 hours` / `5.9 days` — the same gap in the unit a reader can hold.

    Past two days an hour count stops meaning anything: "140.6 hours early" is
    read as a big number, "5.9 days early" is read as almost a whole window.
    """
    moment = _epoch(seconds)
    if moment is None or moment <= 0:
        # "cleared -24.0 hours early" is a confident sentence about something
        # that did not happen. Fall through to the unmeasured wording instead.
        return None
    hours = moment / 3600
    return f"{hours:.1f} hours" if hours < 48 else f"{hours / 24:.1f} days"


def _percent(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "an unrecorded level"
    return f"{value:g}%"


def observation_sentences(event: dict[str, Any], verdict: str | None) -> tuple[str, str]:
    """The headline and the evidence line, built from fields and nothing else.

    Every clause here prints a value that is on the record, so a sentence can
    never outrun the evidence behind it. The wording rules this obeys: a
    natural expiry is a fact about OUR window and never about the vendor;
    `vendor_reset` is the strongest claim available and is still bounded to
    "nothing this account did explains it"; and the phrase "no reset happened"
    is never written, because one account on one plan tier cannot say that.
    """
    window = f"{PRODUCT} {window_name(window_minutes_of(event))}"
    change = f"{_percent(event.get('used_before'))} to {_percent(event.get('used_after'))}"
    lo, hi = pacific(event.get("clear_bracket_lo")), pacific(event.get("clear_bracket_hi"))
    hi = hi or pacific(event.get("detected_at"))
    anchor = pacific(event.get("prior_active_resets_at")) or "a time we never recorded"
    early = describe_early(event.get("early_by_seconds")) or "an unmeasured amount"
    hours = early_by_hours(event.get("early_by_seconds"))
    before_credits = event.get("credits_before")
    after_credits = event.get("credits_after")

    if lo and hi and lo != hi:
        moment = f"between {lo} and {hi}"
    elif hi:
        moment = f"by {hi}"
    else:
        moment = "at a time we never recorded"

    if event.get("kind") == EVENT_RETRACTION:
        headline = f"A clear we recorded on our {window} window did not hold; we withdrew it."
        returned = event.get("used_at_revert")
        when = pacific(event.get("reverted_at"))
        if isinstance(returned, (int, float)) and not isinstance(returned, bool) and when:
            # Which signal withdrew it decides the sentence: on a re-anchoring
            # contract the original expiry time coming back IS the retraction;
            # on a fixed anchor only a snap-back to the pre-clear level can be.
            if same_anchor(
                _epoch(event.get("prior_active_resets_at")),
                _epoch(event.get("resets_at_at_revert")),
            ):
                because = f"on the expiry time in force before the clear ({anchor})"
            elif returned >= float(event.get("used_before") or 0) - SNAPBACK_TOLERANCE:
                because = (
                    f"back at the {_percent(event.get('used_before'))} it held before "
                    "the clear"
                )
            else:
                because = "on a window we had already recorded as cleared"
            return (
                headline,
                f"Usage returned to {_percent(returned)} at {when}, {because}, so the "
                "window had not actually reset.",
            )
        # The two 2026-09-02 records predate `used_at_revert` and
        # `resets_at_at_revert`. Saying which signal withdrew the clear would
        # be inventing it, and the retraction of a clear is exactly where an
        # invented detail does the most damage.
        return (
            headline,
            "This record predates the fields that say what usage and which expiry time "
            "the detector saw when it withdrew the clear, so nothing more is claimed "
            "about it.",
        )

    if verdict == VERDICT_CREDIT_GRANTED:
        return (
            "A banked reset credit arrived in this account's bank.",
            f"The banked credit count went from {before_credits} to {after_credits} at "
            f"{pacific(event.get('t')) or 'a time we never recorded'}. Our {window} "
            "window did not move, so this is a credit for the account holder to spend, "
            "not a reset.",
        )

    if verdict == VERDICT_LIMIT_CHANGE:
        return (
            f"The size of our {window} limit changed.",
            f"The counter went {change} {moment} with the expiry time and the banked "
            "credit count untouched, which is the limit itself being rescaled rather "
            "than the window clearing.",
        )

    if verdict == VERDICT_NATURAL_EXPIRY:
        return (
            f"Our {window} window reached its scheduled expiry.",
            f"{change} {moment}. The expiry time already on record was {anchor}, so this "
            "is our own window reaching the end it was scheduled for, not an extra reset.",
        )

    # An early clear always has a measured gap; the fallback exists so that a
    # record missing it degrades to a shorter true sentence, not to "None hours".
    gap = f"before its scheduled expiry of {anchor}"
    if hours is not None:
        # The clear is bracketed between two polls, so the true gap is at least
        # this: measured from the later end, which is the conservative one.
        gap = f"at least {hours} hours {gap}"

    if verdict == VERDICT_VENDOR_RESET:
        read_on_both_sides = (
            isinstance(event.get("credits_before"), int)
            and isinstance(event.get("credits_after"), int)
            and not event.get("credits_carried")
        )
        if read_on_both_sides:
            bank = (
                f"the banked credit count was read on both sides "
                f"({before_credits} then {after_credits}) and did not change"
            )
        else:
            # is_public() withholds this row for exactly this reason; the
            # sentence must not claim the reading that was never made.
            bank = (
                "the banked credit count could not be read across the clear, so "
                "this account's own credit cannot be ruled out"
            )
        return (
            f"Our {window} window cleared {early} early, and nothing this account did "
            "explains it.",
            f"{change} {moment}, {gap}; {bank}.",
        )

    if verdict == VERDICT_SELF_APPLIED:
        return (
            f"Our {window} window cleared {early} early because this account spent a "
            "banked reset credit.",
            f"{change} {moment}, {gap}; the banked credit count fell from "
            f"{before_credits} to {after_credits} across the clear, so this account's "
            "own credit explains it.",
        )

    if verdict == VERDICT_UNRESOLVED:
        return (
            f"Our {window} window cleared, but we cannot say whether it was a reset.",
            f"{change} {moment}. No scheduled-expiry time was on record for this window, "
            "so a natural expiry cannot be ruled out.",
        )

    # No verdict: a record this exporter does not recognise. Say that, rather
    # than reaching for the nearest verdict — every one of them is a claim.
    return (
        f"Our {window} window recorded something this exporter cannot classify.",
        "The probe wrote a record in a shape this export does not recognise, so nothing "
        "is claimed about it.",
    )


def observation(event: dict[str, Any], retracted_ids: set[str]) -> dict[str, Any]:
    """One event record in the shared contract's shape.

    The raw record is carried underneath: it is the audit trail for every
    sentence above, and build.py copies only the whitelisted contract keys onto
    the site, so an internal field can never leak by being present here.
    """
    kind = event.get("kind")
    if kind == EVENT_RETRACTION:
        # A withdrawn clear must not carry a publishable verdict string at all.
        # `public` is already False for it; this is the second lock, so that a
        # consumer reading verdicts alone still cannot publish one.
        verdict: str | None = None
        observed_at = event.get("reverted_at")
    elif kind == EVENT_CREDIT_GRANTED:
        verdict, observed_at = VERDICT_CREDIT_GRANTED, event.get("t")
    elif kind == EVENT_LIMIT_CHANGE:
        verdict = VERDICT_LIMIT_CHANGE
        observed_at = event.get("clear_bracket_hi") or event.get("detected_at")
    else:
        # A record whose classification this exporter does not know gets no
        # verdict rather than the nearest one: every verdict is a claim.
        classification = event.get("classification")
        verdict = (
            VERDICT_OF_CLASS.get(classification) if isinstance(classification, str) else None
        )
        observed_at = event.get("clear_bracket_hi") or event.get("detected_at")

    headline, evidence = observation_sentences(event, verdict)
    row = {
        **event,
        "observed_at": iso_utc(observed_at),
        "window": window_name(window_minutes_of(event)),
        "verdict": verdict,
        "public": is_public(event, retracted_ids),
        "headline": headline,
        "evidence": evidence,
        # The bracket the clear happened inside: one poll interval for a
        # consecutive-poll detection, the whole blind gap for an anchor jump.
        # A credit grant has no bracket on record and states none rather than
        # collapsing to a zero-width one it never measured.
        "observed_before": iso_utc(event.get("clear_bracket_lo")),
        "observed_after": iso_utc(event.get("clear_bracket_hi")),
        "early_by_hours": early_by_hours(event.get("early_by_seconds")),
    }
    if kind == EVENT_CLEAR and event.get("event_id") in retracted_ids:
        row["retracted"] = True
    return row


# ─── Coverage: what this probe can and cannot see ────────────────────────────


def slot_history(path: Path) -> tuple[dict[str, float], int | None, dict[str, int]]:
    """(peak used_percent per slot, oldest sample time, samples per slot).

    The sample count matters as much as the peak. An idle window writes no row
    at all — changed_rows() only records movement — so 141 hours of wall clock
    can hold 58 samples, and "has read 0% since Aug 30" would imply a
    continuity the log does not have.

    The coverage sentence is a public claim about the limits of our evidence,
    so it is measured rather than written down: "the 5-hour window has never
    carried usage" stops being true the first day it does, and a hard-coded
    sentence would go on saying it.
    """
    peaks: dict[str, float] = {}
    counts: dict[str, int] = {}
    first: int | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                used, minutes = row.get("used_percent"), row.get("window_minutes")
                if not isinstance(minutes, int) or not isinstance(used, (int, float)):
                    continue
                key = f"{row.get('limit_id')}/{minutes}"
                peaks[key] = max(peaks.get(key, 0.0), float(used))
                counts[key] = counts.get(key, 0) + 1
                moment = _epoch(row.get("t"))
                if moment is not None and (first is None or moment < first):
                    first = moment
    except OSError:
        return {}, None, {}
    return peaks, first, counts


def plan_type_from_cursor() -> str | None:
    """The plan the probed account is on, as the RPC reported it."""
    for state in Detector(load_cursor()).slots.values():
        last = state.get("last")
        plan = last.get("plan_type") if isinstance(last, dict) else None
        if isinstance(plan, str) and plan:
            return plan
    return None


def _windows(names: list[str]) -> tuple[str, str, str]:
    """(joined names, "window"/"windows", "has"/"have")."""
    joined = " and ".join(names)
    return (joined, "window", "has") if len(names) == 1 else (joined, "windows", "have")


def coverage_sentence(
    peaks: dict[str, float],
    first_sample_t: int | None,
    plan_type: str | None,
    counts: dict[str, int] | None = None,
) -> str:
    """One honest sentence naming the population this probe speaks for.

    Never "we saw no reset": what this says is which windows carry enough usage
    that a reset would have been visible at all, and which have never carried
    any, so a reader can tell silence from evidence.
    """
    account = f"One {PRODUCT} account"
    if plan_type:
        account = f"One {PRODUCT} {plan_type.capitalize()} account"
    if not peaks:
        return f"{account}. No samples are on record on this host, so nothing can be observed."

    counts = counts or {}
    by_window: dict[str, float] = {}
    by_window_samples: dict[str, int] = {}
    for key, peak in peaks.items():
        name = window_name(window_minutes_of({"slot": key}))
        by_window[name] = max(by_window.get(name, 0.0), peak)
        by_window_samples[name] = by_window_samples.get(name, 0) + counts.get(key, 0)
    since = f" since {format_epoch_pacific(first_sample_t)}" if first_sample_t else ""

    active = sorted(name for name, peak in by_window.items() if peak >= CLEAR_DROP_MIN)
    quiet = sorted(name for name, peak in by_window.items() if peak < CLEAR_DROP_MIN)
    if not active:
        return (
            f"{account}. No window has carried more than {max(by_window.values()):g}% of "
            f"its limit{since}, under the {CLEAR_DROP_MIN:g}-point drop a clear needs, so "
            "nothing can be observed yet."
        )

    joined, noun, _ = _windows(active)
    head = f"{account}, {joined} {noun} only."
    if not quiet:
        return head
    joined, noun, verb = _windows(quiet)
    peak = max(by_window[name] for name in quiet)
    samples = sum(by_window_samples.get(name, 0) for name in quiet)
    # "since <date>" alone would imply continuous watching. An idle window
    # writes no sample at all, so state how many readings the claim rests on.
    seen = f" on all {samples} readings taken{since}" if samples else since
    if peak <= 0:
        tail = f"The {joined} {noun} {verb} read 0%{seen}"
    else:
        tail = (
            f"The {joined} {noun} {verb} never read above {peak:g}%{seen}, under the "
            f"{CLEAR_DROP_MIN:g}-point drop a clear needs"
        )
    return f"{head} {tail}, so nothing can be observed on it."


def probe_status(health: dict[str, Any], now: int) -> str:
    """`ok` / `blind` / `absent`, read off the health file the probe writes.

    `absent` is "no probe has ever written here" — a worktree, a build host, a
    machine where the unit was never installed. `blind` is "a probe exists and
    is not seeing anything": a failure streak, a recorded outage, a block that
    stopped being updated, or one that has never had a successful poll. Only a
    fresh block with a success behind it is `ok`, because every other state has
    to be able to stop the site claiming the counter was verified.
    """
    updated_at = _epoch(health.get("updated_at"))
    if updated_at is None:
        return PROBE_ABSENT
    if now - updated_at > HEALTH_CARRY_SECONDS:
        return PROBE_BLIND
    if health.get("blind_since") is not None:
        return PROBE_BLIND
    if _epoch(health.get("consecutive_failures")) not in (0, None):
        return PROBE_BLIND
    return PROBE_OK if _epoch(health.get("last_ok_at")) is not None else PROBE_BLIND


def collapse_key(event: dict[str, Any]) -> str:
    """Identity for "this is the same thing I already exported".

    One vendor action can arrive as several records. A banked credit grant is
    reported on EVERY limit window, so the two real grants in this account's
    history are six records; they collapse on the moment they landed.

    Everything else keeps its kind in the key, because a retraction carries the
    SAME event_id as the clear it withdraws — collapsing those would drop the
    retraction and leave the withdrawn clear standing on its own.
    """
    kind = event.get("kind")
    moment = _epoch(event.get("detected_at") or event.get("t"))
    if kind == EVENT_CREDIT_GRANTED:
        return f"{kind}:{moment}"
    identity = event.get("event_id") or f"{event.get('slot')}:{moment}"
    return f"{kind}:{identity}"


def export_observations(
    path: Path, label: str = PROBE_LABEL, now: int | None = None
) -> int:
    """Write the probe's observations for the site and notifier to consume.

    On-disk state only: no RPC, no network, nothing that can hang. The file
    lands in the gitignored data/observed/ directory, never beside the tracked
    data/<vendor>.json seeds, which build.py would clobber.

    This is the export boundary. The internal CLASS_* names stop here and the
    published vocabulary starts, and each observation carries the two sentences
    that say, in plain words, which of the four things happened: the window
    reached its own scheduled expiry, this account spent its own banked credit,
    the window cleared early and nothing this account did explains it, or there
    was no expiry time on record and we cannot tell.
    """
    now = int(time.time()) if now is None else now
    events = read_events(EVENTS_FILE)
    retracted = {
        event["event_id"]
        for event in events
        if event.get("kind") == EVENT_RETRACTION and event.get("event_id")
    }
    # One vendor action can appear as several records: a banked credit grant is
    # reported on every limit window, so the two real grants in this account's
    # history are six rows. Collapse by identity before anything reads them as
    # separate events, keeping the first occurrence (the weekly slot is written
    # first, and it is the window everything else is stated about).
    seen: set[str] = set()
    observations = []
    for event in events:
        key = collapse_key(event)
        if key in seen:
            continue
        seen.add(key)
        observations.append(observation(event, retracted))

    health = health_for_export(label)
    peaks, first_sample_t, sample_counts = slot_history(SAMPLES_FILE)
    payload = {
        "vendor": EXPORT_VENDOR,
        "exported_at": now,
        "probe": {
            "status": probe_status(health, now),
            "last_verified_at": iso_utc(health.get("last_ok_at")),
            "detectable_now": health.get("detectable_now") or {},
            "coverage": coverage_sentence(
                peaks, first_sample_t, plan_type_from_cursor(), sample_counts
            ),
            # Outside the shared contract and not copied onto the site by
            # build.py's whitelist. Kept because this file is also what the
            # owner reads when the probe itself is the thing that is wrong.
            "label": health.get("label", label),
            "last_error": health.get("last_error"),
        },
        "observations": observations,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    public = sum(1 for o in observations if o["public"])
    log(f"exported {len(observations)} observations ({public} public) to {path}")
    return 0


# ─── Runner ──────────────────────────────────────────────────────────────────


def log(message: str) -> None:
    print(message, flush=True)


def poll_once(
    detector: Detector,
    now: int | None = None,
    extra_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    rows = normalise(read_rate_limits(), now)
    to_write = changed_rows(rows, detector)

    events: list[dict[str, Any]] = []
    for row in rows:
        events.extend(detector.observe(row))

    append_jsonl(SAMPLES_FILE, to_write)
    append_jsonl(EVENTS_FILE, events)
    save_cursor({**detector.cursor(), **(extra_cursor or {})})

    for event in events:
        if event.get("kind") == EVENT_RETRACTION:
            log(
                f"RETRACTED {event['event_id']} "
                f"{event['used_before']:.0f}%->{event['used_after']:.0f}% "
                f"reverted at {event.get('reverted_at')} "
                f"(usage {event.get('used_at_revert')}% on anchor "
                f"{event.get('resets_at_at_revert')}) — not a reset"
            )
            continue
        if event.get("kind") == EVENT_CREDIT_GRANTED:
            log(
                f"CREDIT GRANTED {event['slot']} "
                f"{event['credits_before']} -> {event['credits_after']} "
                f"at {event['t']} — an observation, not a reset"
            )
            continue
        log(
            f"DETECTED [{event['classification']}] {event['slot']} "
            f"{event['used_before']:.0f}%->{event['used_after']:.0f}% "
            f"bracket [{event['clear_bracket_lo']},{event['clear_bracket_hi']}] "
            f"— {event['reason']}"
        )

    return {
        "t": now,
        "sampled": len(rows),
        "written": len(to_write),
        "events": len(events),
    }


def run(once: bool, with_upstream: bool) -> int:
    cursor = load_cursor()
    detector = Detector(cursor)
    upstream_sigs: dict[str, str] = cursor.get("upstream_sigs", {})
    last_upstream = 0.0
    failures = 0
    ok = False
    last_ok_at, blind_since = resume_health()
    last_error: str | None = None

    while True:
        started = time.time()
        try:
            summary = poll_once(detector, extra_cursor={"upstream_sigs": upstream_sigs})
            failures = 0
            ok = True
            last_ok_at = summary["t"]
            blind_since = None
            last_error = None
            log(
                f"poll ok sampled={summary['sampled']} "
                f"written={summary['written']} events={summary['events']}"
            )
        except ProbeError as exc:
            failures += 1
            ok = False
            last_error = str(exc)[:200]
            if blind_since is None:
                blind_since = int(started)
            # A blind detector is the failure mode that matters: the pipeline
            # would look healthy while silently detecting nothing. Surface it
            # loudly so the unit's OnFailure/journal alerting can catch it.
            log(f"PROBE FAILED ({failures} in a row): {exc}")
            if failures % BLIND_LOG_EVERY == 0:
                log(
                    f"PROBE BLIND: {failures} consecutive failures "
                    "— check `codex login`"
                )

        # Every iteration, success or failure: this file IS the heartbeat.
        write_health(
            health_block(
                detector,
                int(time.time()),
                last_ok_at=last_ok_at,
                consecutive_failures=failures,
                blind_since=blind_since,
                last_error=last_error,
            )
        )

        if failures >= BLIND_EXIT_FAILURES:
            # Staying up while blind is what let the 2026-09-03 outage run 35
            # polls unnoticed. Exiting hands the problem to systemd, whose
            # Restart=always/RestartSec=10 re-tries with a clean process and
            # whose OnFailure= can mail the owner.
            log(
                f"PROBE EXITING: {failures} consecutive failures "
                f"since {blind_since} — restarting under systemd"
            )
            sys.exit(2)

        if with_upstream and started - last_upstream >= UPSTREAM_EVERY_SECONDS:
            fresh = dedupe_upstream(snapshot_upstream(int(started)), upstream_sigs)
            if append_jsonl(UPSTREAM_FILE, fresh):
                save_cursor({**detector.cursor(), "upstream_sigs": upstream_sigs})
            last_upstream = started

        if once:
            # Exit non-zero on a failed poll so a smoke test cannot pass on a
            # probe that never actually read anything.
            return 0 if ok else 1
        time.sleep(max(1.0, POLL_SECONDS - (time.time() - started)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument(
        "--no-upstream",
        action="store_true",
        help="skip the corroborating-feed snapshots",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current snapshot as JSON and exit without recording",
    )
    parser.add_argument(
        "--export",
        metavar="PATH",
        help="write the observation export from on-disk state only (no RPC)",
    )
    args = parser.parse_args(argv)

    if args.export:
        try:
            return export_observations(Path(args.export))
        except ProbeError as exc:
            # publish.sh keeps the previous export on a non-zero exit, so an
            # unreadable state directory costs a stale observation column
            # rather than an empty one that reads as "no resets".
            log(f"EXPORT FAILED: {exc}")
            return 1

    if args.show:
        now = int(time.time())
        print(json.dumps(normalise(read_rate_limits(), now), indent=2))
        return 0

    return run(once=args.once, with_upstream=not args.no_upstream)


if __name__ == "__main__":
    sys.exit(main())
