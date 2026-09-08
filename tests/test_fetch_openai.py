"""Tests for the codex-resets.com fetcher.

The upstream payload is mirrored verbatim from a live response so the field
names stay honest — in particular there is no top-level `generated_at`, which
is what left `fetched_at` null on the public site since launch.

The soft-failure suite below mirrors the exception types publish.log actually
recorded (52 tracebacks): urllib.error.URLError from DNS and TLS handshake
failures, a bare TimeoutError raised by r.read() after urlopen had already
returned, and a body that does not parse as JSON.
"""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import fetch_openai
from scripts.fetch_openai import to_common_schema

FETCHED_AT = "2026-08-30T09:00:00Z"

RAW = {
    "events": [
        {
            "tweet_id": "2093801758665715784",
            "tweet_url": "https://x.com/thsottiaux/status/2093801758665715784",
            "text": "We are reseting usage for all paid users.",
            "announced_at": "2026-08-29T20:43:34.000Z",
            "reset_type": "full",
            "source": "webhook",
        },
        {
            "tweet_id": "observed-20260825T143200Z",
            "tweet_url": "https://x.com/thsottiaux/status/2092311059197808936",
            "text": "@dtzy_88 Ah yeah, forgot to say",
            "announced_at": "2026-08-25T14:30:00.000Z",
            "reset_type": "full",
            "source": "observed",
        },
    ],
    "watch": {"reset_chance": 75, "forecast_window": "by end of sunday"},
    "stats": {"total": 2},
}


