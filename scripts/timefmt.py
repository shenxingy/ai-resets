#!/usr/bin/env python3
"""One Pacific clock, shared by the site and the notification email.

The site has always shown Pacific time with the correct PST/PDT abbreviation.
The email showed a bare UTC calendar date, so the same reset read as two
different moments depending on where a subscriber looked. Both now call the
functions here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_pacific(value: str, *, date_only: bool = False) -> str:
    local = parse_timestamp(value).astimezone(PACIFIC)
    if date_only:
        return f"{local.strftime('%b')} {local.day}, {local.year}"
    return f"{local.strftime('%b')} {local.day}, {local.year}, {local.strftime('%-I:%M %p %Z')}"


def format_pacific_with_utc(value: str) -> str:
    """`Sep 4, 2026, 5:39 PM PDT (00:39 UTC Sep 5)`.

    Pacific first because every vendor announcement so far has been written on
    Pacific business hours; UTC in parentheses because that is what the source
    post carries and what a reader in another zone can convert from. The UTC
    date is appended only when it differs from the Pacific one, which is the
    case that silently misled readers of the old date-only line.
    """
    moment = parse_timestamp(value)
    local = moment.astimezone(PACIFIC)
    utc = moment.astimezone(timezone.utc)
    suffix = "" if utc.date() == local.date() else f" {utc.strftime('%b')} {utc.day}"
    return f"{format_pacific(value)} ({utc.strftime('%H:%M')} UTC{suffix})"


def format_epoch_pacific(epoch: int | float) -> str:
    local = datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(PACIFIC)
    return f"{local.strftime('%b')} {local.day}, {local.year}, {local.strftime('%-I:%M %p %Z')}"


def describe_age(seconds: int | float) -> str:
    """`47 min` / `3.2 h` / `2.1 days` — for "last verified N ago" lines."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        # floor(x + 0.5), i.e. round half UP. Python's round() is half-to-even,
        # which disagrees with the site's JavaScript on every exact half-minute
        # (150 s reads "2 min" here and "3 min" there). The two renderers state
        # the same age or the parity test is meaningless.
        return f"{int(seconds / 60 + 0.5)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"
