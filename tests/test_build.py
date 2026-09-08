import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import build
from scripts.build import (
    OBSERVED_CONFIRMED,
    OBSERVED_NOT_SEEN,
    OBSERVED_NO_READING,
    OBSERVED_UNPROBED,
    annotate_announcements,
    OBSERVATIONS_SHOWN,
    PROBE_STALE_SECONDS,
    VERDICT_LABELS,
    attach_observations,
    build_observations,
    build_timing_pattern,
    build_schema,
    build_vendor_cards,
    load_vendors,
    probe_badge,
    public_observations,
    replace_marker,
    timing_stats,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────
#
# Shaped from the real rows in /var/lib/ai-resets/quota_events.jsonl and the
# 2026-08-30 clear that predates that log: a vendor_reset that may be shown, a
# self_applied clear and a credit grant that may never be, all in the export
# contract's own vocabulary. Written out here so these tests never depend on
# the gitignored data/observed/openai.json actually existing.

VENDOR_RESET = {
    "observed_at": "2026-08-31T02:26:56Z",
    "window": "weekly",
    "verdict": "vendor_reset",
    "public": True,
    "headline": "Our own Codex weekly counter cleared 5.9 days early, and nothing this account did explains it.",
    "evidence": "Usage went 16% to 0%, 140.6 hours before the scheduled expiry, with the banked-credit count read on both sides and unchanged at 1.",
    "used_before": 16.0,
    "used_after": 0.0,
    "early_by_hours": 140.6,
}
SELF_APPLIED = {
    "observed_at": "2026-09-05T05:32:36Z",
    "window": "weekly",
    "verdict": "self_applied",
    "public": False,
    "headline": "This account spent one of its own banked credits.",
    "evidence": "banked credit count fell 2 -> 1",
    "early_by_hours": 94.6,
}
CREDIT_GRANTED = {
    "observed_at": "2026-09-05T04:20:36Z",
    "window": "weekly",
    "verdict": "credit_granted",
    "public": False,
    "headline": "A banked credit arrived.",
    "evidence": "credits 1 -> 2",
}
NATURAL = {
    "observed_at": "2026-09-03T00:00:00Z",
    "window": "weekly",
    "verdict": "natural_expiry",
    "public": True,
    "headline": "Our weekly window reached its scheduled expiry.",
    "evidence": "The clear landed 2 minutes after the anchor that was in force.",
}
PROBE_OK = {
    "status": "ok",
    "last_verified_at": "2026-09-05T06:24:36Z",
    "detectable_now": {"codex/10080": False},
    "coverage": "One Codex Pro account, weekly window only.",
}
NOW = datetime(2026, 9, 5, 6, 25, 36, tzinfo=timezone.utc)  # 60 s after PROBE_OK


def vendor(observations=(), probe=None, events=()):
    payload = {"source": None, "fetched_at": None, "events": list(events)}
    if probe is not None:
        payload["probe"] = probe
    payload["observations"] = public_observations(observations)
    return payload


class BuildTests(unittest.TestCase):
    def test_marker_replacement_is_repeatable(self):
        original = "a<!-- STATIC_TEST_START -->old<!-- STATIC_TEST_END -->z"
        updated = replace_marker(original, "TEST", "new")
        self.assertEqual(updated, "a<!-- STATIC_TEST_START -->new<!-- STATIC_TEST_END -->z")
        self.assertEqual(replace_marker(updated, "TEST", "next"), "a<!-- STATIC_TEST_START -->next<!-- STATIC_TEST_END -->z")

    def test_timing_uses_pacific_weekday_across_utc_boundary(self):
        events = [
            {"announced_at": "2026-08-01T03:32:37Z"},  # Friday 8:32 PM PDT
            {"announced_at": "2026-07-31T16:00:00Z"},  # Friday 9:00 AM PDT
        ]
        stats = timing_stats(events, datetime(2026, 8, 1, 6, tzinfo=timezone.utc))
        self.assertEqual(stats["friday_total"], 2)
        self.assertEqual(stats["friday_evening"], 1)
        self.assertEqual(stats["cells"][4], [0, 1, 0, 1])

    def test_schema_describes_public_dataset(self):
        vendors = {
            "openai": {
                "source": {"name": "Source", "url": "https://example.test/source"},
                "events": [
                    {
                        "id": "one",
                        "announced_at": "2026-07-31T16:00:00Z",
                        "text": "Reset",
                        "url": "https://example.test/event",
                    }
                ],
            }
        }
        block = build_schema(vendors, "2026-08-01T00:00:00Z")
        payload = block.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
        graph = json.loads(payload)["@graph"]
        dataset = next(item for item in graph if item["@type"] == "Dataset")
        self.assertEqual(dataset["distribution"]["contentUrl"], "https://resets.alexshen.dev/data.json")
        self.assertEqual(dataset["isBasedOn"], ["https://example.test/source"])


# ─── Which observations may be published ─────────────────────────────────────


class PublicObservationTests(unittest.TestCase):
    def test_owner_verdicts_never_survive_even_when_flagged_public(self):
        # The worst thing this build could do: publish what the account holder
        # did with their own banked credits. The verdict gate has to hold on its
        # own, without relying on the exporter's `public` flag being right.
        leaky = [dict(SELF_APPLIED, public=True), dict(CREDIT_GRANTED, public=True)]
        self.assertEqual(public_observations(leaky), [])

    def test_public_flag_false_drops_an_otherwise_publishable_row(self):
        self.assertEqual(public_observations([dict(VENDOR_RESET, public=False)]), [])

    def test_unknown_verdict_is_dropped(self):
        self.assertEqual(public_observations([dict(VENDOR_RESET, verdict="something_new")]), [])

    def test_row_with_no_sentence_is_dropped(self):
        empty = dict(VENDOR_RESET)
        empty.pop("headline")
        empty.pop("evidence")
        self.assertEqual(public_observations([empty]), [])

    def test_newest_first_and_unknown_fields_stripped(self):
        rows = public_observations(
            [
                dict(NATURAL, reason="banked credit count fell 2 -> 1"),
                VENDOR_RESET,
                SELF_APPLIED,
            ]
        )
        self.assertEqual([row["verdict"] for row in rows], ["natural_expiry", "vendor_reset"])
        # Whitelisted fields only: an internal reason string that names the
        # credit bank must not ride along into site/data.json.
        self.assertNotIn("reason", rows[0])
        self.assertNotIn("public", rows[0])

    def test_missing_observed_at_does_not_raise_while_sorting(self):
        undated = dict(VENDOR_RESET)
        undated.pop("observed_at")
        rows = public_observations([undated, NATURAL])
        self.assertEqual(len(rows), 2)


# ─── The badge that replaced "Tracking" ──────────────────────────────────────


class ProbeBadgeTests(unittest.TestCase):
    def test_fresh_probe_reports_ground_truth(self):
        text, state = probe_badge(PROBE_OK, NOW)
        self.assertEqual(state, "verified")
        self.assertEqual(text, "Ground truth · verified 60 s ago")

    def test_stale_ok_probe_is_not_allowed_to_claim_verified(self):
        # An "ok" status written 47 minutes ago is a probe that stopped writing,
        # not a probe that just reported. The owner is being paged at 15 min.
        stale = NOW + timedelta(seconds=PROBE_STALE_SECONDS + 1)
        text, state = probe_badge(PROBE_OK, stale + timedelta(minutes=45))
        self.assertEqual(state, "offline")
        self.assertIn("probe offline", text)

    def test_blind_probe_says_offline(self):
        text, state = probe_badge(dict(PROBE_OK, status="blind"), NOW)
        self.assertEqual(state, "offline")
        self.assertEqual(text, "Ground truth · probe offline 60 s")

    def test_a_probe_with_no_health_file_depends_on_what_the_card_shows(self):
        # Two opposite mistakes, one on each side of the same status.
        # With observations on the card, "Announcements only" would deny a probe
        # directly above its own measurements.
        text, state = probe_badge({"status": "absent"}, NOW, [{"verdict": "vendor_reset"}])
        self.assertEqual((text, state), ("Ground truth · probe state unknown", "offline"))
        # With nothing on the card, "Ground truth" promises a measurement the
        # card does not contain. This is the Anthropic case the day its probe
        # ships and before it has ever run.
        text, state = probe_badge({"status": "absent"}, NOW, [])
        self.assertEqual((text, state), ("Announcements only", "none"))
        self.assertEqual(probe_badge({"status": "absent"}, NOW), ("Announcements only", "none"))

    def test_the_two_renderers_agree_on_every_badge_case(self):
        """Run the JavaScript and compare it to the Python, case by case.

        They diverged silently once: app.js sent an `absent` probe to
        "Announcements only" while build.py sent it to "probe state unknown",
        so the crawlable snapshot and the live page disagreed on the one card
        the Claude probe was about to appear on. A test that greps the source
        for strings cannot catch that; only running both can.
        """
        node = shutil.which("node")
        if node is None:
            raise unittest.SkipTest("node is not available on this machine")
        app_js = Path(__file__).resolve().parent.parent / "site" / "app.js"

        epoch = int(NOW.timestamp())

        def at(offset):
            return datetime.fromtimestamp(epoch - offset, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        cases = [
            (None, []),
            ({}, []),
            ({"status": "absent"}, []),
            ({"status": "absent"}, [{"verdict": "vendor_reset"}]),
            ({"status": "unknown-to-this-build"}, [{"verdict": "vendor_reset"}]),
            ({"status": "ok", "last_verified_at": at(30)}, []),
            ({"status": "ok", "last_verified_at": at(30)}, [{"verdict": "x"}]),
            ({"status": "ok", "last_verified_at": at(4000)}, [{"verdict": "x"}]),
            ({"status": "blind", "last_verified_at": at(4000)}, [{"verdict": "x"}]),
            ({"status": "ok"}, [{"verdict": "x"}]),
        ]
        harness = f"""
        const fs = require('fs');
        const src = fs.readFileSync({str(app_js)!r}, 'utf8');
        const PROBE_STALE_SECONDS = {PROBE_STALE_SECONDS};
        eval(src.match(/function describeAge[\\s\\S]*?\\n}}/)[0]);
        eval(src.match(/function probeAgeSeconds[\\s\\S]*?\\n}}/)[0]);
        eval(src.match(/function probeBadge[\\s\\S]*?\\n}}/)[0]);
        const now = new Date({epoch * 1000});
        const cases = JSON.parse(process.env.BADGE_CASES);
        console.log(JSON.stringify(cases.map(
          ([probe, obs]) => probeBadge(probe, now, obs)
        )));
        """
        # Through the environment rather than argv: `node -e` swallows the
        # script and the separator, so the position of a trailing argument is
        # a detail of the node version.
        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "BADGE_CASES": json.dumps(cases)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        from_js = json.loads(result.stdout)

        generated = NOW
        for (probe, observations), js in zip(cases, from_js):
            with self.subTest(probe=probe, observations=len(observations)):
                text, state = probe_badge(probe, generated, observations)
                self.assertEqual(text, js["text"])
                self.assertEqual(state, js["state"])

    def test_vendor_without_a_probe_says_announcements_only(self):
        for probe in (None, {}, {"status": "unknown-to-this-build"}, "not a dict"):
            text, state = probe_badge(probe, NOW)
            self.assertEqual((text, state), ("Announcements only", "none"), probe)

    def test_unparseable_verified_time_never_claims_freshness(self):
        text, state = probe_badge(dict(PROBE_OK, last_verified_at="not a time"), NOW)
        self.assertEqual(state, "offline")
        self.assertNotIn("verified", text)


# ─── Rendering ───────────────────────────────────────────────────────────────


class ObservationRenderTests(unittest.TestCase):
    def test_renders_headline_evidence_verdict_and_pacific_time(self):
        markup = build_observations(vendor([VENDOR_RESET, SELF_APPLIED], PROBE_OK))
        self.assertIn('data-verdict="vendor_reset"', markup)
        self.assertIn(VERDICT_LABELS["vendor_reset"], markup)
        self.assertIn("cleared 5.9 days early", markup)
        self.assertIn("140.6 hours before the scheduled expiry", markup)
        self.assertIn("Aug 30, 2026, 7:26 PM PDT", markup)
        self.assertIn('<time datetime="2026-08-31T02:26:56Z">', markup)
        # The credit spend is not on the page in any form.
        self.assertNotIn("self_applied", markup)
        self.assertNotIn("banked credit count fell", markup)

    def test_coverage_sentence_comes_from_the_probe_not_from_this_file(self):
        markup = build_observations(vendor([VENDOR_RESET], PROBE_OK))
        self.assertIn("One Codex Pro account, weekly window only.", markup)
        self.assertIn('href="methodology.html#ground-truth"', markup)

    def test_missing_coverage_falls_back_without_claiming_a_population(self):
        markup = build_observations(vendor([VENDOR_RESET], {"status": "ok"}))
        self.assertIn(build.DEFAULT_COVERAGE, markup)

    def test_truncation_is_stated_rather_than_silent(self):
        rows = [
            dict(VENDOR_RESET, observed_at=f"2026-08-{day:02d}T02:26:56Z")
            for day in range(1, OBSERVATIONS_SHOWN + 4)
        ]
        markup = build_observations(vendor(rows, PROBE_OK))
        self.assertEqual(markup.count('<li class="observation"'), OBSERVATIONS_SHOWN)
        self.assertIn(f"Showing the {OBSERVATIONS_SHOWN} most recent of {len(rows)}.", markup)

    def test_no_observations_renders_nothing(self):
        self.assertEqual(build_observations(vendor([SELF_APPLIED], PROBE_OK)), "")


ANNOUNCEMENT = {
    "id": "one",
    "announced_at": "2026-09-04T16:00:00Z",
    "text": "Reset for everyone.",
    "url": "https://example.test/event",
    "kind": "reset",
}


class VendorCardTests(unittest.TestCase):
    def test_card_carries_the_probe_badge_instead_of_tracking(self):
        vendors = {"openai": vendor([VENDOR_RESET], PROBE_OK, [ANNOUNCEMENT])}
        markup = build_vendor_cards(vendors, NOW)
        self.assertIn('<span class="signal-status" data-probe="verified">', markup)
        self.assertNotIn("Tracking", markup)
        self.assertIn('class="observations"', markup)

    def test_vendor_without_a_probe_says_announcements_only(self):
        vendors = {"anthropic": vendor(events=[ANNOUNCEMENT])}
        markup = build_vendor_cards(vendors, NOW)
        self.assertIn('data-probe="none">Announcements only<', markup)
        self.assertNotIn('class="observations"', markup)

    def test_probe_only_vendor_renders_without_an_announcement_feed(self):
        # data/openai.json is a gitignored fetch cache. Before this, an empty
        # events list reached events[-1] and took the whole publish down.
        vendors = {"openai": vendor([VENDOR_RESET], PROBE_OK)}
        markup = build_vendor_cards(vendors, NOW)
        self.assertIn("Codex / ChatGPT", markup)
        self.assertNotIn("Last tracked move", markup)
        self.assertIn("cleared 5.9 days early", markup)

    def test_vendor_with_neither_signal_is_not_rendered(self):
        self.assertEqual(build_vendor_cards({"openai": vendor()}, NOW), "")

    def test_observations_stay_out_of_announcement_statistics(self):
        # "Latest tracked move", the tracked-event count and the weekday timing
        # pattern are about announcements. An observation is not one.
        with_observations = vendor([VENDOR_RESET, NATURAL], PROBE_OK, [ANNOUNCEMENT])
        without = vendor(probe=PROBE_OK, events=[ANNOUNCEMENT])
        markup = build_vendor_cards({"openai": with_observations}, NOW)
        self.assertIn('<div class="kpi-value">1</div>', markup)  # one event, not three
        self.assertEqual(
            build.build_announcement_body(with_observations, with_observations["events"], NOW),
            build.build_announcement_body(without, without["events"], NOW),
        )
        self.assertLess(markup.index("Last tracked move"), markup.index('class="observations"'))
        self.assertEqual(
            timing_stats(with_observations["events"], NOW),
            timing_stats(without["events"], NOW),
        )
        self.assertEqual(
            build_timing_pattern(with_observations["events"], NOW),
            build_timing_pattern(without["events"], NOW),
        )


# ─── Loading ─────────────────────────────────────────────────────────────────


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # The loaders print what they skipped, which is the point in a cron log
        # and noise here. Swallow it so a failure is the only thing on screen.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def write(self, name, payload):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, encoding="utf-8")
        return path

    def test_seed_without_a_vendor_key_does_not_abort_the_publish(self):
        # This ran from cron ahead of the rsync: one KeyError here used to take
        # the whole site build down before anything was published.
        self.write("broken.json", {"events": []})
        self.write("openai.json", {"vendor": "openai", "events": [ANNOUNCEMENT]})
        vendors = load_vendors(self.data)
        self.assertEqual(list(vendors), ["openai"])

    def test_unparseable_seed_is_skipped(self):
        self.write("openai.json", "{not json")
        self.assertEqual(load_vendors(self.data), {})

    def test_observed_directory_is_never_read_as_a_vendor_seed(self):
        (self.data / "observed").mkdir()
        self.write("observed/openai.json", {"vendor": "openai", "observations": []})
        (self.data / "trap.json").mkdir()
        self.write("anthropic.json", {"vendor": "anthropic", "events": [ANNOUNCEMENT]})
        self.assertEqual(list(load_vendors(self.data)), ["anthropic"])

    def test_events_without_a_timestamp_cannot_break_the_sort(self):
        self.write("openai.json", {"vendor": "openai", "events": [ANNOUNCEMENT, {"id": "junk"}, "nope"]})
        self.assertEqual(len(load_vendors(self.data)["openai"]["events"]), 1)

    def test_attach_observations_publishes_only_public_rows(self):
        observed = self.data / "observed"
        observed.mkdir()
        self.write(
            "observed/openai.json",
            {
                "vendor": "openai",
                "probe": dict(PROBE_OK, last_error="/home/someone/token.json"),
                "observations": [VENDOR_RESET, SELF_APPLIED, CREDIT_GRANTED],
            },
        )
        vendors = attach_observations({}, observed)
        # A probe with no announcement seed still gets a card: an own-account
        # measurement is a signal about that vendor whether or not anyone
        # tweeted about it.
        self.assertEqual(list(vendors), ["openai"])
        self.assertEqual([row["verdict"] for row in vendors["openai"]["observations"]], ["vendor_reset"])
        self.assertEqual(vendors["openai"]["events"], [])
        # Probe fields are whitelisted too: last_error can carry a filesystem
        # path, and nothing here has reviewed it.
        self.assertNotIn("last_error", vendors["openai"]["probe"])
        self.assertEqual(vendors["openai"]["probe"]["status"], "ok")

    def test_missing_or_malformed_export_leaves_the_build_alone(self):
        observed = self.data / "observed"
        observed.mkdir()
        self.assertEqual(attach_observations({}, observed), {})
        self.write("observed/openai.json", "[]")
        self.assertEqual(attach_observations({}, observed), {})
        self.write("observed/openai.json", "{oops")
        self.assertEqual(attach_observations({}, observed), {})


# ─── The two renderers must not drift ────────────────────────────────────────


class StaticAndInteractiveParityTests(unittest.TestCase):
    """scripts/build.py renders the crawlable snapshot and site/app.js renders
    the live page. They are separate implementations of one design, so every
    term one of them uses has to exist in the other."""

    def setUp(self):
        self.app_js = (Path(build.SITE_DIR) / "app.js").read_text(encoding="utf-8")

    def test_verdict_labels_match(self):
        for verdict, label in VERDICT_LABELS.items():
            self.assertIn(verdict, self.app_js, f"app.js has no {verdict}")
            self.assertIn(label, self.app_js, f"app.js has no label for {verdict}")

    def test_owner_only_verdicts_have_no_label_on_either_side(self):
        for private in ("self_applied", "credit_granted"):
            self.assertNotIn(private, VERDICT_LABELS)
            self.assertNotIn(f"{private}:", self.app_js)

    def test_markup_vocabulary_matches(self):
        for token in (
            "observations",
            "observations-label",
            "observation-list",
            "observation-head",
            "observation-verdict",
            "observation-headline",
            "observation-evidence",
            "observation-note",
            "signal-status",
            "data-probe",
            "methodology.html#ground-truth",
        ):
            self.assertIn(token, self.app_js, f"app.js has no {token}")

    def test_badge_wording_and_thresholds_match(self):
        for token in (
            "Announcements only",
            "Ground truth · verified ",
            "Ground truth · probe offline ",
            "Ground truth · probe state unknown",
            build.DEFAULT_COVERAGE,
            build.NO_ANNOUNCEMENTS_HTML.split(">")[1].split("<")[0],
        ):
            self.assertIn(token, self.app_js, f"app.js has no {token!r}")
        self.assertIn(f"PROBE_STALE_SECONDS = {PROBE_STALE_SECONDS}", self.app_js)
        self.assertIn(f"OBSERVATIONS_SHOWN = {OBSERVATIONS_SHOWN}", self.app_js)

    def test_the_tracking_badge_is_gone_from_both(self):
        self.assertNotIn('["Tracking"]', self.app_js)
        self.assertNotIn(">Tracking<", (Path(build.SITE_DIR) / "index.template.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class AnnounceAnnotationTests(unittest.TestCase):
    """Does an announcement's claim show up on a real account, and when.

    Four statuses that must never be collapsed, because only one of them means
    the announcement went unobserved:
      unprobed    we measure nothing for this vendor
      no_reading  the probe ships but has never reported
      not_seen    the probe looked and saw no vendor reset near the post
      confirmed   a vendor_reset observation sits within the match window
    """

    # The real 2026-08-30 pair: this account cleared 149 seconds BEFORE the post.
    OBSERVED_AT = 1788143216
    POST_AT = 1788143365
    CLEAR = {
        "event_id": "codex/10080:1788143216",
        "kind": "clear",
        "slot": "codex/10080",
        "detected_at": OBSERVED_AT,
        "used_before": 16.0,
        "used_after": 0.0,
        "classification": "global_candidate",
        "confirmed": True,
        "early_by_seconds": 506134,
        "credits_before": 1,
        "credits_after": 1,
    }

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state = Path(self.tempdir.name)

    def write_events(self, records, name="quota_events.jsonl"):
        (self.state / name).write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    def write_health(self, *labels):
        (self.state / "probe_health.json").write_text(
            json.dumps({label: {"label": label, "updated_at": self.POST_AT} for label in labels}),
            encoding="utf-8",
        )

    def vendors(self, key="openai", announced_at="2026-08-31T02:29:25Z"):
        return {key: {"events": [{"id": "1", "announced_at": announced_at, "text": "reset"}]}}

    def status(self, vendors, key="openai"):
        return (vendors[key]["events"][0].get("observed") or {}).get("status")

    def test_a_vendor_we_do_not_measure_is_marked_unprobed(self):
        vendors = self.vendors("google")
        annotate_announcements(vendors, self.state)
        self.assertEqual(self.status(vendors, "google"), OBSERVED_UNPROBED)

    def test_a_probe_that_never_reported_is_not_a_look_that_found_nothing(self):
        vendors = self.vendors()
        annotate_announcements(vendors, self.state)  # no health file at all
        self.assertEqual(self.status(vendors), OBSERVED_NO_READING)

    def test_a_probe_that_looked_and_saw_nothing_is_not_seen(self):
        self.write_health("codex")
        self.write_events([])
        vendors = self.vendors()
        annotate_announcements(vendors, self.state)
        self.assertEqual(self.status(vendors), OBSERVED_NOT_SEEN)

    def test_an_observation_before_the_post_confirms_it(self):
        self.write_health("codex")
        self.write_events([self.CLEAR])
        vendors = self.vendors()
        annotate_announcements(vendors, self.state)
        observed = vendors["openai"]["events"][0]["observed"]
        self.assertEqual(observed["status"], OBSERVED_CONFIRMED)
        # Positive lead means our account saw it first, which is the ordinary case.
        self.assertEqual(observed["lead_seconds"], self.POST_AT - self.OBSERVED_AT)

    def test_a_self_applied_clear_never_confirms_an_announcement(self):
        # It says what the account holder did with their own banked credit.
        self.write_health("codex")
        self.write_events([{**self.CLEAR, "classification": "self_applied_credit"}])
        vendors = self.vendors()
        annotate_announcements(vendors, self.state)
        self.assertEqual(self.status(vendors), OBSERVED_NOT_SEEN)

    def test_a_natural_expiry_never_confirms_an_announcement(self):
        self.write_health("codex")
        self.write_events([{**self.CLEAR, "classification": "natural_expiry"}])
        vendors = self.vendors()
        annotate_announcements(vendors, self.state)
        self.assertEqual(self.status(vendors), OBSERVED_NOT_SEEN)

    def test_an_observation_outside_the_window_does_not_confirm(self):
        self.write_health("codex")
        self.write_events([self.CLEAR])
        vendors = self.vendors(announced_at="2026-09-01T02:29:25Z")
        annotate_announcements(vendors, self.state)
        self.assertEqual(self.status(vendors), OBSERVED_NOT_SEEN)

    def test_an_undatable_announcement_is_left_alone_rather_than_guessed(self):
        self.write_health("codex")
        self.write_events([self.CLEAR])
        vendors = {"openai": {"events": [{"id": "1", "text": "reset"}]}}
        annotate_announcements(vendors, self.state)
        self.assertIsNone(vendors["openai"]["events"][0].get("observed"))

    def test_a_broken_state_directory_does_not_take_the_publish_down(self):
        self.write_health("codex")
        (self.state / "quota_events.jsonl").write_text("{not json\n", encoding="utf-8")
        vendors = self.vendors()
        annotate_announcements(vendors, self.state)  # must not raise
        self.assertEqual(self.status(vendors), OBSERVED_NOT_SEEN)


# ─── The analytics script, and the key that must not be in the repository ────


class AnalyticsRenderTests(unittest.TestCase):
    """`write_analytics` decides whether this deployment tracks anything.

    The PostHog project key used to be a literal in the tracked `analytics.js`.
    A project key in a public repository is a project key strangers can post
    events into, so the tracked file became a template and the key moved to the
    environment. These tests hold that line: what ships with no key set must be
    inert, and what ships with one must carry exactly that one.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ai-resets-analytics-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = self.tmp / "analytics.js"
        patcher = unittest.mock.patch.object(build, "ANALYTICS_FILE", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, key=None):
        env = {} if key is None else {"AI_RESETS_POSTHOG_KEY": key}
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            if key is None:
                os.environ.pop("AI_RESETS_POSTHOG_KEY", None)
            build.write_analytics()
        return self.out.read_text(encoding="utf-8")

    def test_the_tracked_template_carries_no_key(self):
        # The assertion the whole change exists for.
        template = build.ANALYTICS_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("phc_", template)
        self.assertIn("__AI_RESETS_POSTHOG_KEY__", template)

    def test_no_key_yields_an_inert_file_that_still_exists(self):
        # Three pages carry `<script src="analytics.js">`; a missing file is a
        # 404 on every page view, so "disabled" cannot mean "absent".
        rendered = self.render(None)
        self.assertTrue(self.out.is_file())
        for forbidden in ("sendBeacon", "fetch(", "phc_", "aiResetTrack ="):
            self.assertNotIn(forbidden, rendered)
        # The file has to SAY it is disabled. check_site reads that line rather
        # than guessing from content, because guessing is what went wrong: the
        # stub's own comment used to name the tracker function and a substring
        # test then graded it as a live tracker with the privacy flags missing.
        self.assertIn("// ai-resets-analytics: disabled", rendered)

    def test_a_key_is_substituted_and_nothing_else_changes(self):
        rendered = self.render("phc_testkey123")
        self.assertIn("phc_testkey123", rendered)
        self.assertNotIn("__AI_RESETS_POSTHOG_KEY__", rendered)
        self.assertIn("// ai-resets-analytics: enabled", rendered)
        self.assertIn("$geoip_disable: true", rendered)
        self.assertIn("$process_person_profile: false", rendered)

    def test_a_key_that_could_escape_the_string_literal_is_refused(self):
        # The key lands inside a double-quoted JavaScript literal. A quote or a
        # newline in it would end that literal and put the rest of the value
        # into executable position on three public pages.
        for hostile in ('a"; evil(); //', "a\nb", "a'b", "a\\b"):
            with self.subTest(key=hostile):
                with self.assertRaises(SystemExit):
                    self.render(hostile)

    def test_an_empty_or_whitespace_key_counts_as_no_key(self):
        for blank in ("", "   ", "\t"):
            with self.subTest(key=repr(blank)):
                self.assertNotIn("phc_", self.render(blank))
