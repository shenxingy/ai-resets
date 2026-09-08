#!/usr/bin/env python3
"""Fast deterministic checks for the generated static site."""

import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ANALYTICS_ENABLED_MARKER = "// ai-resets-analytics: enabled"
ANALYTICS_DISABLED_MARKER = "// ai-resets-analytics: disabled"

SITE = ROOT / "site"

# Imported rather than restated: the one place that decides which verdicts may
# be published is scripts/build.py, and a check that keeps its own copy of that
# list is a check that will one day pass while the page leaks.
from scripts.build import PUBLIC_VERDICTS, VERDICT_LABELS  # noqa: E402


def png_size(path):
    with path.open("rb") as handle:
        signature = handle.read(24)
    if signature[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path.name} is not a PNG")
    return struct.unpack(">II", signature[16:24])


def json_ld(document, element_id=None):
    id_pattern = rf' id="{re.escape(element_id)}"' if element_id else ""
    match = re.search(
        rf'<script type="application/ld\+json"{id_pattern}>\s*(.*?)\s*</script>',
        document,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing JSON-LD block {element_id or ''}")
    return json.loads(match.group(1))


def main():
    required = {
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
    }
    missing = sorted(name for name in required if not (SITE / name).is_file())
    assert not missing, f"missing site assets: {', '.join(missing)}"

    index = (SITE / "index.html").read_text(encoding="utf-8")
    methodology = (SITE / "methodology.html").read_text(encoding="utf-8")
    analytics = (SITE / "analytics.js").read_text(encoding="utf-8")
    assert "og-image.svg" not in index
    assert 'twitter:card" content="summary_large_image"' in index
    assert 'og:image:type" content="image/png"' in index
    assert 'class="vendor static-vendor"' in index
    # Either real rows or the explicit empty state — both prove the events
    # section rendered. Asserting rows unconditionally made the whole publish
    # fail on any tree with no announcement inside build.py's 30-day window
    # (a fresh clone, where data/openai.json is gitignored and absent).
    assert 'class="event-item' in index, "the events section rendered nothing at all"
    assert "Calculating timing pattern" not in index

    # ─── Probe badge ───
    # Every card used to read "Tracking", which let Anthropic and Google borrow
    # the credibility of the one vendor with an own-account probe.
    assert '<span class="signal-status">Tracking</span>' not in index, "the hardcoded Tracking badge is back"
    badges = re.findall(r'<span class="signal-status"([^>]*)>([^<]+)</span>', index)
    assert badges, "no vendor status badge rendered"
    states = set()
    for attrs, label in badges:
        assert label.strip(), "a status badge rendered with no text"
        # Scoped to the badges rather than the whole document on purpose: this
        # check gates the rsync, and a vendor post containing the word
        # "Tracking" would otherwise stop the site from publishing at all.
        assert "Tracking" not in label, f"a status badge still reads {label!r}"
        match = re.search(r'data-probe="([^"]+)"', attrs)
        assert match, f"status badge {label!r} carries no probe state"
        states.add(match.group(1))
    assert states <= {"verified", "offline", "none"}, f"unknown probe badge state: {sorted(states)}"

    # ─── Own-account observations ───
    # A `self_applied` clear or a `credit_granted` row says what the OWNER did
    # with their own account. Neither may reach the published page or feed.
    data = json.loads((SITE / "data.json").read_text(encoding="utf-8"))
    published = 0
    for name, vendor in data.get("vendors", {}).items():
        for observation in vendor.get("observations", []):
            verdict = observation.get("verdict")
            assert verdict in PUBLIC_VERDICTS, f"{name} published verdict {verdict!r}"
            published += 1
    for private in ("self_applied", "credit_granted"):
        assert private not in index, f"{private} reached the rendered page"
    if published:
        assert 'class="observations"' in index, "public observations exist but nothing rendered them"
        assert any(label in index for label in VERDICT_LABELS.values()), "no verdict label rendered"
        assert 'class="observation-note"' in index, "the observations block lost its coverage note"
    assert 'id="ground-truth"' in methodology, "methodology lost the reset-vs-expiry section"
    assert "Reading signal history" not in index
    assert "cookies" in (SITE / "privacy.html").read_text(encoding="utf-8")
    # Analytics is optional: build.py renders the beacon only when a project
    # key is supplied, and writes an inert file otherwise so the three pages
    # that reference it do not 404. Both shapes have to be checked, because
    # asserting the privacy markers unconditionally would fail every build that
    # has analytics switched off, and asserting nothing would let a tracker
    # with a fingerprinting default sail through.
    # The file says which it is on its own line. Inferring it from the presence
    # of some function name is how this check first went wrong: the disabled
    # stub NAMES the tracker in its explanatory comment, so a substring test
    # graded an inert file against the rules for a live one.
    enabled = ANALYTICS_ENABLED_MARKER in analytics
    disabled = ANALYTICS_DISABLED_MARKER in analytics
    assert enabled != disabled, (
        "analytics.js declares neither "
        f"`{ANALYTICS_ENABLED_MARKER}` nor `{ANALYTICS_DISABLED_MARKER}`; "
        "it was not produced by build.py"
    )
    if enabled:
        assert "$geoip_disable: true" in analytics
        assert "$process_person_profile: false" in analytics
        assert "localStorage." not in analytics
    else:
        # Inert means inert: no key, and nothing that could reach the network.
        for forbidden in ("sendBeacon", "fetch(", "XMLHttpRequest", "phc_"):
            assert forbidden not in analytics, (
                f"analytics.js claims to be disabled but contains {forbidden}"
            )
    assert json_ld(index, "site-schema")["@context"] == "https://schema.org"
    assert json_ld(methodology)["@context"] == "https://schema.org"
    assert png_size(SITE / "og-image.png") == (1200, 630)
    assert png_size(SITE / "apple-touch-icon.png") == (180, 180)
    json.loads((SITE / "data.json").read_text(encoding="utf-8"))
    json.loads((SITE / "site.webmanifest").read_text(encoding="utf-8"))
    ET.parse(SITE / "sitemap.xml")
    robots = (SITE / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://resets.alexshen.dev/sitemap.xml" in robots
    print("site checks: ok")


if __name__ == "__main__":
    main()
