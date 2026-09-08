#!/usr/bin/env python3
"""P6: own-account quota probe for Google, through the Antigravity CLI.

Status on this host, 2026-09-07: INSTALLED, NOT SIGNED IN. `agy` 1.1.27 is at
`~/.local/bin/agy` and `~/.gemini/antigravity-cli/` exists, so the install half
is done. The sign-in half cannot be done from here: `agy` opens a Google OAuth
consent page and waits for an authorization code, and the code is bound to a
per-invocation PKCE challenge, so it has to be pasted back into the same live
process. One interactive login by the owner is the whole remaining step; the
quota parsing below still runs against fixtures until then.

Why Google needs its own probe at all
-------------------------------------
`data/google.json` carries a note saying Google has "no recurring reset
mechanism". That is wrong: the plan records forced Google resets on 2026-05-21
(twice), 05-23, 06-02 and 07-21. Gemini CLI stopped serving consumer accounts on
2026-06-18 and the product is now Antigravity CLI, so the probe has to speak to
`agy`.

What is verified and what is not
--------------------------------
Measured against agy 1.1.27 on this host, 2026-09-07:
  * `-p "/usage" --output-format json` is accepted — `--print`, `--output-format`
    and `--json-schema` are all real flags, so the non-interactive form is right,
  * `agy --help` lists its subcommands as agent, agents, changelog and help —
    no login and no auth among them, confirming the documented absence,
  * an unsigned-in run prints the consent URL and "Or, paste the authorization
    code here and press Enter" to STDERR, waits 60 s, then exits 1,
  * it simultaneously writes a well-formed error envelope to STDOUT:
    `{"conversation_id":"","status":"ERROR","response":"","error":
    "authentication failed or timed out","duration_seconds":0,...}`.

That last one is why `read_usage` checks the envelope as well as the exit code:
the failure arrives as valid JSON, so the JSON guard alone would pass it down to
a parser that would blame the payload shape instead of the missing login.

Verified from https://antigravity.google/docs/cli/install/ on 2026-09-06:
  * the install command is `curl -fsSL https://antigravity.google/cli/install.sh | bash`
    (a Go binary, not an npm package),
  * `/usage` exists as a slash command ("Model Quotas").

STILL not confirmed — these need the first authenticated response:
  * that `/usage` answers without spending quota,
  * the bucket payload of
    `POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota`:
    `{buckets: [{modelId, tokenType: "REQUESTS", remainingFraction,
    remainingAmount?, resetTime}]}`.

So the parser is written defensively: it locates `buckets` through a small
envelope search, accepts three encodings of `resetTime`, and skips a bucket it
cannot read rather than inventing a number. The FIRST authenticated response
must be saved to `tests/fixtures/antigravity-usage.json` and this parser
re-checked against it; until then every shape assumption here is a test, not a
measurement.

The window duration is inferred, not reported
---------------------------------------------
`quota_probe.slot_key` is `f"{limit_id}/{window_minutes}"`, and the bucket
payload carries no window duration — only a `resetTime`. Time-to-reset shrinks
across a window, so snapping it to a ladder on every sample would rename the
slot as the window drains. `infer_window_minutes` therefore snaps UP to the
ladder and never narrows a slot it has already widened: a 5-hour bucket can
never observe more than 5 hours of remaining time, and a 7-day bucket widens to
10080 the first time it is sampled early in its own window. Rows carry
`window_inferred` so no reader mistakes the value for something the vendor said.

No credit bank
--------------
(One consequence for the lead: `Detector._track_credits` logs a single "CREDIT
FIELD ABSENT" line per slot when the field never appears. For Codex that line
means the field flapped and is worth reading; for Antigravity it is the normal,
permanent state. Harmless — one line per slot per process — but it belongs in
the same pass that collapses the per-slot credit_granted duplicates.)

Antigravity has nothing like Codex's `rateLimitResetCredits`, so every row
carries `credits_available: None` — not 0, which `classify()` would read as "the
owner just spent a credit". With no bank on either side of a clear,
`credit_spent` is False and an early clear classifies as `global_candidate`,
which is the correct reading for a vendor with no self-service reset.

Poll cadence
------------
`quota_probe.NATURAL_GRACE_SECONDS` is `2 * POLL_SECONDS + 60`, derived from the
Codex probe's 60 s cadence. A probe that polls more slowly than that grace
allows would bracket an ordinary scheduled expiry wider than the grace and
classify it `global_candidate` — a false vendor-reset candidate on every single
window rollover. `check_cadence` refuses to start rather than produce that, and
names the environment variable that fixes it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import quota_probe  # noqa: E402
from scripts.timefmt import parse_timestamp  # noqa: E402

# ─── Contract ────────────────────────────────────────────────────────────────

PROBE_LABEL = "antigravity"
VENDOR = "google"

AGY_BINARY = os.environ.get("AI_RESETS_AGY", "agy")
USAGE_ARGS = ("-p", "/usage", "--output-format", "json")
QUOTA_ENDPOINT = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"

STATE_DIR = Path(os.environ.get("AI_RESETS_STATE", "/var/lib/ai-resets"))
CURSOR_FILE = STATE_DIR / "antigravity_cursor.json"

POLL_SECONDS = int(os.environ.get("AI_RESETS_ANTIGRAVITY_POLL_SECONDS", "300"))
MAX_CONSECUTIVE_FAILURES = 10
# Measured on this host, 2026-09-07: an unauthenticated `agy` prints its sign-in
# URL and then waits SIXTY seconds for a pasted authorization code before giving
# up with exit 1 and a usable stderr line. A 60 s subprocess timeout races that
# exactly, and the race is the bad outcome: TimeoutExpired throws the CLI's own
# words away and reports a hang instead of "nobody has signed this host in".
# The budget must sit above the CLI's internal wait, not on top of it.
AGY_INTERNAL_AUTH_WAIT_SECONDS = 60
CALL_TIMEOUT_SECONDS = AGY_INTERNAL_AUTH_WAIT_SECONDS + 30

# 5 hours, 1 day, 7 days. Every quota window this project has met on any vendor
# is one of these; a bucket whose remaining time exceeds the last entry is
# reported with the last entry and `window_inferred` set, never silently.
WINDOW_LADDER = (300, 1440, 10080)

# ONE line, and it has to be actionable on its own: this is what a 3am journal
# reader gets, and there is no second line explaining it.
UNAVAILABLE = (
    "antigravity probe unavailable: install with "
    "`curl -fsSL https://antigravity.google/cli/install.sh | bash`, then run `agy` "
    "once in an interactive terminal and finish the Google sign-in "
    "(over SSH it prints a URL plus a one-time code to paste back) — "
    "the docs define no login subcommand, so no unattended path exists"
)


# ─── Reading the CLI ─────────────────────────────────────────────────────────


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def unavailable(detail: str = "") -> quota_probe.ProbeError:
    """The one actionable line, optionally carrying the CLI's own words."""
    return quota_probe.ProbeError(f"{UNAVAILABLE}{f' (agy said: {detail})' if detail else ''}")


