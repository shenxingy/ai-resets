#!/usr/bin/env python3
"""What our own accounts actually show, in one sentence an email can carry.

The probe has been reading this account's real Codex quota every 60 seconds
since 2026-08-31, and nothing consumed it: the site never mentioned it and the
notifier never looked. So subscribers were told about announcements with no
statement of whether the thing had reached a real account — including a
forecast that had not landed, and including the 2026-08-30 reset that this
account SAW three minutes before the announcement was posted.

This module turns the probe's on-disk state into that statement, and refuses to
overstate it. Every branch below is a claim we can defend from a file:

  observed / not observed   the probe is healthy and the window is readable
  too empty to tell         our own usage is under the detection floor
  could not verify          the probe is blind, throttled, or its state is gone
  not observable            we run no probe for that vendor yet

"Not observed" is never rendered as "no reset happened". A single account on a
single plan tier cannot say that, and saying it is how a tracker loses trust.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.timefmt import describe_age, format_epoch_pacific

DEFAULT_STATE_DIR = Path("/var/lib/ai-resets")

HEALTH_FILENAME = "probe_health.json"
CURSOR_FILENAME = "quota_cursor.json"
EVENTS_FILENAME = "quota_events.jsonl"

# A probe that has not written its health file this recently is treated as
# blind. Two missed 60-second polls plus slack; the notifier runs every five
# minutes, so anything tighter would alert on ordinary scheduling jitter.
PROBE_STALE_SECONDS = 15 * 60

# Mirrors quota_probe.CLEAR_DROP_MIN. A window used less than this cannot show
# a clear at all, so "we did not see it" would be dishonest — we could not have.
# tests/test_groundtruth.py asserts the two constants stay equal.
DETECTION_FLOOR_PERCENT = 10.0

# The only slot that has ever carried real usage on this account. The 5-hour
# slot (a second limit id the account exposes) has read 0% for its entire
# history, so quoting it
# would say nothing.
CODEX_WEEKLY_SLOT = "codex/10080"

STATUS_OK = "ok"
STATUS_BLIND = "blind"
STATUS_ABSENT = "absent"
STATUS_THROTTLED = "throttled"
STATUS_TOKEN_STALE = "token_stale"
STATUS_UNPROBED = "unprobed"

# The public vocabulary for what an observed clear was. The probe computes the
# internal classification; these are the only words a reader ever sees.
VERDICT_NATURAL = "natural_expiry"
VERDICT_SELF = "self_applied"
VERDICT_VENDOR = "vendor_reset"
VERDICT_UNRESOLVED = "unresolved"
_INTERNAL_TO_VERDICT = {
    "natural_expiry": VERDICT_NATURAL,
    "self_applied_credit": VERDICT_SELF,
    "global_candidate": VERDICT_VENDOR,
    "unresolved": VERDICT_UNRESOLVED,
}

# How far from an announcement an observation may sit and still be the same
# event. Our own account saw the 2026-08-30 reset 2.5 minutes BEFORE the post,
# so the window has to be symmetric: a rollout reaching us first is the normal
# case, not an anomaly.
MATCH_WINDOW_SECONDS = 6 * 3600



# ─── Which vendors we actually measure ───────────────────────────────────────


@dataclass(frozen=True)
class ProbeSpec:
    """Where one vendor's ground truth lives on disk, and how to describe it.

    Keyed by vendor rather than hard-coded so adding the Claude probe is a data
    change here and nothing else: build.py already renders observations for any
    vendor that has them, and the email sentence below reads this table.
    """

    vendor: str
    labels: tuple[str, ...]
    events_filename: str
    account_phrase: str
    weekly_kinds: tuple[str, ...]
    script: str
    # Whether this probe records a usage percentage we can quote. Without one,
    # "not observed" is still sayable but "the window was too empty to tell" is
    # not, so the two must not share a branch.
    has_usage_reading: bool = False

    def exists(self, root: Path | None = None) -> bool:
        """Does this probe actually ship? A spec for a script that is not here
        would make the email claim a measurement nothing can produce."""
        base = root or Path(__file__).resolve().parent
        return (base / self.script).is_file()


PROBE_SPECS = {
    "openai": ProbeSpec(
        vendor="openai",
        labels=("codex",),
        events_filename="quota_events.jsonl",
        account_phrase="our Pro account",
        weekly_kinds=("codex/10080",),
        script="quota_probe.py",
        has_usage_reading=True,
    ),
    "anthropic": ProbeSpec(
        vendor="anthropic",
        # Two Max 20x accounts on one operator machine. Only the labels are ever
        # written or shown; the organisations behind them are not ours to name.
        labels=("claude:a", "claude:b"),
        events_filename="claude_events.jsonl",
        account_phrase="our Max 20x accounts",
        weekly_kinds=("weekly_all",),
        script="claude_probe.py",
    ),
}


def probed_vendors(
    state_dir: Path | str = DEFAULT_STATE_DIR, *, now: int | None = None
) -> tuple[str, ...]:
    """The vendors a probe is actually running for, read off the health file.

    Derived rather than declared, because the two states have to be told apart
    honestly and the transition happens without a code change. Before the probe
    exists the email says "we do not run one for this vendor"; the moment it
    starts writing health, the same email starts saying observed or not
    observed. A declared list would have to be edited at exactly the right
    moment, and would lie on either side of the mistake.
    """
    health = load_probe_health(state_dir)
    return tuple(
        vendor
        for vendor, spec in PROBE_SPECS.items()
        if spec.exists()
        and any(isinstance(health.get(label), dict) for label in spec.labels)
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def load_probe_health(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict[str, Any]:
    return _read_json(Path(state_dir) / HEALTH_FILENAME) or {}


def load_cursor(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict[str, Any]:
    return _read_json(Path(state_dir) / CURSOR_FILENAME) or {}


def clear_observations(
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    vendor: str = "openai",
) -> list[dict[str, Any]]:
    """Every window clear the probe recorded, with what it concluded and why.

    Read from the probe's own append-only event log rather than from the
    exported file, so the email keeps working when an export has not run.
    Retracted clears are dropped: a clear we have already withdrawn is not
    evidence of anything.
    """
    spec = PROBE_SPECS.get(vendor)
    path = Path(state_dir) / (spec.events_filename if spec else EVENTS_FILENAME)
    clears: dict[str, dict[str, Any]] = {}
    retracted: set[str] = set()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("kind")
                key = event.get("event_id") or f"{event.get('slot')}:{event.get('detected_at')}"
                if kind == "retraction":
                    retracted.add(key)
                elif kind in (None, "clear") and event.get("classification"):
                    # kind is absent on records written before it existed; a
                    # classification is what makes a record a clear.
                    clears[key] = event
    except OSError:
        return []
    out = []
    for key, event in clears.items():
        if key in retracted or not event.get("confirmed"):
            continue
        if event.get("concordance") == "accounts_disagree":
            # One account's weekly window cleared while another readable
            # account's did not. The Claude probe already withholds this from
            # its own export for that reason; reading the raw event log here
            # would walk straight around that gate and publish the
            # announcement as confirmed. A withhold only one consumer honours
            # is not a withhold.
            continue
        moment = event.get("detected_at")
        if not isinstance(moment, int):
            continue
        out.append(
            {
                "key": key,
                "t": moment,
                "verdict": _INTERNAL_TO_VERDICT.get(
                    str(event.get("classification")), VERDICT_UNRESOLVED
                ),
                "used_before": event.get("used_before"),
                "used_after": event.get("used_after"),
                "early_by_seconds": event.get("early_by_seconds"),
                "credits_before": event.get("credits_before"),
                "credits_after": event.get("credits_after"),
                "credits_carried": bool(event.get("credits_carried")),
            }
        )
    return sorted(out, key=lambda o: o["t"])


def observation_near(
    announced_at: int | None,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    window_seconds: int = MATCH_WINDOW_SECONDS,
    vendor: str = "openai",
) -> dict[str, Any] | None:
    """The clear closest to an announcement, if one sits inside the window.

    Only a vendor_reset is returned. A natural expiry says nothing about the
    vendor, and a self-applied clear says what the OWNER did with their own
    banked credit — publishing that as corroboration would both mislead and
    leak.
    """
    if announced_at is None:
        return None
    candidates = [
        o
        for o in clear_observations(state_dir, vendor=vendor)
        if o["verdict"] == VERDICT_VENDOR
        and abs(o["t"] - announced_at) <= window_seconds
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda o: abs(o["t"] - announced_at))


def credit_grant_since(
    since: int | None, state_dir: Path | str = DEFAULT_STATE_DIR
) -> dict[str, Any] | None:
    """The first banked-credit grant the probe recorded after `since`.

    A banked reset does not clear the window. It drops a credit into the bank
    for the account holder to spend, so the landing signal is the credit count
    rising, which the probe records as a credit_granted observation. Reading
    the window instead is how an email came to say "not landed" at 10:16 PM
    while carrying "2 banked credits" in the same sentence — the grant had
    landed at 9:20 PM, three hours and forty-one minutes after the post.

    The log is append-only and small (one line per event, not per sample), so
    it is read whole. Grants are emitted once per limit window, so the
    earliest matching record is the grant.
    """
    if since is None:
        return None
    path = Path(state_dir) / EVENTS_FILENAME
    best: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("kind") != "credit_granted":
                    continue
                moment = event.get("t")
                if not isinstance(moment, int) or moment < since:
                    continue
                if best is None or moment < best["t"]:
                    best = event
    except OSError:
        return None
    return best


def probe_status(
    health: dict[str, Any],
    label: str,
    now: int,
    *,
    stale_seconds: int = PROBE_STALE_SECONDS,
) -> dict[str, Any]:
    """Health of one probe, with the age that justifies the verdict.

    A missing file and a stale file are deliberately different states: the
    first means the probe has never run here, the second means it stopped.
    Neither may be read as "nothing happened".
    """
    entry = health.get(label)
    if not isinstance(entry, dict):
        return {"status": STATUS_ABSENT, "label": label, "age_seconds": None}

    updated_at = entry.get("updated_at")
    age = None if not isinstance(updated_at, (int, float)) else max(0, now - int(updated_at))
    result = {
        "status": STATUS_OK,
        "label": label,
        "age_seconds": age,
        "blind_since": entry.get("blind_since"),
        "consecutive_failures": entry.get("consecutive_failures") or 0,
        "last_ok_at": entry.get("last_ok_at"),
        "detectable_now": entry.get("detectable_now") or {},
    }

    throttled_until = entry.get("throttled_until")
    if age is None or age > stale_seconds:
        result["status"] = STATUS_BLIND
    elif entry.get("blind_since"):
        result["status"] = STATUS_BLIND
    elif isinstance(throttled_until, (int, float)) and int(throttled_until) > now:
        result["status"] = STATUS_THROTTLED
    elif entry.get("token_stale"):
        result["status"] = STATUS_TOKEN_STALE
    return result


def codex_weekly_reading(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict[str, Any] | None:
    """The newest weekly-window sample the probe recorded, or None."""
    slots = load_cursor(state_dir).get("slots")
    if not isinstance(slots, dict):
        return None
    slot = slots.get(CODEX_WEEKLY_SLOT)
    last = slot.get("last") if isinstance(slot, dict) else None
    if not isinstance(last, dict) or not isinstance(last.get("used_percent"), (int, float)):
        return None
    return {
        "used_percent": float(last["used_percent"]),
        "credits_available": last.get("credits_available"),
        "t": last.get("t"),
        "resets_at": last.get("resets_at"),
    }


def _credit_phrase(credits: Any) -> str:
    if not isinstance(credits, int):
        return ""
    return f", {credits} banked credit" + ("" if credits == 1 else "s")


def ground_truth_line(
    vendor: str,
    *,
    is_forecast: bool,
    now: int,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    probed_vendors: tuple[str, ...] | None = None,
    kind: str | None = None,
    announced_at: int | None = None,
) -> dict[str, Any]:
    """One sentence about our own accounts, plus the status that produced it.

    `kind` and `announced_at` matter for a forecast: what counts as "landed"
    depends on what was forecast. A banked reset lands as a credit in the
    bank; a plain reset lands as the window clearing.
    """
    # Three different states, and conflating any two of them produces a false
    # sentence: we run no probe for this vendor at all; we run one and it has
    # never reported; we run one and it is currently blind. Only the first is
    # "we do not measure this vendor".
    spec = PROBE_SPECS.get(vendor)
    if spec is not None and not spec.exists():
        spec = None  # declared but not shipped yet
    account = spec.account_phrase if spec else "our account"
    label = spec.labels[0] if spec else "codex"
    if probed_vendors is not None:
        known = vendor in probed_vendors
    else:
        known = spec is not None
    if not known or spec is None:
        return {
            "status": STATUS_UNPROBED,
            "line": (
                "We do not yet run a ground-truth probe for this vendor, so this "
                "update is the announcement only, not a confirmed observation."
            ),
        }

    health = load_probe_health(state_dir)
    status = probe_status(health, label, now)
    for other in spec.labels[1:]:
        # With more than one account, the probe is only healthy when they all
        # are: a blind half cannot be reported as a clean look.
        if probe_status(health, other, now)["status"] != STATUS_OK:
            status = probe_status(health, other, now)
            break
    if status["status"] in (STATUS_ABSENT, STATUS_BLIND):
        since = status.get("blind_since") or status.get("last_ok_at")
        when = (
            f" since {format_epoch_pacific(since)}"
            if isinstance(since, (int, float))
            else ""
        )
        return {
            "status": status["status"],
            "line": (
                f"Could not verify on {account}: "
                + (
                    f"the probe has been offline{when}."
                    if when
                    else "no probe reading is on record."
                )
            ),
        }

    # The observation is checked FIRST, before any usage reading. It is the
    # strongest evidence there is and it does not need a usage percentage to
    # be true. A probe with no percentage to quote — the Claude one — was
    # otherwise reporting "not observed" for resets it had just measured,
    # because the reading branch returned before this was ever consulted.
    observed = observation_near(announced_at, state_dir, vendor=vendor)
    if observed is not None:
        # The question this whole tracker exists to answer: did the thing
        # actually reach a real account, and was it a reset rather than the
        # window expiring on its own schedule?
        lead = announced_at - observed["t"] if isinstance(announced_at, int) else None
        timing = ""
        if isinstance(lead, int) and abs(lead) >= 60:
            timing = (
                f", {describe_age(abs(lead))} {'before' if lead > 0 else 'after'} this post"
            )
        early = observed.get("early_by_seconds")
        early_phrase = (
            f" {describe_age(early)} before its scheduled expiry"
            if isinstance(early, (int, float)) and early > 0
            else ""
        )
        return {
            "status": STATUS_OK,
            "observed": True,
            "verdict": VERDICT_VENDOR,
            "line": (
                f"Observed on {account} at "
                f"{format_epoch_pacific(observed['t'])}{timing}: the weekly "
                f"window went {observed['used_before']:.0f}% to "
                f"{observed['used_after']:.0f}%{early_phrase}, with the banked "
                "credit count read on both sides and unchanged. Nothing this "
                "account did explains it."
            ),
        }

    reading = codex_weekly_reading(state_dir) if spec.has_usage_reading else None
    if reading is None and spec.has_usage_reading:
        return {
            "status": STATUS_ABSENT,
            "line": (
                f"Could not verify on {account}: no weekly-window reading is "
                "on record."
            ),
        }
    if reading is None:
        # A probe with no usage reading to quote can still say the honest
        # thing: it was watching and it saw nothing.
        verified = status.get("last_ok_at")
        seen = (
            f" as of {format_epoch_pacific(verified)}"
            if isinstance(verified, (int, float))
            else ""
        )
        return {
            "status": STATUS_OK,
            "line": (
                f"Not observed on {account}{seen}. One operator's accounts on one "
                "plan tier cannot rule a reset out."
            ),
        }

    used = reading["used_percent"]
    when = (
        format_epoch_pacific(reading["t"])
        if isinstance(reading["t"], (int, float))
        else "the last reading"
    )
    credits = _credit_phrase(reading["credits_available"])

    if is_forecast:
        if kind == "banked":
            grant = credit_grant_since(announced_at, state_dir)
            if grant is not None:
                delay = (
                    f", {describe_age(grant['t'] - announced_at)} after the post"
                    if isinstance(announced_at, int)
                    else ""
                )
                after = grant.get("credits_after")
                bank = _credit_phrase(after).lstrip(", ") or "the bank rose"
                return {
                    "status": STATUS_OK,
                    "landed": True,
                    "line": (
                        f"Landed on {account} at "
                        f"{format_epoch_pacific(grant['t'])}{delay}: {bank}. "
                        "A banked reset is a credit to spend, so the weekly "
                        f"window still reads {used:.0f}% used. This match is by "
                        "timing alone: a standing daily grant would look the same."
                    ),
                }
            return {
                "status": STATUS_OK,
                "line": (
                    f"Not landed on {account} as of {when}: the bank still "
                    f"holds{credits or ' no banked credit'} and the weekly window "
                    f"reads {used:.0f}% used. We will not send a second email "
                    "unless it lands."
                ),
            }
        return {
            "status": STATUS_OK,
            "line": (
                f"Not landed on {account} as of {when}: the weekly window "
                f"reads {used:.0f}% used{credits}. We will not send a second "
                "email unless it lands."
            ),
        }
    if used < DETECTION_FLOOR_PERCENT:
        return {
            "status": STATUS_OK,
            "line": (
                f"Our own weekly window was only {used:.0f}% used at {when}, too "
                "empty for a reset to be visible on it. This is not evidence "
                "either way."
            ),
        }
    return {
        "status": STATUS_OK,
        "line": (
            f"Not observed on {account} as of {when}: the weekly window "
            f"still reads {used:.0f}% used{credits}. One account on one plan "
            "cannot rule a reset out."
        ),
    }


def stale_age_line(status: dict[str, Any]) -> str:
    age = status.get("age_seconds")
    if not isinstance(age, (int, float)):
        return "never"
    return describe_age(age)
