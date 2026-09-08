"""End-to-end tests for infra/publish.sh.

P0d: one tracker's outage must not freeze the whole site. Before this, the
script ran `python3 scripts/fetch_openai.py` first under an unconditional
`set -e`, so each of the 52 fetcher tracebacks in publish.log aborted the tick
before build.py ran and Anthropic and Google went stale over an OpenAI
problem. The fetch step is now non-fatal; build and check stay fatal and run
BEFORE the rsync, so a broken build leaves the live docroot untouched.

Everything here runs against a throwaway copy of the repo with
AI_RESETS_DEPLOY_TARGET pointed inside a temp directory and
AI_RESETS_FETCH_OPENAI pointed at a stub, so no test touches /srv/ai-resets,
the real data/openai.json, or the network.
"""
import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISH_SH = REPO_ROOT / "infra" / "publish.sh"

# check_site.py asserts the rendered page carries at least one event-item, and
# build.py only renders events from the last 30 days. The seeded OpenAI event
# is therefore dated relative to now: the tracked anthropic/google seeds are
# months old, so a fixed date here would silently rot the suite.
RECENT = datetime.now(timezone.utc) - timedelta(days=1)

SEED_OPENAI = {
    "vendor": "openai",
    "source": {
        "name": "codex-resets.com",
        "url": "https://codex-resets.com/",
        "note": "Tracks @thsottiaux's reset announcements on X.",
    },
    "fetched_at": RECENT.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "events": [
        {
            "id": "cached-1",
            "text": "Cached announcement from the last good fetch.",
            "url": "https://x.com/thsottiaux/status/1",
            "announced_at": RECENT.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "reset",
        }
    ],
}

STUB_HARD_FAIL = "import sys\nprint('stub fetcher: exploding')\nsys.exit(1)\n"
# What the patched fetch_openai.py actually does on a tracker outage.
STUB_SOFT_FAIL = "print('FETCH FAILED codex-resets.com: URLError: down')\n"


def missing_tool():
    for tool in ("bash", "rsync", "python3"):
        if shutil.which(tool) is None:
            return tool
    return None


class PublishScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        absent = missing_tool()
        if absent:
            raise unittest.SkipTest(f"{absent} is not available on this machine")
        if not PUBLISH_SH.is_file():
            raise unittest.SkipTest(f"{PUBLISH_SH} not found")

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

        self.repo = self.tmp / "repo"
        (self.repo / "infra").mkdir(parents=True)
        shutil.copytree(REPO_ROOT / "site", self.repo / "site")
        shutil.copytree(REPO_ROOT / "scripts", self.repo / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO_ROOT / "data", self.repo / "data")
        shutil.copy2(PUBLISH_SH, self.repo / "infra" / "publish.sh")
        # build.py reads the site identity from here at import, so a fake repo
        # without it is a deployment without it. Copy rather than synthesise:
        # the point is to publish with the same configuration the real site has.
        shutil.copy2(REPO_ROOT / "site.config.json", self.repo / "site.config.json")
        (self.repo / "data" / "openai.json").write_text(json.dumps(SEED_OPENAI, indent=2), encoding="utf-8")

        self.deploy = self.tmp / "docroot"
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()

    # ─── Harness ───

    def stub(self, name, body):
        path = self.stubs / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        return path

    def publish(self, fetcher):
        env = dict(os.environ)
        env["AI_RESETS_DEPLOY_TARGET"] = str(self.deploy)
        # Point this at the temp tree, always. Left alone it defaults to
        # /etc/ai-resets/publish.env, and a suite whose result depends on a
        # file outside the repository is a suite that passes on the maintainer's
        # host and fails on a clean runner — which is exactly how the quiet-month
        # test went wrong.
        env["AI_RESETS_PUBLISH_ENV"] = str(self.tmp / "publish.env")
        env["AI_RESETS_FETCH_OPENAI"] = str(fetcher)
        # Belt and braces: a bug in this harness must never reach the live
        # docroot or the live checkout.
        self.assertTrue(str(self.deploy).startswith(str(self.tmp)))
        self.assertNotEqual(self.repo.resolve(), REPO_ROOT)
        return subprocess.run(
            ["bash", str(self.repo / "infra" / "publish.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def deployed_names(self):
        return sorted(p.name for p in self.deploy.iterdir())

    # ─── The site still publishes when the tracker is down ───

    def test_hard_failing_fetcher_still_publishes(self):
        result = self.publish(self.stub("boom", STUB_HARD_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fetch openai FAILED (exit 1)", result.stdout)
        self.assertIn("published site/ ->", result.stdout)
        self.assertIn("index.html", self.deployed_names())
        self.assertIn("data.json", self.deployed_names())

    def test_a_hard_failure_stamps_staleness_into_the_published_data(self):
        # A permanently broken fetcher used to publish an ever-staler OpenAI
        # column with no marker anywhere: exit 0, twenty files deployed, and
        # the only trace one echo into a cron log with MAILTO="".
        self.publish(self.stub("boom", STUB_HARD_FAIL))
        cached = json.loads(
            (self.repo / "data" / "openai.json").read_text(encoding="utf-8")
        )
        self.assertTrue(cached["source"].get("stale_since"), cached["source"])
        self.assertEqual(len(cached["events"]), len(SEED_OPENAI["events"]))

    def test_an_optional_source_that_has_not_shipped_is_skipped_silently(self):
        # Phases land one at a time. A publish script that fails because the
        # next phase's script is not written yet would make every deploy a
        # coordinated one.
        for script in ("claude_probe.py", "fetch_anthropic.py", "discover_posts.py"):
            (self.repo / "scripts" / script).unlink(missing_ok=True)
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fetch anthropic", result.stdout)
        self.assertIn("published site/ ->", result.stdout)

    def test_an_optional_source_that_fails_does_not_take_the_site_down(self):
        (self.repo / "scripts" / "fetch_anthropic.py").write_text(
            "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8"
        )
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fetch anthropic FAILED (exit 3)", result.stdout)
        self.assertIn("published site/ ->", result.stdout)
        self.assertIn("index.html", self.deployed_names())

    def test_an_optional_source_that_works_runs_before_the_build(self):
        (self.repo / "scripts" / "fetch_anthropic.py").write_text(
            "print('anthropic feed refreshed')\n", encoding="utf-8"
        )
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("anthropic feed refreshed", result.stdout)
        self.assertLess(
            result.stdout.index("anthropic feed refreshed"),
            result.stdout.index("[publish] build"),
        )

    def test_a_host_env_file_is_sourced_and_reaches_the_build(self):
        # The whole point of the file: a setting the cron line does not carry.
        # Asserting on the rendered analytics proves it travelled all the way
        # from the file into build.py's environment, not merely that bash read
        # a line.
        (self.tmp / "publish.env").write_text(
            "AI_RESETS_POSTHOG_KEY=phc_from_the_host_env_file\n", encoding="utf-8"
        )
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        analytics = (self.deploy / "analytics.js").read_text(encoding="utf-8")
        self.assertIn("phc_from_the_host_env_file", analytics)
        self.assertIn("// ai-resets-analytics: enabled", analytics)

    def test_no_host_env_file_leaves_analytics_off(self):
        # The default everywhere except the one host that opts in.
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        analytics = (self.deploy / "analytics.js").read_text(encoding="utf-8")
        self.assertIn("// ai-resets-analytics: disabled", analytics)
        self.assertNotIn("phc_", analytics)

    def test_the_env_file_is_never_deployed(self):
        # It lives in /etc, but a host that put one in the repo by mistake must
        # not have it rsynced into a public docroot.
        (self.tmp / "publish.env").write_text("AI_RESETS_POSTHOG_KEY=phc_x\n", encoding="utf-8")
        self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertFalse((self.deploy / "publish.env").exists())

    def test_a_quiet_month_still_publishes(self):
        # Nothing inside build.py's 30-day window: every fetcher is down and
        # every tracked announcement has aged out. This used to abort
        # check_site.py and take the whole tick with it, and it is the exact
        # case the soft-failure contract claims to survive.
        #
        # The vendors themselves stay. An earlier version of this test deleted
        # `data/*.json` outright to reach the empty state, which also deleted
        # the TRACKED seeds — and a fresh clone has those, so the scenario was
        # one that cannot occur. It passed anyway on the maintainer's machine,
        # where the optional discovery steps have network and credentials and
        # quietly wrote the files back; on a clean runner with neither, the
        # page came out with no vendor cards at all and check_site refused it.
        # Ageing the events models the real thing and keeps the cards.
        old = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for seed in (self.repo / "data").glob("*.json"):
            payload = json.loads(seed.read_text(encoding="utf-8"))
            for event in payload.get("events") or []:
                event["announced_at"] = old
            seed.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # No discovery either: with these present they would fetch today's news
        # back and there would be nothing quiet about the month.
        for script in ("fetch_anthropic.py", "discover_posts.py", "claude_probe.py"):
            (self.repo / "scripts" / script).unlink(missing_ok=True)
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("published site/ ->", result.stdout)
        index = (self.deploy / "index.html").read_text(encoding="utf-8")
        self.assertIn("no-recent-events", index)
        self.assertIn("No tracked announcements in the last 30 days", index)

    def test_soft_failing_fetcher_publishes_the_cached_openai_events(self):
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        published = json.loads((self.deploy / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(published["vendors"]["openai"]["events"][0]["id"], "cached-1")
        # The whole point of P0d: the other vendors ship on this tick anyway.
        self.assertIn("anthropic", published["vendors"])
        self.assertIn("google", published["vendors"])

    def test_template_is_excluded_and_stale_files_are_deleted(self):
        self.deploy.mkdir()
        (self.deploy / "removed-last-week.html").write_text("stale", encoding="utf-8")
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("index.template.html", self.deployed_names())
        self.assertNotIn("removed-last-week.html", self.deployed_names())

    # ─── A broken build is never published ───

    def assert_docroot_untouched(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("published site/ ->", result.stdout)
        self.assertEqual(self.deployed_names(), ["sentinel.html"])
        self.assertEqual((self.deploy / "sentinel.html").read_text(encoding="utf-8"), "last good publish")

    def seed_docroot(self):
        self.deploy.mkdir()
        (self.deploy / "sentinel.html").write_text("last good publish", encoding="utf-8")

    def test_failing_build_leaves_the_docroot_untouched(self):
        self.seed_docroot()
        # A data file without a "vendor" key used to abort the build; it is now
        # skipped with a line, which is the right behaviour and no longer a way
        # to force a failure. Break the template instead: build.py requires
        # exactly one STATIC_EVENTS marker pair and raises without it.
        template = self.repo / "site" / "index.template.html"
        template.write_text(
            template.read_text(encoding="utf-8").replace(
                "<!-- STATIC_EVENTS_START -->", "", 1
            ),
            encoding="utf-8",
        )
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertIn("[publish] build", result.stdout)
        # Both echoes are emitted whenever build succeeds, so without this the
        # test would still pass if the CHECK step were what aborted.
        self.assertNotIn("[publish] check", result.stdout)
        self.assert_docroot_untouched(result)

    def test_failing_check_leaves_the_docroot_untouched(self):
        self.seed_docroot()
        # check_site.py requires privacy.html; build.py never touches it, so
        # removing it fails the last gate before the rsync and nothing else.
        (self.repo / "site" / "privacy.html").unlink()
        result = self.publish(self.stub("soft", STUB_SOFT_FAIL))
        self.assertIn("[publish] check", result.stdout)
        self.assert_docroot_untouched(result)


if __name__ == "__main__":
    unittest.main()
