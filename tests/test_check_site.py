#!/usr/bin/env python3
"""Every assertion in `scripts/check_site.py`, proved to actually fire.

check_site is the last gate before `rsync` puts the tree on the public host, and
it had no test file. A gate nobody tests is a gate that can quietly stop
gating — a mistyped attribute name in one of its regexes turns an assertion into
a tautology, the check keeps printing "site checks: ok", and the first person to
notice is a reader of the published page.

So the shape here is one golden site that passes, then one mutation per
assertion that breaks exactly that assertion. A check that cannot be made to
fail is not in this file, and that is the point: if someone weakens an
assertion, its test starts passing a broken page and fails here.

The golden site is written into a temp directory and `check_site.SITE` is
pointed at it. The real `site/` tree is never read and never written: it is the
directory a cron rsyncs to production every five minutes.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import tempfile
import unittest
import zlib
from functools import lru_cache
from pathlib import Path
from unittest import mock

from scripts import check_site
from scripts.build import VERDICT_LABELS


# ─── A golden site ───────────────────────────────────────────────────────────


@lru_cache(maxsize=8)
def png_bytes(width: int, height: int) -> bytes:
    """A real 8-bit greyscale PNG, not just the 24 bytes png_size reads.

    check_site only unpacks the IHDR, so a header-shaped stub would pass — and
    would then be a fixture that the checker accepts but no browser would
    render, which is the wrong thing for a test of a publish gate to normalise.
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    scanlines = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )


VENDOR_LABEL = VERDICT_LABELS["vendor_reset"]

INDEX = f"""<!doctype html>
<html lang="en"><head>
<meta name="twitter:card" content="summary_large_image">
<meta property="og:image" content="https://resets.alexshen.dev/og-image.png">
<meta property="og:image:type" content="image/png">
<script type="application/ld+json" id="site-schema">
{{"@context": "https://schema.org", "@type": "WebSite"}}
</script>
</head><body>
<section class="vendor static-vendor" data-vendor="openai">
  <span class="signal-status" data-probe="verified">Ground truth &middot; verified 3 min ago</span>
  <div class="observations">
    <div class="observation"><span class="verdict">{VENDOR_LABEL}</span></div>
    <p class="observation-note">Coverage: one Pro account, weekly window.</p>
  </div>
</section>
<section class="vendor static-vendor" data-vendor="anthropic">
  <span class="signal-status" data-probe="none">Announcements only</span>
</section>
<ol class="events">
  <li class="event-item" data-id="2095967323412930677">weekly limits reset</li>
</ol>
</body></html>
"""

METHODOLOGY = """<!doctype html>
<html lang="en"><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "TechArticle"}
</script>
</head><body>
<section id="ground-truth"><h2>Reset or scheduled expiry</h2></section>
</body></html>
"""

PRIVACY = "<!doctype html><html><body><p>No cookies are set by this site.</p></body></html>\n"

# What build.py renders when a project key IS supplied. `aiResetTrack` is the
# marker check_site keys on, so the fixture has to carry it or the file would be
# graded against the rules for a disabled one.
ANALYTICS = """// ai-resets-analytics: enabled
window.aiResetTrack = track;
window.posthog && posthog.init("phc_x", {
  $geoip_disable: true,
  $process_person_profile: false,
});
"""

# What it renders when the key is absent, which is what a fork gets.
ANALYTICS_DISABLED = (
    '"use strict";\n'
    "// ai-resets-analytics: disabled\n"
    "// AI_RESETS_POSTHOG_KEY is not set.\n"
)

DATA = {
    "generated_at": "2026-09-06T00:00:00Z",
    "vendors": {
        "openai": {
            "observations": [
                {"verdict": "vendor_reset", "observed_at": "2026-08-30T19:26:00-07:00"},
                {"verdict": "natural_expiry", "observed_at": "2026-08-29T19:26:00-07:00"},
            ]
        },
        "anthropic": {"observations": []},
    },
}

SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://resets.alexshen.dev/</loc></url></urlset>\n"
)

ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://resets.alexshen.dev/sitemap.xml\n"


def golden() -> dict[str, str | bytes]:
    """The smallest tree that satisfies every assertion in check_site.main."""
    return {
        "index.html": INDEX,
        "methodology.html": METHODOLOGY,
        "privacy.html": PRIVACY,
        "analytics.js": ANALYTICS,
        "data.json": json.dumps(DATA, indent=2),
        "site.webmanifest": json.dumps({"name": "AI Reset Watch", "icons": []}),
        "sitemap.xml": SITEMAP,
        "robots.txt": ROBOTS,
        "llms.txt": "# AI Reset Watch\n",
        "favicon.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
        "favicon.ico": b"\x00\x00\x01\x00",
        "favicon-32x32.png": png_bytes(32, 32),
        "og-image.png": png_bytes(1200, 630),
        "apple-touch-icon.png": png_bytes(180, 180),
    }


