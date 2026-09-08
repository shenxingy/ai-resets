#!/usr/bin/env python3
"""The one Pacific clock, under test.

`scripts/timefmt.py` existed with no test file of its own until now, which is
awkward for the module whose entire reason to exist is that the site and the
email had disagreed about when a reset happened. Every case below is one of the
ways they could disagree again:

  * the DST switch, where the abbreviation itself changes and an hour repeats,
  * a Pacific evening whose UTC calendar date is already tomorrow — the exact
    shape that made one reset read as two different days,
  * and `describe_age`'s rounding, which is half-UP rather than Python's
    half-to-even because `site/app.js` rounds half-up. A parity check against
    the JavaScript is included, so the two renderers cannot drift apart in
    silence.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.timefmt import (
    PACIFIC,
    describe_age,
    format_epoch_pacific,
    format_pacific,
    format_pacific_with_utc,
    parse_timestamp,
)

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "site" / "app.js"

# The ages the two renderers must agree on, chosen at the boundaries rather than
# at random: each is the first or last second of one of describe_age's branches,
# plus the exact half-minutes where half-up and half-to-even disagree.
PARITY_AGES = [
    0,
    1,
    89,
    90,
    91,
    149,
    150,
    151,
    209,
    210,
    269,
    270,
    5399,
    5400,
    5401,
    9000,
    172799,
    172800,
    400000,
    1209600,
]


# ─── Parsing ─────────────────────────────────────────────────────────────────


class ParseTimestampTests(unittest.TestCase):
    def test_zulu_suffix_becomes_utc(self):
        moment = parse_timestamp("2026-09-04T20:08:45Z")
        self.assertEqual(moment.tzinfo, timezone.utc)
        self.assertEqual(moment.timestamp(), 1788552525.0)

    def test_explicit_offset_is_kept(self):
        # The tracker feed and the syndication API do not agree on which form
        # they emit; both have to land on the same instant.
        offset = parse_timestamp("2026-09-04T13:08:45-07:00")
        zulu = parse_timestamp("2026-09-04T20:08:45Z")
        self.assertEqual(offset, zulu)

    def test_fractional_seconds_survive(self):
        self.assertEqual(parse_timestamp("2026-09-04T20:08:45.500Z").microsecond, 500000)


# ─── Daylight saving ─────────────────────────────────────────────────────────


class DaylightSavingTests(unittest.TestCase):
    """March 8 2026 10:00 UTC and November 1 2026 09:00 UTC are the switches."""

    def test_minute_before_spring_forward_is_pst(self):
        self.assertEqual(format_pacific("2026-03-08T09:59:00Z"), "Mar 8, 2026, 1:59 AM PST")

    def test_spring_forward_skips_the_2am_hour(self):
        # 09:59 UTC reads 1:59 AM and the very next minute reads 3:00 AM. A
        # naive fixed -8 offset would print 2:00 AM, a local time that does not
        # exist on this date.
        self.assertEqual(format_pacific("2026-03-08T10:00:00Z"), "Mar 8, 2026, 3:00 AM PDT")

    def test_minute_before_fall_back_is_pdt(self):
        self.assertEqual(format_pacific("2026-11-01T08:59:00Z"), "Nov 1, 2026, 1:59 AM PDT")

    def test_fall_back_repeats_the_1am_hour_with_a_new_abbreviation(self):
        # 1:59 AM PDT is followed by 1:00 AM PST. The clock reading repeats, so
        # the zone abbreviation is the only thing distinguishing the two hours —
        # which is why it is never dropped from the rendered string.
        self.assertEqual(format_pacific("2026-11-01T09:00:00Z"), "Nov 1, 2026, 1:00 AM PST")

    def test_the_repeated_hour_stays_ordered(self):
        first = parse_timestamp("2026-11-01T08:30:00Z").astimezone(PACIFIC)
        second = parse_timestamp("2026-11-01T09:30:00Z").astimezone(PACIFIC)
        self.assertEqual(first.strftime("%-I:%M %p"), second.strftime("%-I:%M %p"))
        # Compared as epochs, not as datetimes: Python defines comparison
        # between two aware datetimes IN THE SAME ZONE as a wall-clock
        # comparison that ignores `fold`, so `first < second` is False here
        # even though the second moment is an hour later. Anything that sorts
        # probe samples across the fall-back hour has to sort on the epoch.
        self.assertLess(first.timestamp(), second.timestamp())
        self.assertEqual(second.timestamp() - first.timestamp(), 3600)

    def test_date_only_drops_the_time_and_the_zone(self):
        self.assertEqual(
            format_pacific("2026-03-08T10:00:00Z", date_only=True), "Mar 8, 2026"
        )

    def test_day_is_not_zero_padded(self):
        # `%-d` would be a platform trap; the module builds the day from
        # `local.day`, so a single-digit day must render without a leading zero.
        self.assertIn("Mar 8, 2026", format_pacific("2026-03-08T10:00:00Z"))


# ─── Pacific with UTC in parentheses ─────────────────────────────────────────


class PacificWithUtcTests(unittest.TestCase):
    def test_same_calendar_date_appends_no_utc_date(self):
        self.assertEqual(
            format_pacific_with_utc("2026-09-04T23:39:00Z"),
            "Sep 4, 2026, 4:39 PM PDT (23:39 UTC)",
        )

    def test_utc_date_ahead_is_spelled_out(self):
        # The case the module's docstring names. A Pacific evening is already
        # tomorrow in UTC, and the old date-only email line rendered that as a
        # different day than the site did.
        self.assertEqual(
            format_pacific_with_utc("2026-09-05T00:39:00Z"),
            "Sep 4, 2026, 5:39 PM PDT (00:39 UTC Sep 5)",
        )

    def test_utc_date_behind_is_spelled_out_too(self):
        # The other direction: a Pacific morning in winter whose UTC time is
        # still yesterday never happens (UTC leads Pacific), but a UTC-behind
        # suffix is what the code would emit and must not be malformed.
        rendered = format_pacific_with_utc("2026-01-01T00:30:00Z")
        self.assertEqual(rendered, "Dec 31, 2025, 4:30 PM PST (00:30 UTC Jan 1)")

    def test_the_two_halves_describe_one_instant(self):
        value = "2026-09-05T00:39:00Z"
        rendered = format_pacific_with_utc(value)
        moment = parse_timestamp(value)
        self.assertIn(moment.astimezone(timezone.utc).strftime("%H:%M"), rendered)
        self.assertIn(format_pacific(value), rendered)


# ─── Epoch rendering ─────────────────────────────────────────────────────────


class EpochTests(unittest.TestCase):
    def test_epoch_matches_the_iso_renderer(self):
        moment = datetime(2026, 9, 4, 23, 39, tzinfo=timezone.utc)
        self.assertEqual(
            format_epoch_pacific(int(moment.timestamp())),
            format_pacific(moment.isoformat().replace("+00:00", "Z")),
        )

    def test_float_epoch_is_truncated_not_rounded(self):
        # The probe stores integer seconds; a float arrives only from a caller
        # that divided something. Truncating keeps it inside the same second
        # rather than jumping the rendered minute at .5.
        self.assertEqual(format_epoch_pacific(1757029139.9), format_epoch_pacific(1757029139))


# ─── describe_age ────────────────────────────────────────────────────────────


class DescribeAgeTests(unittest.TestCase):
    def test_negative_ages_clamp_to_zero(self):
        # Clock skew between the probe host and the build host produced a
        # negative age on the site once; "-3 s ago" is not a thing to publish.
        self.assertEqual(describe_age(-5), "0 s")
        self.assertEqual(describe_age(-0.4), "0 s")

    def test_seconds_branch_runs_to_89(self):
        self.assertEqual(describe_age(0), "0 s")
        self.assertEqual(describe_age(1), "1 s")
        self.assertEqual(describe_age(89), "89 s")
        self.assertEqual(describe_age(89.9), "89 s")

    def test_ninety_seconds_is_the_first_minute_reading(self):
        self.assertEqual(describe_age(90), "2 min")

    def test_exact_half_minutes_round_up_not_to_even(self):
        # This is the whole reason the module does `int(x + 0.5)`. Python's
        # round() is half-to-even: round(2.5) is 2 and round(3.5) is 4, so 150 s
        # would read "2 min" here while site/app.js read "3 min" for the same
        # sample. Half-up is what both sides do now.
        self.assertEqual(describe_age(150), "3 min")
        self.assertEqual(describe_age(210), "4 min")
        self.assertEqual(describe_age(270), "5 min")
        self.assertEqual(round(2.5), 2, "round() is still half-to-even; the comment holds")

    def test_minutes_branch_runs_to_5399(self):
        self.assertEqual(describe_age(149), "2 min")
        self.assertEqual(describe_age(5399), "90 min")

    def test_hours_branch_starts_at_5400(self):
        self.assertEqual(describe_age(5400), "1.5 h")
        self.assertEqual(describe_age(9000), "2.5 h")

    def test_hours_branch_runs_to_172799(self):
        self.assertEqual(describe_age(172799), "48.0 h")

    def test_days_branch_starts_at_172800(self):
        self.assertEqual(describe_age(172800), "2.0 days")
        self.assertEqual(describe_age(1209600), "14.0 days")

    def test_every_reading_is_one_number_and_one_unit(self):
        for seconds in PARITY_AGES:
            with self.subTest(seconds=seconds):
                self.assertRegex(describe_age(seconds), r"^\d+(\.\d)? (s|min|h|days)$")


# ─── Python / JavaScript parity ──────────────────────────────────────────────


def _extract_describe_age() -> str | None:
    """Pull `function describeAge(...)` out of site/app.js, or None."""
    if not APP_JS.is_file():
        return None
    match = re.search(
        r"^function describeAge\(.*?^\}", APP_JS.read_text(encoding="utf-8"), re.S | re.M
    )
    return match.group(0) if match else None


class JavaScriptParityTests(unittest.TestCase):
    """The site renders ages in the browser; the email renders them in Python.

    A subscriber who opens both sees the same reset described twice. When the
    two rounding rules disagree the difference is one minute, which reads as two
    different observations rather than one — the same class of bug that made
    this module necessary. Skipped rather than failed when node is absent: node
    is a developer convenience here, not a runtime dependency of the site.
    """

    def test_python_and_app_js_agree_on_every_boundary(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; the JS half cannot be evaluated")
        source = _extract_describe_age()
        if source is None:
            self.skipTest(f"no `function describeAge` block found in {APP_JS}")
        script = f"{source}\nconsole.log(JSON.stringify({json.dumps(PARITY_AGES)}.map(describeAge)));"
        completed = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
        )
        from_js = json.loads(completed.stdout)
        self.assertEqual(from_js, [describe_age(age) for age in PARITY_AGES])


if __name__ == "__main__":
    unittest.main()