class ToCommonSchemaTest(unittest.TestCase):
    def setUp(self):
        self.data = to_common_schema(RAW, FETCHED_AT)
        self.by_id = {e["id"]: e for e in self.data["events"]}

    def test_fetched_at_is_stamped_not_null(self):
        # Regression: the API has no `generated_at`, so the old
        # raw.get("generated_at") shipped null forever.
        self.assertEqual(self.data["fetched_at"], FETCHED_AT)

    def test_events_are_sorted_oldest_first(self):
        stamps = [e["announced_at"] for e in self.data["events"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_core_fields_are_mapped(self):
        event = self.by_id["2093801758665715784"]
        self.assertEqual(event["url"], RAW["events"][0]["tweet_url"])
        self.assertEqual(event["text"], RAW["events"][0]["text"])
        self.assertEqual(event["kind"], "reset")

    def test_upstream_provenance_is_preserved(self):
        # `source` is the only signal separating a real post from an
        # upstream-estimated "observed" record whose announced_at is a
        # hand-rounded guess and whose url can point at an unrelated tweet.
        self.assertEqual(self.by_id["2093801758665715784"]["upstream_source"], "webhook")
        self.assertEqual(self.by_id["observed-20260825T143200Z"]["upstream_source"], "observed")

    def test_reset_type_is_preserved(self):
        self.assertEqual(self.by_id["2093801758665715784"]["upstream_reset_type"], "full")

    def test_absent_optional_fields_are_omitted_not_nulled(self):
        data = to_common_schema(
            {
                "events": [
                    {
                        "tweet_id": "1",
                        "tweet_url": "https://x.com/a/status/1",
                        "text": "t",
                        "announced_at": "2026-01-01T00:00:00.000Z",
                    }
                ]
            },
            FETCHED_AT,
        )
        self.assertNotIn("upstream_source", data["events"][0])
        self.assertNotIn("upstream_reset_type", data["events"][0])

    def test_empty_feed_is_handled(self):
        data = to_common_schema({}, FETCHED_AT)
        self.assertEqual(data["events"], [])
        self.assertEqual(data["vendor"], "openai")
        self.assertEqual(data["fetched_at"], FETCHED_AT)

    def test_source_attribution_is_retained(self):
        self.assertEqual(self.data["source"]["name"], "codex-resets.com")


# ─── Soft failure: a tracker outage must not freeze the site ───

CACHED = {
    "vendor": "openai",
    "source": {
        "name": "codex-resets.com",
        "url": "https://codex-resets.com/",
        "note": "Tracks @thsottiaux's reset announcements on X.",
    },
    "fetched_at": "2026-08-30T09:00:00Z",
    "events": [
        {
            "id": "1",
            "text": "first",
            "url": "https://x.com/thsottiaux/status/1",
            "announced_at": "2026-08-29T20:43:34.000Z",
            "kind": "reset",
        },
        {
            "id": "2",
            "text": "second",
            "url": "https://x.com/thsottiaux/status/2",
            "announced_at": "2026-08-30T08:00:00.000Z",
            "kind": "reset",
        },
    ],
}

FRESH = {
    "events": [
        {
            "tweet_id": "3",
            "tweet_url": "https://x.com/thsottiaux/status/3",
            "text": "third",
            "announced_at": "2026-09-01T10:00:00.000Z",
        }
    ]
}


class SoftFailureTest(unittest.TestCase):
    """main() must exit 0 and keep the last good file on any upstream failure."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.out_file = self.data_dir / "openai.json"
        patcher = mock.patch.object(fetch_openai, "OUT_FILE", self.out_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_cache(self, payload=None):
        text = json.dumps(CACHED if payload is None else payload, indent=2)
        self.out_file.write_text(text, encoding="utf-8")

    def read_out(self):
        return json.loads(self.out_file.read_text(encoding="utf-8"))

    def run_main(self, *, raises=None, returns=None, now="2026-09-04T12:00:00Z"):
        fetcher = mock.Mock(side_effect=raises) if raises is not None else mock.Mock(return_value=returns)
        buffer = io.StringIO()
        with mock.patch.object(fetch_openai, "fetch", fetcher), mock.patch.object(
            fetch_openai, "utc_now", return_value=now
        ), redirect_stdout(buffer):
            code = fetch_openai.main()
        return code, buffer.getvalue()

    def test_failing_urlopen_keeps_previous_file_and_exits_zero(self):
        # publish.log shape: socket.gaierror wrapped in URLError by urllib.
        self.seed_cache()
        code, output = self.run_main(
            raises=urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        )
        self.assertEqual(code, 0)
        self.assertIn("FETCH FAILED codex-resets.com:", output)
        self.assertIn("keeping 2 cached openai events", output)
        self.assertEqual(self.read_out()["events"], CACHED["events"])

    def test_read_timeout_after_urlopen_is_soft(self):
        # publish.log shape: a BARE TimeoutError from r.read(), raised after
        # urlopen already returned, so it never becomes a URLError. Catching
        # only URLError would have left this one fatal.
        self.seed_cache()
        code, output = self.run_main(raises=TimeoutError("The read operation timed out"))
        self.assertEqual(code, 0)
        self.assertIn("TimeoutError: The read operation timed out", output)
        self.assertEqual(len(self.read_out()["events"]), 2)

    def test_malformed_json_body_is_a_failure_not_garbage(self):
        self.seed_cache()
        code, output = self.run_main(
            raises=json.JSONDecodeError("Expecting value", "<html>503</html>", 0)
        )
        self.assertEqual(code, 0)
        self.assertIn("FETCH FAILED codex-resets.com:", output)
        self.assertEqual(self.read_out()["events"], CACHED["events"])

    def test_stale_since_is_stamped_once_and_never_bumped(self):
        # The age of the outage is the number the site and the owner alert
        # need; re-stamping every five minutes would reset it to zero forever.
        self.seed_cache()
        self.run_main(raises=urllib.error.URLError("down"), now="2026-09-04T12:00:00Z")
        self.assertEqual(self.read_out()["source"]["stale_since"], "2026-09-04T12:00:00Z")
        _code, output = self.run_main(raises=urllib.error.URLError("down"), now="2026-09-04T18:00:00Z")
        self.assertEqual(self.read_out()["source"]["stale_since"], "2026-09-04T12:00:00Z")
        self.assertIn("stale since 2026-09-04T12:00:00Z", output)

    def test_recovery_clears_stale_since(self):
        self.seed_cache()
        self.run_main(raises=urllib.error.URLError("down"))
        self.assertIn("stale_since", self.read_out()["source"])
        code, output = self.run_main(returns=FRESH, now="2026-09-04T19:00:00Z")
        self.assertEqual(code, 0)
        data = self.read_out()
        self.assertNotIn("stale_since", data["source"])
        self.assertEqual(data["fetched_at"], "2026-09-04T19:00:00Z")
        self.assertEqual([e["id"] for e in data["events"]], ["3"])
        self.assertIn("wrote 1 openai events", output)

    def test_missing_cache_is_not_created_on_failure(self):
        # build.py tolerates a missing vendor file (verified: it renders the
        # other vendors and exits 0), so an absent file beats inventing an
        # empty OpenAI history and publishing it as the record.
        code, output = self.run_main(raises=urllib.error.URLError("down"))
        self.assertEqual(code, 0)
        self.assertFalse(self.out_file.exists())
        self.assertIn("no cached data/openai.json", output)

    def test_unusable_event_url_refuses_the_whole_payload(self):
        # 2026-08-27: upstream served a row with tweet_url null, build.py died
        # in html.escape() on 40 consecutive ticks. Refusing the payload keeps
        # the last good file and lets the row return complete once fixed.
        self.seed_cache()
        broken = {"events": [dict(FRESH["events"][0], tweet_url=None)]}
        code, output = self.run_main(returns=broken)
        self.assertEqual(code, 0)
        self.assertIn("unusable tweet_url", output)
        self.assertEqual(self.read_out()["events"], CACHED["events"])

    def test_unparsable_announced_at_is_refused(self):
        # build.py calls datetime.fromisoformat on this field for every event.
        self.seed_cache()
        broken = {"events": [dict(FRESH["events"][0], announced_at="soon")]}
        code, _output = self.run_main(returns=broken)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_out()["events"], CACHED["events"])

    def test_renamed_upstream_field_is_soft(self):
        self.seed_cache()
        row = dict(FRESH["events"][0])
        del row["tweet_id"]
        code, output = self.run_main(returns={"events": [row]})
        self.assertEqual(code, 0)
        self.assertIn("KeyError", output)
        self.assertEqual(self.read_out()["events"], CACHED["events"])

    def test_reshaped_payload_is_soft(self):
        # A bare list, or rows that are ids instead of objects, would raise
        # AttributeError/TypeError — neither of which is a fetch error — and
        # skip the stale_since stamp. Both are normalised into ValueError.
        for payload in ([], {"events": ["2093014447833116908"]}, {"events": {"a": 1}}):
            with self.subTest(payload=payload):
                self.seed_cache()
                code, output = self.run_main(returns=payload)
                self.assertEqual(code, 0)
                self.assertIn("FETCH FAILED codex-resets.com: ValueError", output)
                self.assertEqual(self.read_out()["events"], CACHED["events"])

    def test_empty_feed_does_not_erase_a_good_cache(self):
        # A 200 with an empty body is upstream being broken, not every tracked
        # announcement being retracted at once.
        self.seed_cache()
        code, output = self.run_main(returns={"events": []})
        self.assertEqual(code, 0)
        self.assertIn("upstream returned 0 events, cache holds 2", output)
        self.assertEqual(len(self.read_out()["events"]), 2)

    def test_empty_feed_writes_when_there_is_nothing_to_lose(self):
        code, _output = self.run_main(returns={"events": []}, now="2026-09-04T19:00:00Z")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_out()["events"], [])
        self.assertEqual(self.read_out()["fetched_at"], "2026-09-04T19:00:00Z")

    def test_unparsable_cache_is_left_alone(self):
        self.out_file.write_text("{truncated", encoding="utf-8")
        code, output = self.run_main(raises=urllib.error.URLError("down"))
        self.assertEqual(code, 0)
        self.assertIn("no cached data/openai.json", output)
        self.assertEqual(self.out_file.read_text(encoding="utf-8"), "{truncated")

    def test_write_leaves_no_temp_file_behind(self):
        # build.py globs data/*.json every five minutes; a stray half-written
        # sibling must never be visible to it.
        self.run_main(returns=FRESH)
        self.assertEqual([p.name for p in self.data_dir.iterdir()], ["openai.json"])


class RowsBuildCannotRenderTest(unittest.TestCase):
    """Rows that pass a naive check and then kill build.py on every tick.

    Each case here was measured against the real scripts/build.py, which exits
    1 on it. The 2026-08-27 null-tweet_url incident froze the whole site for
    about 3.3 hours in exactly this shape, so the validator has to reject the
    row shapes rather than the one field that happened to break first.
    """

    def payload(self, **overrides):
        row = {
            "tweet_id": "1",
            "tweet_url": "https://x.com/a/status/1",
            "announced_at": "2026-09-04T12:00:00Z",
            "text": "We have reset usage limits.",
        }
        row.update(overrides)
        return {"events": [row]}

    def assert_refused(self, **overrides):
        with self.assertRaises(ValueError):
            fetch_openai.to_common_schema(self.payload(**overrides), "2026-09-04T12:00:00Z")

    def test_a_non_string_text_is_refused(self):
        # build.py runs re.sub over this value: a non-string raises TypeError
        # there on every five-minute tick.
        for value in (123, {"full": "hi"}, ["hi"], True):
            with self.subTest(text=value):
                self.assert_refused(text=value)

    def test_a_missing_or_empty_text_is_still_accepted(self):
        # Cosmetic upstream quirk, not a reason to freeze the column.
        for value in (None, ""):
            with self.subTest(text=value):
                data = fetch_openai.to_common_schema(
                    self.payload(text=value), "2026-09-04T12:00:00Z"
                )
                self.assertEqual(data["events"][0]["text"], "")

    def test_a_timezone_naive_timestamp_is_refused(self):
        # build.py compares announced_at against an aware `now`; a naive value
        # parses fine and then raises "can't compare offset-naive and
        # offset-aware datetimes" forever.
        self.assert_refused(announced_at="2026-09-04T12:00:00")

    def test_a_date_only_timestamp_is_refused(self):
        self.assert_refused(announced_at="2026-09-04")

    def test_a_well_formed_row_still_passes(self):
        data = fetch_openai.to_common_schema(self.payload(), "2026-09-04T12:00:00Z")
        self.assertEqual(data["events"][0]["id"], "1")


class ShrinkingFeedTest(unittest.TestCase):
    """A truncated feed must not delete published history.

    This is the only failure path in the fetcher that DESTROYS data rather than
    freezing it: a body carrying one event where the cache holds fifty-two used
    to be written straight over the cache, with no failure line, no
    stale_since, and exit 0.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.out = Path(self.tempdir.name) / "openai.json"
        self._original = fetch_openai.OUT_FILE
        fetch_openai.OUT_FILE = self.out
        self.addCleanup(setattr, fetch_openai, "OUT_FILE", self._original)

    def seed(self, count):
        self.out.write_text(
            json.dumps(
                {
                    "vendor": "openai",
                    "source": {"name": "codex-resets.com"},
                    "fetched_at": "2026-09-04T00:00:00Z",
                    "events": [
                        {
                            "id": str(index),
                            "text": "Reset",
                            "url": "https://x.com/a/status/%d" % index,
                            "announced_at": "2026-09-0%dT00:00:00Z" % (index % 9 + 1),
                            "kind": "reset",
                        }
                        for index in range(count)
                    ],
                }
            )
        )

    def run_main(self, upstream_count):
        events = [
            {
                "tweet_id": str(index),
                "tweet_url": "https://x.com/a/status/%d" % index,
                "announced_at": "2026-09-0%dT00:00:00Z" % (index % 9 + 1),
                "text": "Reset",
            }
            for index in range(upstream_count)
        ]
        buffer = io.StringIO()
        with mock.patch.object(fetch_openai, "fetch", return_value={"events": events}):
            with contextlib.redirect_stdout(buffer):
                code = fetch_openai.main([])
        return code, buffer.getvalue(), json.loads(self.out.read_text())

    def test_a_truncated_feed_is_refused_and_the_cache_survives(self):
        self.seed(52)
        code, output, data = self.run_main(1)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["events"]), 52)
        self.assertIn("FETCH FAILED", output)
        self.assertIn("cache holds 52", output)
        self.assertTrue(data["source"]["stale_since"])

    def test_an_empty_feed_is_refused_even_when_the_cache_is_tiny(self):
        self.seed(2)
        code, _output, data = self.run_main(0)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["events"]), 2)

    def test_upstream_removing_one_bad_row_is_accepted_and_reported(self):
        # Upstream has twice deleted a row it should not have published.
        # Refusing those forever would freeze the column on a real correction.
        self.seed(52)
        code, output, data = self.run_main(51)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["events"]), 51)
        self.assertIn("upstream dropped 1 event", output)
        self.assertNotIn("stale_since", data["source"])

    def test_a_growing_feed_is_normal_and_silent(self):
        self.seed(52)
        _code, output, data = self.run_main(53)
        self.assertEqual(len(data["events"]), 53)
        self.assertNotIn("dropped", output)


class StampStaleTest(unittest.TestCase):
    """`--stamp-stale` is how infra/publish.sh marks a fetcher that died hard."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.out = Path(self.tempdir.name) / "openai.json"
        self._original = fetch_openai.OUT_FILE
        fetch_openai.OUT_FILE = self.out
        self.addCleanup(setattr, fetch_openai, "OUT_FILE", self._original)

    def test_it_marks_the_cached_file_without_fetching_anything(self):
        self.out.write_text(
            json.dumps(
                {
                    "vendor": "openai",
                    "source": {"name": "codex-resets.com"},
                    "events": [{"id": "1", "text": "Reset", "url": "u", "announced_at": "2026-09-01T00:00:00Z"}],
                }
            )
        )
        buffer = io.StringIO()
        with mock.patch.object(fetch_openai, "fetch", side_effect=AssertionError("must not fetch")):
            with contextlib.redirect_stdout(buffer):
                code = fetch_openai.main(["--stamp-stale"])
        self.assertEqual(code, 0)
        data = json.loads(self.out.read_text())
        self.assertTrue(data["source"]["stale_since"])
        self.assertEqual(len(data["events"]), 1)


if __name__ == "__main__":
    unittest.main()