def read_usage(
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    binary: str = AGY_BINARY,
    timeout: int = CALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """`agy -p "/usage" --output-format json`, or the actionable line.

    Every failure mode collapses to the same message on purpose. "command not
    found", "exit 1 with a sign-in prompt" and "printed an OAuth URL where JSON
    was expected" are three spellings of one situation — nobody has logged this
    host in — and the fix is identical for all three.
    """
    # Resolved here rather than as default arguments: a default is bound at
    # import, so `mock.patch("shutil.which", ...)` would silently miss it.
    runner = runner or subprocess.run
    which = which or shutil.which
    if which(binary) is None:
        raise unavailable(f"{binary} is not on PATH")
    try:
        completed = runner(
            [binary, *USAGE_ARGS],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # An unauthenticated `agy` waits at an interactive prompt forever; the
        # timeout is what turns that hang into a log line and a retry.
        raise unavailable(f"no answer in {timeout}s (interactive sign-in prompt?)") from None
    except OSError as exc:
        raise unavailable(str(exc)) from None

    if completed.returncode != 0:
        raise unavailable(_first_line(completed.stderr) or f"exit {completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise unavailable(_first_line(completed.stdout) or "no JSON on stdout") from None
    if not isinstance(payload, dict):
        raise unavailable(f"expected a JSON object, got {type(payload).__name__}")
    if envelope_error(payload):
        # `agy` answers a failed sign-in with a WELL-FORMED JSON object on
        # stdout — `{"status":"ERROR","error":"authentication failed or timed
        # out",...}` — measured on this host on 2026-09-07. It parses, so the
        # JSON guard above lets it through, and today only the non-zero exit
        # code stops it. That is one thin thread: an envelope like this reaching
        # extract_buckets would be reported as "no `buckets` list in the /usage
        # payload ... record it as a fixture and re-check the parser", which
        # sends the reader to the parser when the fix is to log in.
        raise unavailable(envelope_error(payload))
    return payload


def envelope_error(payload: dict[str, Any]) -> str:
    """The CLI's own error text if this envelope declares a failure, else "".

    Read from the envelope rather than the exit code because the two are
    independent: the fields are what the CLI says happened, and the exit code is
    a separate promise that a future version may keep differently.
    """
    error = payload.get("error")
    status = payload.get("status")
    if isinstance(error, str) and error.strip():
        return error.strip()[:160]
    if isinstance(status, str) and status.strip().upper() == "ERROR":
        return "the CLI reported status ERROR with no message"
    return ""


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


# ─── Parsing the quota payload ───────────────────────────────────────────────


def extract_buckets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The `buckets` list, wherever the CLI envelope put it.

    The raw `v1internal:retrieveUserQuota` body has `buckets` at the top level.
    Whether `agy --output-format json` passes that through or wraps it (in
    `result`, `data`, `response`, ...) is not documented, so one level of
    envelope is searched. Deeper than that would be guessing, and a payload this
    function cannot read must raise rather than return an empty list that reads
    as "no quota buckets exist".
    """
    for candidate in (payload, *(v for v in payload.values() if isinstance(v, dict))):
        buckets = candidate.get("buckets")
        if isinstance(buckets, list):
            return [b for b in buckets if isinstance(b, dict)]
    raise quota_probe.ProbeError(
        f"no `buckets` list in the /usage payload (keys: {sorted(payload)[:8]}); "
        f"record it as a fixture and re-check the parser against {QUOTA_ENDPOINT}"
    )


def parse_reset_time(value: Any) -> int | None:
    """RFC3339 string, epoch number, or a protobuf `{seconds: n}` — else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(parse_timestamp(value).timestamp())
        except ValueError:
            return None
    if isinstance(value, dict) and isinstance(value.get("seconds"), (int, str)):
        try:
            return int(value["seconds"])
        except (TypeError, ValueError):
            return None
    return None


def infer_window_minutes(seconds_to_reset: int | None, previous: int | None) -> tuple[int, bool]:
    """(window minutes, inferred?) — snapped UP the ladder, never narrowed.

    Monotone by construction: a slot that has once seen more than five hours of
    remaining time is a daily or weekly window and must not fall back to the
    5-hour rung when it is next sampled with twenty minutes left, because the
    slot key is built from this number and a slot that renames itself loses its
    own history mid-window.
    """
    floor = previous or 0
    if seconds_to_reset is None or seconds_to_reset <= 0:
        return max(WINDOW_LADDER[0], floor), True
    minutes = seconds_to_reset / 60
    rung = next((w for w in WINDOW_LADDER if w >= minutes), WINDOW_LADDER[-1])
    return max(rung, floor), True


def bucket_limit_id(bucket: dict[str, Any]) -> str:
    """`<modelId>:<tokenType>`.

    The token type is always in the key, even though REQUESTS is the only value
    the documented shape carries. Adding it later, on the day a TOKENS bucket
    appears, would rename every existing slot and orphan its stored anchor.
    """
    model = str(bucket.get("modelId") or "unknown-model")
    token_type = str(bucket.get("tokenType") or "UNKNOWN")
    return f"{model}:{token_type}"


def parse_quota(
    payload: dict[str, Any],
    now: int,
    *,
    windows: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Flatten the quota payload into rows `quota_probe.Detector` can consume.

    `windows` is the per-slot window ledger; it is READ and UPDATED in place so
    the caller can persist it beside the detector cursor.
    """
    ledger = windows if windows is not None else {}
    plan = payload.get("planType") or payload.get("tier")
    rows: list[dict[str, Any]] = []
    for bucket in extract_buckets(payload):
        fraction = bucket.get("remainingFraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            # No readable remaining fraction means no usage signal. Skipping is
            # the honest outcome: a bucket defaulted to 0% used would look like
            # a window that just cleared.
            quota_probe.log(
                f"SKIPPED bucket {bucket_limit_id(bucket)}: "
                f"remainingFraction is {fraction!r}, not a number"
            )
            continue
        limit_id = bucket_limit_id(bucket)
        resets_at = parse_reset_time(bucket.get("resetTime"))
        minutes, inferred = infer_window_minutes(
            None if resets_at is None else resets_at - now, ledger.get(limit_id)
        )
        ledger[limit_id] = minutes
        rows.append(
            {
                "t": now,
                "limit_id": limit_id,
                "slot": "primary",
                "window_minutes": minutes,
                # Clamped: a fraction outside [0, 1] would otherwise render as a
                # negative or >100 usage figure on the site.
                "used_percent": round(min(100.0, max(0.0, (1.0 - float(fraction)) * 100.0)), 4),
                "resets_at": resets_at,
                "plan_type": plan,
                # Not 0: Antigravity has no banked-credit bank at all, and 0
                # would let classify() read the next clear as a spent credit.
                "credits_available": None,
                "window_inferred": inferred,
                "remaining_amount": bucket.get("remainingAmount"),
            }
        )
    return sorted(rows, key=lambda row: row["limit_id"])


# ─── Cadence precondition ────────────────────────────────────────────────────


def check_cadence(poll_seconds: int = POLL_SECONDS) -> None:
    """Refuse to run at a cadence that would manufacture false candidates.

    `classify()` calls a clear natural when it lands within
    NATURAL_GRACE_SECONDS of the declared expiry. That grace is derived from the
    Codex probe's own POLL_SECONDS, so at a 300 s cadence against a 180 s grace
    every ordinary window rollover would be bracketed too widely and read as
    "cleared early with the credit bank unchanged" — a global_candidate on every
    single window, forever. Refusing is better than emitting that.
    """
    grace = quota_probe.NATURAL_GRACE_SECONDS
    if grace < 2 * poll_seconds + 60:
        raise quota_probe.ProbeError(
            f"antigravity probe would misread every scheduled expiry as an early "
            f"clear: it polls every {poll_seconds}s but quota_probe's natural-expiry "
            f"grace is only {grace}s; start the unit with "
            f"AI_RESETS_POLL_SECONDS={poll_seconds} so the grace scales with the cadence"
        )


# ─── Cursor ──────────────────────────────────────────────────────────────────


def load_cursor(path: Path | None = None) -> dict[str, Any]:
    try:
        loaded = json.loads((path or CURSOR_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_cursor(cursor: dict[str, Any], path: Path | None = None) -> None:
    target = path or CURSOR_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, target)


# ─── Polling ─────────────────────────────────────────────────────────────────


def poll_once(
    detector: quota_probe.Detector,
    windows: dict[str, int],
    *,
    now: int | None = None,
    fetch: Callable[[], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One sample through the detector; returns the events it produced."""
    # `read_usage` is resolved here, not as a default: a default binds the
    # function object at import and would ignore a patched module attribute.
    fetch = fetch or read_usage
    moment = int(time.time()) if now is None else now
    events: list[dict[str, Any]] = []
    for row in parse_quota(fetch(), moment, windows=windows):
        events.extend(detector.observe(row))
    return events


def run(
    *,
    once: bool = False,
    fetch: Callable[[], dict[str, Any]] | None = None,
    cursor_path: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], int] = lambda: int(time.time()),
) -> int:
    """The poll loop. Exit 2 after MAX_CONSECUTIVE_FAILURES, matching Codex.

    Health is written on EVERY iteration, failing ones included, and always
    through `quota_probe.write_health`, which merges into probe_health.json
    under this probe's own label. Writing the file wholesale here would erase
    the codex block and blind the notifier to the one probe that works.
    """
    check_cadence()
    stored = load_cursor(cursor_path)
    detector = quota_probe.Detector(stored)
    windows: dict[str, int] = {
        str(k): int(v) for k, v in (stored.get("windows") or {}).items() if isinstance(v, int)
    }
    last_ok_at, blind_since = quota_probe.resume_health(PROBE_LABEL)
    failures = 0

    while True:
        now = clock()
        error: str | None = None
        try:
            for event in poll_once(detector, windows, now=now, fetch=fetch):
                # `.get` throughout: a credit_granted record carries no
                # used_before/used_after, and this line must never be the thing
                # that crashes a probe loop.
                quota_probe.log(
                    f"{VENDOR} {event.get('kind')} {event.get('slot')} "
                    f"{event.get('used_before')}%->{event.get('used_after')}% "
                    f"[{event.get('classification')}]"
                )
            failures, last_ok_at, blind_since = 0, now, None
        except quota_probe.ProbeError as exc:
            failures += 1
            error = str(exc)
            blind_since = blind_since or now
            quota_probe.log(f"POLL FAILED ({failures}/{MAX_CONSECUTIVE_FAILURES}): {error}")

        quota_probe.write_health(
            quota_probe.health_block(
                detector,
                now,
                last_ok_at=last_ok_at,
                consecutive_failures=failures,
                blind_since=blind_since,
                last_error=error,
                label=PROBE_LABEL,
            ),
            label=PROBE_LABEL,
        )
        save_cursor({**detector.cursor(), "windows": windows}, cursor_path)

        if failures >= MAX_CONSECUTIVE_FAILURES:
            quota_probe.log(f"PROBE EXITING after {failures} consecutive failures")
            return 2
        if once:
            return 0 if error is None else 1
        sleep(POLL_SECONDS)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    parser.add_argument(
        "--fixture",
        metavar="PATH",
        help="parse a recorded /usage payload instead of calling agy — the only "
        "path that works on a host with no interactive Google login",
    )
    parser.add_argument("--cursor", metavar="PATH", help="override the cursor file")
    args = parser.parse_args(argv)

    fetch: Callable[[], dict[str, Any]] | None = None
    if args.fixture:
        recorded = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        fetch = lambda: recorded  # noqa: E731 — a one-expression stub reads better here
    try:
        return run(
            once=args.once,
            fetch=fetch,
            cursor_path=Path(args.cursor) if args.cursor else None,
        )
    except quota_probe.ProbeError as exc:
        quota_probe.log(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