class SiteCheckCase(unittest.TestCase):
    """Writes a golden site, applies one mutation, runs check_site.main()."""

    def build(self, **changes: str | bytes | None) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="ai-resets-site-"))
        self.addCleanup(_remove_tree, directory)
        files: dict[str, str | bytes | None] = dict(golden())
        files.update(changes)
        for name, body in files.items():
            if body is None:  # a deliberately missing asset
                continue
            path = directory / name
            if isinstance(body, bytes):
                path.write_bytes(body)
            else:
                path.write_text(body, encoding="utf-8")
        return directory

    def run_check(self, **changes: str | bytes | None) -> None:
        directory = self.build(**changes)
        # check_site.main() prints "site checks: ok" on success; forty passes of
        # it would drown the runner's own output.
        with mock.patch.object(check_site, "SITE", directory):
            with contextlib.redirect_stdout(io.StringIO()):
                check_site.main()

    def assert_fires(self, expected: str | None, **changes: str | bytes | None) -> None:
        with self.assertRaises(AssertionError) as caught:
            self.run_check(**changes)
        if expected is not None:
            self.assertIn(expected, str(caught.exception))

    def index_without(self, fragment: str, replacement: str = "") -> str:
        self.assertIn(fragment, INDEX, "the golden index no longer contains this fragment")
        return INDEX.replace(fragment, replacement)


def _remove_tree(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    directory.rmdir()


# ─── The golden site passes ──────────────────────────────────────────────────


class GoldenSiteTests(SiteCheckCase):
    def test_the_golden_site_passes(self):
        # If this ever fails, every negative test below is meaningless: they all
        # prove "one mutation broke it", which says nothing when the unmutated
        # tree was already broken.
        self.run_check()

    def test_an_empty_observation_list_still_passes(self):
        # A fresh clone has no probe export at all. The publish must not depend
        # on the owner's own account having produced an observation this week.
        empty = {"generated_at": DATA["generated_at"], "vendors": {"anthropic": {}}}
        bare = (
            INDEX.replace('class="observations"', 'class="announcements"')
            .replace('class="observation-note"', 'class="source-note"')
            .replace(VENDOR_LABEL, "Announcement only")
        )
        self.run_check(**{"data.json": json.dumps(empty), "index.html": bare})


# ─── Required assets ─────────────────────────────────────────────────────────


class RequiredAssetTests(SiteCheckCase):
    def test_each_required_asset_is_actually_required(self):
        required = [
            "index.html",
            "methodology.html",
            "privacy.html",
            "data.json",
            "robots.txt",
            "sitemap.xml",
            "llms.txt",
            "og-image.png",
            "favicon.svg",
            "favicon.ico",
            "favicon-32x32.png",
            "apple-touch-icon.png",
            "site.webmanifest",
            "analytics.js",
        ]
        for name in required:
            with self.subTest(missing=name):
                self.assert_fires(f"missing site assets: {name}", **{name: None})

    def test_several_missing_assets_are_all_named(self):
        # The operator reading a failed publish should get the whole list, not
        # one file per re-run.
        self.assert_fires("llms.txt, robots.txt", **{"llms.txt": None, "robots.txt": None})


# ─── Social card and page structure ──────────────────────────────────────────


class IndexStructureTests(SiteCheckCase):
    def test_the_old_svg_open_graph_image_is_rejected(self):
        # X and Slack never rendered the SVG card; the PNG replaced it, and the
        # SVG reference coming back is a silent regression to a blank preview.
        self.assert_fires(
            None,
            **{"index.html": INDEX.replace("og-image.png", "og-image.svg")},
        )

    def test_missing_twitter_card_meta_fails(self):
        self.assert_fires(
            None,
            **{"index.html": self.index_without('content="summary_large_image"', 'content="summary"')},
        )

    def test_missing_og_image_type_fails(self):
        self.assert_fires(
            None,
            **{"index.html": self.index_without('<meta property="og:image:type" content="image/png">')},
        )

    def test_missing_static_vendor_markup_fails(self):
        # The server-rendered vendor cards are what a reader with JavaScript off
        # sees; app.js re-renders the same markup on top of them.
        self.assert_fires(
            None,
            **{"index.html": INDEX.replace('class="vendor static-vendor"', 'class="vendor"')},
        )

    def test_no_event_rows_at_all_fails(self):
        self.assert_fires(
            "the events section rendered nothing at all",
            **{"index.html": INDEX.replace('class="event-item"', 'class="event-row"')},
        )

    def test_the_timing_placeholder_must_not_ship(self):
        self.assert_fires(
            None,
            **{"index.html": INDEX.replace("</body>", "<p>Calculating timing pattern</p></body>")},
        )

    def test_the_signal_history_placeholder_must_not_ship(self):
        self.assert_fires(
            None,
            **{"index.html": INDEX.replace("</body>", "<p>Reading signal history</p></body>")},
        )


# ─── Probe badges ────────────────────────────────────────────────────────────


class ProbeBadgeTests(SiteCheckCase):
    def test_the_hardcoded_tracking_badge_is_rejected(self):
        # Every card once read "Tracking", which let Anthropic and Google borrow
        # the credibility of the one vendor with an own-account probe.
        self.assert_fires(
            "the hardcoded Tracking badge is back",
            **{
                "index.html": INDEX.replace(
                    '<span class="signal-status" data-probe="none">Announcements only</span>',
                    '<span class="signal-status">Tracking</span>',
                )
            },
        )

    def test_no_badge_at_all_fails(self):
        self.assert_fires(
            "no vendor status badge rendered",
            **{"index.html": INDEX.replace('class="signal-status"', 'class="signal-label"')},
        )

    def test_a_blank_badge_label_fails(self):
        self.assert_fires(
            "a status badge rendered with no text",
            **{
                "index.html": INDEX.replace(
                    ">Announcements only</span>",
                    "> </span>",
                )
            },
        )

    def test_a_badge_that_still_says_tracking_fails(self):
        self.assert_fires(
            "still reads",
            **{"index.html": INDEX.replace(">Announcements only<", ">Tracking soon<")},
        )

    def test_a_badge_without_a_probe_state_fails(self):
        self.assert_fires(
            "carries no probe state",
            **{"index.html": INDEX.replace(' data-probe="none"', "")},
        )

    def test_an_unknown_probe_state_fails(self):
        self.assert_fires(
            "unknown probe badge state",
            **{"index.html": INDEX.replace('data-probe="none"', 'data-probe="pending"')},
        )

    def test_a_vendor_post_containing_the_word_tracking_still_publishes(self):
        # The Tracking check is scoped to the badges on purpose. This assertion
        # gates the rsync, and a vendor writing "we are tracking the rollout"
        # must not be able to stop the whole site from publishing.
        self.run_check(
            **{
                "index.html": INDEX.replace(
                    "weekly limits reset",
                    "we are tracking the rollout; Tracking continues",
                )
            }
        )


# ─── Own-account observations must not leak ──────────────────────────────────


class ObservationPrivacyTests(SiteCheckCase):
    def test_a_private_verdict_in_the_feed_fails(self):
        for verdict in ("self_applied", "credit_granted"):
            with self.subTest(verdict=verdict):
                leaked = json.loads(json.dumps(DATA))
                leaked["vendors"]["openai"]["observations"][0]["verdict"] = verdict
                self.assert_fires(
                    "published verdict",
                    **{"data.json": json.dumps(leaked)},
                )

    def test_a_private_verdict_in_the_page_fails(self):
        for verdict in ("self_applied", "credit_granted"):
            with self.subTest(verdict=verdict):
                self.assert_fires(
                    "reached the rendered page",
                    **{
                        "index.html": INDEX.replace(
                            "</body>", f'<!-- debug: {verdict} --></body>'
                        )
                    },
                )

    def test_an_unknown_verdict_in_the_feed_fails(self):
        # A whitelist, not a blacklist: a verdict string this build has never
        # reviewed must not reach the feed just because it is not on a bad list.
        leaked = json.loads(json.dumps(DATA))
        leaked["vendors"]["openai"]["observations"][0]["verdict"] = "vendor_reset_v2"
        self.assert_fires("published verdict", **{"data.json": json.dumps(leaked)})

    def test_observations_in_the_feed_but_not_on_the_page_fails(self):
        self.assert_fires(
            "public observations exist but nothing rendered them",
            **{"index.html": INDEX.replace('class="observations"', 'class="observation-list"')},
        )

    def test_no_verdict_label_rendered_fails(self):
        self.assert_fires(
            "no verdict label rendered",
            **{"index.html": INDEX.replace(VENDOR_LABEL, "Something happened")},
        )

    def test_the_coverage_note_is_required(self):
        # The note is what stops "Cleared early · unexplained" from reading as a
        # vendor-wide claim; the row must never render without it.
        self.assert_fires(
            "lost its coverage note",
            **{"index.html": INDEX.replace('class="observation-note"', 'class="note"')},
        )


# ─── Other pages ─────────────────────────────────────────────────────────────


class OtherPageTests(SiteCheckCase):
    def test_methodology_without_the_ground_truth_section_fails(self):
        self.assert_fires(
            "methodology lost the reset-vs-expiry section",
            **{"methodology.html": METHODOLOGY.replace('id="ground-truth"', 'id="how"')},
        )

    def test_privacy_page_must_mention_cookies(self):
        self.assert_fires(
            None,
            **{"privacy.html": PRIVACY.replace("cookies", "trackers")},
        )

    def test_analytics_must_disable_geoip(self):
        self.assert_fires(
            None,
            **{"analytics.js": ANALYTICS.replace("$geoip_disable: true", "$geoip_disable: false")},
        )

    def test_analytics_must_disable_person_profiles(self):
        self.assert_fires(
            None,
            **{
                "analytics.js": ANALYTICS.replace(
                    "$process_person_profile: false", "$process_person_profile: true"
                )
            },
        )

    def test_analytics_must_not_touch_local_storage(self):
        self.assert_fires(
            None,
            **{"analytics.js": ANALYTICS + 'localStorage.setItem("ph", "1");\n'},
        )

    def test_a_site_with_analytics_switched_off_passes(self):
        # The default for anyone who has not supplied their own project key,
        # and therefore the shape most deployments of this repository will
        # have. It must not be treated as a broken tracker.
        self.run_check(**{"analytics.js": ANALYTICS_DISABLED})

    def test_a_disabled_analytics_file_that_still_phones_home_fails(self):
        # The hole the "is it disabled" branch could open: a file with no
        # `aiResetTrack` marker skips the privacy assertions entirely, so
        # something that still reaches the network has to be caught by name.
        for beacon in (
            'navigator.sendBeacon("https://us.i.posthog.com/", "{}");\n',
            'fetch("https://example.com/collect");\n',
        ):
            with self.subTest(beacon=beacon):
                self.assert_fires(
                    "claims to be disabled",
                    **{"analytics.js": ANALYTICS_DISABLED + beacon},
                )

    def test_a_file_declaring_neither_state_is_refused(self):
        # Not a shape build.py produces. Something else wrote it, or an edit
        # dropped the marker, and either way the privacy rules that apply are
        # unknown — which is not the same as satisfied.
        self.assert_fires(
            "declares neither",
            **{"analytics.js": '"use strict";\n// hand-written\n'},
        )

    def test_a_key_left_in_a_disabled_file_fails(self):
        self.assert_fires(
            "claims to be disabled",
            **{"analytics.js": ANALYTICS_DISABLED + 'const k = "phc_leftover";\n'},
        )


# ─── Structured data and binary assets ───────────────────────────────────────


class SchemaAndAssetTests(SiteCheckCase):
    def test_missing_site_schema_block_fails(self):
        self.assert_fires(
            "missing JSON-LD block site-schema",
            **{"index.html": INDEX.replace(' id="site-schema"', ' id="other-schema"')},
        )

    def test_wrong_site_schema_context_fails(self):
        self.assert_fires(
            None,
            **{"index.html": INDEX.replace("https://schema.org", "https://example.test")},
        )

    def test_missing_methodology_schema_block_fails(self):
        self.assert_fires(
            "missing JSON-LD block",
            **{
                "methodology.html": METHODOLOGY.replace(
                    '<script type="application/ld+json">', "<script>"
                )
            },
        )

    def test_og_image_of_the_wrong_size_fails(self):
        self.assert_fires(None, **{"og-image.png": png_bytes(1200, 628)})

    def test_apple_touch_icon_of_the_wrong_size_fails(self):
        self.assert_fires(None, **{"apple-touch-icon.png": png_bytes(192, 192)})

    def test_a_non_png_with_a_png_name_fails(self):
        self.assert_fires(
            "is not a PNG",
            **{"og-image.png": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" + b"\x00" * 24},
        )

    def test_unparsable_data_json_fails(self):
        with self.assertRaises(json.JSONDecodeError):
            self.run_check(**{"data.json": "{ not json"})

    def test_unparsable_webmanifest_fails(self):
        with self.assertRaises(json.JSONDecodeError):
            self.run_check(**{"site.webmanifest": "{ not json"})

    def test_malformed_sitemap_fails(self):
        import xml.etree.ElementTree as ET

        with self.assertRaises(ET.ParseError):
            self.run_check(**{"sitemap.xml": "<urlset><url></urlset>"})

    def test_robots_without_the_sitemap_line_fails(self):
        self.assert_fires(
            None,
            **{"robots.txt": ROBOTS.replace("Sitemap: https://resets.alexshen.dev/sitemap.xml", "")},
        )


if __name__ == "__main__":
    unittest.main()
