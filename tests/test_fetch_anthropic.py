"""Tests for the Anthropic discovery job.

Every upstream payload here is the LIVE claude-resets.com response saved at
tests/fixtures/claude-resets-api.json on 2026-09-07, and every verified post is
the live cdn.syndication.twimg.com response saved at
tests/fixtures/tweet-result.json on 2026-09-06. Nothing in this file touches
the network: both endpoints are unmetered courtesy services, and a unit suite
that calls them is a unit suite that fails when somebody else's server does.

The two facts these tests keep true are the two that cost this project a month
of silent misses:

  * the tracker is not a superset of the truth — it carries 2095967323412930677
    (2026-09-04) and does NOT carry 2094856679250919746 (2026-09-01) — so a
    hand-seeded row must survive every fetch, and
  * the tracker's `note` is its own summary, so a row whose text has not been
    read back from the post is marked text_verified false rather than quoted.
"""

import contextlib
import io
import json
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import fetch_anthropic
from scripts.fetch_anthropic import (
    apply_verification,
    confidence_for,
    merge_events,
    normalise_created_at,
    pending_ids,
    read_post_response,
    to_common_schema,
    verify_ids,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RAW = json.loads((FIXTURES / "claude-resets-api.json").read_text(encoding="utf-8"))
POSTS = {
    key: value
    for key, value in json.loads((FIXTURES / "tweet-result.json").read_text(encoding="utf-8")).items()
    if not key.startswith("_")
}

FETCHED_AT = "2026-09-07T04:00:00Z"

# The 2026-09-01 reset. claude-resets.com does not have it; this project does.
MISSED_SEP_1 = "2094856679250919746"
# The 2026-09-04 reset, announced by a staff account and not by @ClaudeDevs.
MISSED_SEP_4 = "2095967323412930677"


# ─── Mapping the tracker feed ────────────────────────────────────────────────


class ToCommonSchemaTest(unittest.TestCase):
    def setUp(self):
        self.data = to_common_schema(RAW, FETCHED_AT)
        self.by_id = {event["id"]: event for event in self.data["events"]}

    def test_only_the_claude_provider_is_read(self):
        # The same payload carries 51 codex rows that scripts/fetch_openai.py
        # already owns; importing them here would double every Codex event.
        self.assertEqual(len(self.data["events"]), 16)
        self.assertEqual(self.data["vendor"], "anthropic")

    def test_core_fields_are_mapped_from_the_trackers_names(self):
        event = self.by_id[MISSED_SEP_4]
        self.assertEqual(event["announced_at"], "2026-09-04T20:08:45Z")
        self.assertEqual(event["kind"], "reset")
        self.assertEqual(event["scope"], "Max")
        self.assertEqual(event["url"], f"https://x.com/lydiahallie/status/{MISSED_SEP_4}")
        self.assertTrue(event["text"].startswith("Reset weekly limits"))

    def test_the_announcer_comes_from_the_post_url_not_the_provider_account(self):
        # The provider block says account "ClaudeDevs" for every row. Attributing
        # @lydiahallie's post to the product account would put words in the
        # vendor's mouth, and it is exactly why this reset was hard to catch.
        self.assertEqual(self.by_id[MISSED_SEP_4]["announcer"], "lydiahallie")
        self.assertEqual(self.by_id[MISSED_SEP_4]["announcer_role"], "staff")
        self.assertEqual(self.by_id["2044868953206612154"]["announcer"], "ClaudeDevs")
        self.assertEqual(self.by_id["2044868953206612154"]["announcer_role"], "official")

    def test_tracker_text_is_never_marked_verified(self):
        self.assertTrue(all(not e["text_verified"] for e in self.data["events"]))
        self.assertTrue(all(e["text_source"] == "tracker" for e in self.data["events"]))

    def test_the_tracker_does_not_carry_the_2026_09_01_reset(self):
        # The measured reason a tracker cannot be the only discovery path.
        self.assertNotIn(MISSED_SEP_1, self.by_id)

    def test_events_are_sorted_oldest_first(self):
        stamps = [event["announced_at"] for event in self.data["events"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_the_source_note_no_longer_claims_a_weekly_scheduled_search(self):
        note = self.data["source"]["note"]
        self.assertNotIn("weekly scheduled search", note)
        self.assertIn("tweet-result", note)

    def test_the_upstream_detector_status_is_recorded_as_evidence(self):
        # "degraded", with lastSuccessfulCheckAt null: the file-level evidence
        # for describing this feed as hand-curated rather than detected.
        self.assertEqual(self.data["source"]["upstream_detector_status"], "degraded")

    def test_fetched_at_is_stamped_by_us(self):
        self.assertEqual(self.data["fetched_at"], FETCHED_AT)

    def test_a_missing_provider_block_is_refused(self):
        # "Anthropic announced nothing" and "the feed lost its claude key" must
        # not look alike.
        with self.assertRaises(ValueError):
            to_common_schema({"providers": {"codex": {"events": []}}}, FETCHED_AT)

    def test_an_empty_claude_block_maps_to_no_events(self):
        data = to_common_schema({"providers": {"claude": {}}}, FETCHED_AT)
        self.assertEqual(data["events"], [])


class RowsBuildCannotRenderTest(unittest.TestCase):
    """Rows that parse and then kill scripts/build.py on every five-minute tick.

    Measured against the real build.py, which exits 1: it html.escape()s `url`
    and `kind`, re.sub()s `text`, and compares `announced_at` against an aware
    `now`. The 2026-08-27 null-url incident on the OpenAI feed froze the whole
    site for 3.3 hours in exactly this shape.
    """

    def payload(self, **overrides):
        row = {
            "id": "1",
            "url": "https://x.com/ClaudeDevs/status/1",
            "date": "2026-09-04T12:00:00Z",
            "kind": "reset",
            "scope": "all",
            "note": "We have reset usage limits.",
        }
        row.update(overrides)
        return {"providers": {"claude": {"events": [row]}}}

    def assert_refused(self, **overrides):
        with self.assertRaises(ValueError):
            to_common_schema(self.payload(**overrides), FETCHED_AT)

    def test_a_null_or_empty_url_is_refused(self):
        for value in (None, "", "   "):
            with self.subTest(url=value):
                self.assert_refused(url=value)

    def test_a_null_or_empty_id_is_refused(self):
        for value in (None, ""):
            with self.subTest(id=value):
                self.assert_refused(id=value)

    def test_a_timezone_naive_or_date_only_timestamp_is_refused(self):
        self.assert_refused(date="2026-09-04T12:00:00")
        self.assert_refused(date="2026-09-04")
        self.assert_refused(date="soon")
        self.assert_refused(date=None)

    def test_non_string_text_scope_and_kind_are_refused(self):
        for field in ("note", "scope", "kind"):
            for value in (123, {"full": "hi"}, ["hi"]):
                with self.subTest(field=field, value=value):
                    self.assert_refused(**{field: value})

    def test_a_missing_note_stays_cosmetic(self):
        data = to_common_schema(self.payload(note=None), FETCHED_AT)
        self.assertEqual(data["events"][0]["text"], "")

    def test_an_unlabelled_row_claims_nothing(self):
        # incidents.event_kind maps "unknown" to KIND_UNKNOWN, whose subject
        # asserts nothing. Defaulting it to "reset" is the over-claim this
        # whole phase removes.
        data = to_common_schema(self.payload(kind=None), FETCHED_AT)
        self.assertEqual(data["events"][0]["kind"], "unknown")

    def test_a_dropped_upstream_field_raises_keyerror_not_typeerror(self):
        row = dict(self.payload()["providers"]["claude"]["events"][0])
        del row["date"]
        with self.assertRaises(KeyError):
            to_common_schema({"providers": {"claude": {"events": [row]}}}, FETCHED_AT)

    def test_reshaped_payloads_become_valueerror(self):
        for payload in (
            [],
            {"providers": []},
            {"providers": {"claude": {"events": {"a": 1}}}},
            {"providers": {"claude": {"events": ["2094856679250919746"]}}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    to_common_schema(payload, FETCHED_AT)


# ─── Verifying a post id ─────────────────────────────────────────────────────


class ReadPostResponseTest(unittest.TestCase):
    def test_the_live_response_maps_to_four_fields(self):
        fields = read_post_response(POSTS[MISSED_SEP_1])
        self.assertEqual(
            fields["text"],
            "With Fable 5.1 out today, we've also reset 5-hour and weekly limits for all users.",
        )
        self.assertEqual(fields["created_at"], "2026-09-01T18:35:27Z")
        self.assertEqual(fields["screen_name"], "ClaudeDevs")
        self.assertFalse(fields["is_reply"])

    def test_absent_reply_keys_mean_not_a_reply(self):
        # Neither live response carried in_reply_to_* at all.
        self.assertNotIn("in_reply_to_screen_name", POSTS[MISSED_SEP_4])
        self.assertFalse(read_post_response(POSTS[MISSED_SEP_4])["is_reply"])

    def test_a_reply_is_flagged(self):
        raw = dict(POSTS[MISSED_SEP_4], in_reply_to_screen_name="someone")
        self.assertTrue(read_post_response(raw)["is_reply"])

    def test_an_unusable_response_is_a_valueerror(self):
        for raw in (
            "not json",
            {},
            dict(POSTS[MISSED_SEP_1], text=""),
            dict(POSTS[MISSED_SEP_1], text=None),
            dict(POSTS[MISSED_SEP_1], created_at="whenever"),
            dict(POSTS[MISSED_SEP_1], created_at="2026-09-01T18:35:27"),
            dict(POSTS[MISSED_SEP_1], user=None),
            dict(POSTS[MISSED_SEP_1], user={"screen_name": ""}),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    read_post_response(raw)

    def test_created_at_normalisation(self):
        self.assertEqual(normalise_created_at("2026-09-01T18:35:27.000Z"), "2026-09-01T18:35:27Z")
        self.assertEqual(normalise_created_at("2026-09-01T11:35:27-07:00"), "2026-09-01T18:35:27Z")
        for bad in (None, "", "2026-09-01", 5, "2026-09-01T18:35:27"):
            with self.subTest(value=bad):
                self.assertIsNone(normalise_created_at(bad))


class VerifyIdsTest(unittest.TestCase):
    """One id is fetched once, and a dead id stops eating the budget."""

    def calls(self, ids, cache, now="2026-09-07T04:00:00Z", budget=3, fail=()):
        seen = []

        def fetcher(post_id):
            seen.append(post_id)
            if post_id in fail:
                raise urllib.error.URLError("404")
            return POSTS[post_id]

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            updated, spent = verify_ids(ids, cache, now, budget=budget, fetcher=fetcher)
        return updated, spent, seen, buffer.getvalue()

    def test_a_verified_id_is_never_fetched_twice(self):
        cache, spent, seen, _ = self.calls([MISSED_SEP_1], {})
        self.assertEqual(spent, 1)
        self.assertTrue(cache[MISSED_SEP_1]["verified"])
        _cache, spent, seen, _ = self.calls([MISSED_SEP_1], cache)
        self.assertEqual((spent, seen), (0, []))

    def test_the_budget_caps_network_calls_per_run(self):
        _cache, spent, seen, _ = self.calls([MISSED_SEP_1, MISSED_SEP_4], {}, budget=1)
        self.assertEqual(spent, 1)
        self.assertEqual(seen, [MISSED_SEP_1])

    def test_a_failure_is_cached_with_its_attempt_count(self):
        cache, spent, _seen, output = self.calls([MISSED_SEP_1], {}, fail={MISSED_SEP_1})
        self.assertEqual(spent, 1)
        self.assertFalse(cache[MISSED_SEP_1]["verified"])
        self.assertEqual(cache[MISSED_SEP_1]["attempts"], 1)
        self.assertIn("not verified", output)

    def test_a_recent_failure_is_not_retried_on_the_next_tick(self):
        # Five minutes later, which is when the cron runs again.
        cache, _spent, _seen, _ = self.calls([MISSED_SEP_1], {}, fail={MISSED_SEP_1})
        _cache, spent, seen, _ = self.calls(
            [MISSED_SEP_1], cache, now="2026-09-07T04:05:00Z", fail={MISSED_SEP_1}
        )
        self.assertEqual((spent, seen), (0, []))

    def test_a_failure_is_retried_after_the_backoff(self):
        cache, _spent, _seen, _ = self.calls([MISSED_SEP_1], {}, fail={MISSED_SEP_1})
        cache, spent, _seen, _ = self.calls([MISSED_SEP_1], cache, now="2026-09-07T12:00:00Z")
        self.assertEqual(spent, 1)
        self.assertTrue(cache[MISSED_SEP_1]["verified"])

    def test_a_dead_id_stops_being_retried_for_ever(self):
        # A deleted post never resolves; without a ceiling it would consume the
        # per-run budget on every tick and starve real new events.
        cache = {
            MISSED_SEP_1: {
                "verified": False,
                "attempts": fetch_anthropic.VERIFY_MAX_ATTEMPTS,
                "last_attempt_at": "2020-01-01T00:00:00Z",
            }
        }
        _cache, spent, seen, _ = self.calls([MISSED_SEP_1], cache)
        self.assertEqual((spent, seen), (0, []))

    def test_a_cache_entry_with_no_timestamp_is_retried(self):
        cache = {MISSED_SEP_1: {"verified": False, "attempts": 1}}
        _cache, spent, _seen, _ = self.calls([MISSED_SEP_1], cache)
        self.assertEqual(spent, 1)

    def test_repeated_failures_accumulate_towards_the_ceiling(self):
        # The count has to carry across runs, or a dead id is retried for ever
        # at one call per backoff window and the ceiling never bites.
        cache, _spent, _seen, _ = self.calls([MISSED_SEP_1], {}, fail={MISSED_SEP_1})
        cache, _spent, _seen, _ = self.calls(
            [MISSED_SEP_1], cache, now="2026-09-07T12:00:00Z", fail={MISSED_SEP_1}
        )
        self.assertEqual(cache[MISSED_SEP_1]["attempts"], 2)


class ApplyVerificationTest(unittest.TestCase):
    def row(self, **overrides):
        row = {
            "id": MISSED_SEP_4,
            "text": "Reset weekly limits for everyone on a Claude Max plan.",
            "url": f"https://x.com/lydiahallie/status/{MISSED_SEP_4}",
            "announced_at": "2026-09-04T20:08:45Z",
            "announcer": "lydiahallie",
            "announcer_role": "staff",
            "text_verified": False,
            "text_source": "tracker",
            "date_source": "tracker",
            "confidence": "high",
        }
        row.update(overrides)
        return row

    def test_the_trackers_summary_is_replaced_by_the_post(self):
        updated = apply_verification(self.row(), read_post_response(POSTS[MISSED_SEP_4]))
        self.assertTrue(updated["text"].startswith("We've just reset weekly limits"))
        self.assertTrue(updated["text_verified"])
        self.assertEqual(updated["text_source"], "post")
        self.assertEqual(updated["date_source"], "post")
        self.assertFalse(updated["post_is_reply"])

    def test_a_hand_typed_date_becomes_the_posts_own_timestamp(self):
        updated = apply_verification(
            self.row(announced_at="2026-09-04T00:00:00Z", date_source="seed", confidence="approx"),
            read_post_response(POSTS[MISSED_SEP_4]),
        )
        self.assertEqual(updated["announced_at"], "2026-09-04T20:08:45Z")
        self.assertEqual(updated["confidence"], "high")

    def test_a_wrong_author_in_the_feed_is_corrected_and_recorded(self):
        # tweet-result is keyed by post id and names that post's real author, so
        # when the tracker disagrees the tracker is wrong. Recorded rather than
        # smoothed over: an announcer the feed got wrong is precisely what the
        # allow-list has to be told about.
        updated = apply_verification(
            self.row(announcer="ClaudeDevs", announcer_role="official"),
            read_post_response(POSTS[MISSED_SEP_4]),
        )
        self.assertEqual(updated["announcer"], "lydiahallie")
        self.assertEqual(updated["announcer_role"], "staff")
        self.assertEqual(updated["announcer_mismatch"], "ClaudeDevs")
        self.assertEqual(updated["url"], f"https://x.com/lydiahallie/status/{MISSED_SEP_4}")

    def test_confidence_follows_the_timestamps_provenance_only(self):
        self.assertEqual(confidence_for("seed"), "approx")
        self.assertEqual(confidence_for("tracker"), "high")
        self.assertEqual(confidence_for("post"), "high")
        self.assertEqual(confidence_for(None), "approx")


class PendingIdsTest(unittest.TestCase):
    def test_hand_ids_and_verified_rows_are_skipped_newest_first(self):
        events = [
            {"id": "claudedevs-2026-07-10", "text_source": "seed", "announced_at": "2026-07-10T00:00:00Z"},
            {"id": "111", "text_source": "tracker", "announced_at": "2026-01-01T00:00:00Z"},
            {"id": "222", "text_source": "post", "announced_at": "2026-09-04T00:00:00Z"},
            {"id": "333", "text_source": "seed", "announced_at": "2026-09-01T00:00:00Z"},
        ]
        # Newest first: the backlog is history, the next event is what a
        # subscriber is about to be mailed about.
        self.assertEqual(pending_ids(events), ["333", "111"])


# ─── Merge: the seed has to survive every fetch ──────────────────────────────


class MergeEventsTest(unittest.TestCase):
    def seed_row(self, **overrides):
        row = {
            "id": "2072429181565288665",
            "text": "Now that Fable 5 is ready to build (again), we've reset everyone's limits.",
            "url": "https://x.com/ClaudeDevs",
            "announced_at": "2026-07-01T00:00:00Z",
            "kind": "reset",
            "scope": "",
            "announcer": "claudedevs",
            "announcer_role": "official",
            "text_verified": False,
            "text_source": "seed",
            "date_source": "seed",
            "confidence": "approx",
            "origin": "seed",
            "in_tracker": False,
        }
        row.update(overrides)
        return row

    def tracker_row(self, **overrides):
        row = to_common_schema(RAW, FETCHED_AT)["events"]
        by_id = {event["id"]: event for event in row}
        event = dict(by_id["2072429181565288665"])
        event.update(overrides)
        return event

    def test_a_row_the_tracker_never_carried_is_kept(self):
        seed = self.seed_row(id=MISSED_SEP_1, in_tracker=False)
        merged = merge_events([seed], to_common_schema(RAW, FETCHED_AT)["events"])
        by_id = {event["id"]: event for event in merged}
        self.assertIn(MISSED_SEP_1, by_id)
        self.assertFalse(by_id[MISSED_SEP_1]["in_tracker"])

    def test_a_hand_copy_of_the_post_outranks_the_trackers_summary(self):
        merged = merge_events([self.seed_row()], [self.tracker_row()])
        self.assertEqual(merged[0]["text"], self.seed_row()["text"])
        self.assertEqual(merged[0]["text_source"], "seed")

    def test_the_trackers_timestamp_outranks_a_hand_typed_midnight(self):
        merged = merge_events([self.seed_row()], [self.tracker_row()])
        self.assertEqual(merged[0]["announced_at"], "2026-07-01T21:16:35Z")
        self.assertEqual(merged[0]["date_source"], "tracker")
        self.assertEqual(merged[0]["confidence"], "high")

    def test_a_verified_post_is_never_downgraded_by_a_later_fetch(self):
        verified = self.seed_row(
            text="the post's own words",
            text_source="post",
            date_source="post",
            text_verified=True,
            announced_at="2026-07-01T21:16:35Z",
            url="https://x.com/ClaudeDevs/status/2072429181565288665",
        )
        merged = merge_events([verified], [self.tracker_row(note="a summary")])
        self.assertEqual(merged[0]["text"], "the post's own words")
        self.assertTrue(merged[0]["text_verified"])
        self.assertEqual(merged[0]["url"], "https://x.com/ClaudeDevs/status/2072429181565288665")

    def test_an_unverified_row_takes_the_trackers_permalink(self):
        # How a hand row with a bare profile URL gains a real status link.
        merged = merge_events([self.seed_row()], [self.tracker_row()])
        self.assertEqual(
            merged[0]["url"], "https://x.com/ClaudeDevs/status/2072429181565288665"
        )

    def test_an_upstream_correction_to_its_own_summary_still_lands(self):
        cached = merge_events([], [self.tracker_row()])
        merged = merge_events(cached, [self.tracker_row(text="corrected upstream")])
        self.assertEqual(merged[0]["text"], "corrected upstream")

    def test_the_tracker_owns_its_classification(self):
        merged = merge_events(
            [self.seed_row(kind="boost", scope="")],
            [self.tracker_row(kind="reset", scope="all")],
        )
        self.assertEqual(merged[0]["kind"], "reset")
        self.assertEqual(merged[0]["scope"], "all")

    def test_in_tracker_is_recomputed_so_a_dropped_row_is_visible(self):
        cached = [self.seed_row(in_tracker=True)]
        merged = merge_events(cached, [])
        self.assertFalse(merged[0]["in_tracker"])

    def test_rows_with_no_id_are_dropped_rather_than_crashing_the_merge(self):
        merged = merge_events([{"text": "orphan"}, "not a row"], [])
        self.assertEqual(merged, [])

    def test_the_result_is_sorted_oldest_first(self):
        merged = merge_events(
            [self.seed_row(id=MISSED_SEP_1, announced_at="2026-09-01T18:35:27Z")],
            to_common_schema(RAW, FETCHED_AT)["events"],
        )
        stamps = [event["announced_at"] for event in merged]
        self.assertEqual(stamps, sorted(stamps))


# ─── Soft failure: a tracker outage must not freeze the site ─────────────────


SEED = {
    "vendor": "anthropic",
    "source": {"name": "claude-resets.com + verified X posts", "url": "https://claude-resets.com/"},
    "fetched_at": "2026-09-07T00:00:00Z",
    "events": [
        {
            "id": MISSED_SEP_1,
            "text": "With Fable 5.1 out today, we've also reset 5-hour and weekly limits for all users.",
            "url": f"https://x.com/ClaudeDevs/status/{MISSED_SEP_1}",
            "announced_at": "2026-09-01T18:35:27Z",
            "kind": "reset",
            "scope": "all",
            "announcer": "ClaudeDevs",
            "announcer_role": "official",
            "text_verified": True,
            "text_source": "post",
            "date_source": "post",
            "confidence": "high",
            "origin": "seed",
            "in_tracker": False,
        }
    ],
}


class MainTest(unittest.TestCase):
    """main() exits 0 and keeps the last good file on any upstream failure."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.out_file = self.data_dir / "anthropic.json"
        self.posts_file = self.data_dir / "posts.json"
        for name, value in (("OUT_FILE", self.out_file), ("POSTS_FILE", self.posts_file)):
            patcher = mock.patch.object(fetch_anthropic, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, payload=None):
        self.out_file.write_text(json.dumps(SEED if payload is None else payload, indent=2))

    def read_out(self):
        return json.loads(self.out_file.read_text(encoding="utf-8"))

    def run_main(
        self,
        *,
        raises=None,
        returns=RAW,
        argv=("--no-verify",),
        now="2026-09-07T04:00:00Z",
        posts=None,
        post_error=None,
    ):
        fetcher = mock.Mock(side_effect=raises) if raises is not None else mock.Mock(return_value=returns)
        self.post_calls = []

        def post_fetcher(post_id):
            self.post_calls.append(post_id)
            if post_error is not None:
                raise post_error
            return (POSTS if posts is None else posts)[post_id]

        buffer = io.StringIO()
        with mock.patch.object(fetch_anthropic, "fetch", fetcher), mock.patch.object(
            fetch_anthropic, "fetch_post", side_effect=post_fetcher
        ), mock.patch.object(fetch_anthropic, "utc_now", return_value=now), contextlib.redirect_stdout(
            buffer
        ):
            code = fetch_anthropic.main(list(argv))
        return code, buffer.getvalue()

    # ── the happy path ──

    def test_the_seed_and_the_tracker_are_merged_into_one_history(self):
        self.seed()
        code, output = self.run_main()
        self.assertEqual(code, 0)
        data = self.read_out()
        ids = [event["id"] for event in data["events"]]
        # 16 tracker rows + the one seeded row the tracker does not carry.
        self.assertEqual(len(ids), 17)
        self.assertIn(MISSED_SEP_1, ids)
        self.assertIn(MISSED_SEP_4, ids)
        self.assertIn("wrote 17 anthropic events", output)

    def test_the_log_counts_verified_rows_on_disk_not_this_runs_calls(self):
        # --no-verify used to report "0 text-verified" over a file whose rows
        # carried the vendor's own words.
        self.seed()
        _code, output = self.run_main()
        self.assertIn("(1 text-verified,", output)

    def test_a_second_run_leaves_the_tracked_file_byte_identical(self):
        # The tracked seed is also the merge target and this runs from a
        # five-minute cron in the DEPLOYED checkout, so rewriting the file for
        # a new read timestamp alone would leave that checkout permanently
        # dirty and make the next `git pull --ff-only` deploy fail. Nothing a
        # reader would notice changed, so nothing is written.
        self.seed()
        self.run_main()
        first = self.out_file.read_bytes()
        _code, output = self.run_main(now="2026-09-07T05:00:00Z")
        self.assertEqual(self.out_file.read_bytes(), first)
        self.assertIn("unchanged", output)

    def test_a_real_change_is_still_written(self):
        self.seed()
        self.run_main()
        before = self.out_file.read_bytes()
        # One more event from upstream is a change a reader would notice.
        extra = json.loads(json.dumps(RAW))
        events = extra["providers"]["claude"]["events"]
        events.append({**events[0], "id": "2099999999999999999",
                       "date": "2026-09-06T12:00:00Z"})
        self.run_main(returns=extra, now="2026-09-07T06:00:00Z")
        self.assertNotEqual(self.out_file.read_bytes(), before)

    def test_a_first_run_with_no_file_writes_the_tracker_alone(self):
        code, _output = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.read_out()["events"]), 16)

    # ── verification ──

    def test_verification_replaces_the_trackers_summary_with_the_post(self):
        self.seed()
        code, output = self.run_main(argv=("--verify-budget", "1"))
        self.assertEqual(code, 0)
        by_id = {event["id"]: event for event in self.read_out()["events"]}
        # Newest unverified first, so the budget of one lands on the 2026-09-04
        # reset rather than on a row from April.
        self.assertEqual(self.post_calls, [MISSED_SEP_4])
        self.assertTrue(by_id[MISSED_SEP_4]["text"].startswith("We've just reset weekly limits"))
        self.assertTrue(by_id[MISSED_SEP_4]["text_verified"])
        self.assertEqual(by_id[MISSED_SEP_4]["url"], f"https://x.com/lydiahallie/status/{MISSED_SEP_4}")
        self.assertIn("checked 1 post id", output)

    def test_the_verification_cache_means_one_id_is_fetched_once(self):
        self.seed()
        self.run_main(argv=("--verify-budget", "1"))
        cache = json.loads(self.posts_file.read_text(encoding="utf-8"))
        self.assertTrue(cache["posts"][MISSED_SEP_4]["verified"])
        # Second run: the id is applied from the cache and never fetched again,
        # so the courtesy endpoint sees one call per post for ever.
        code, _output = self.run_main(argv=("--verify-budget", "1"), now="2026-09-07T05:00:00Z")
        self.assertEqual(code, 0)
        self.assertNotIn(MISSED_SEP_4, self.post_calls)
        by_id = {event["id"]: event for event in self.read_out()["events"]}
        self.assertTrue(by_id[MISSED_SEP_4]["text_verified"])

    def test_a_verification_failure_keeps_the_event_with_text_verified_false(self):
        # Never drop an event for failing verification: the tracker's text is
        # still the only record that the announcement happened at all.
        self.seed()
        code, output = self.run_main(
            argv=("--verify-budget", "1"), post_error=urllib.error.URLError("cdn down")
        )
        self.assertEqual(code, 0)
        by_id = {event["id"]: event for event in self.read_out()["events"]}
        self.assertFalse(by_id[MISSED_SEP_4]["text_verified"])
        self.assertEqual(len(by_id), 17)
        self.assertIn("not verified", output)

    def test_an_unwritable_posts_cache_never_fails_the_run(self):
        self.seed()
        with mock.patch.object(fetch_anthropic, "write_json", side_effect=self._fail_on_posts):
            code, output = self.run_main(argv=("--verify-budget", "1"))
        self.assertEqual(code, 0)
        self.assertIn("could not write posts.json", output)

    def _fail_on_posts(self, path, payload):
        if path.name == "posts.json":
            raise OSError("read-only file system")
        path.write_text(json.dumps(payload, indent=2))

    # ── soft failure ──

    def test_a_dns_failure_keeps_the_seed_and_exits_zero(self):
        self.seed()
        code, output = self.run_main(
            raises=urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        )
        self.assertEqual(code, 0)
        self.assertIn("FETCH FAILED claude-resets.com:", output)
        self.assertIn("keeping 1 cached anthropic events", output)
        self.assertEqual(self.read_out()["events"], SEED["events"])

    def test_a_read_timeout_after_urlopen_is_soft(self):
        # publish.log shape: a BARE TimeoutError from r.read(), which never
        # becomes a URLError. Catching only URLError would leave this fatal.
        self.seed()
        code, output = self.run_main(raises=TimeoutError("The read operation timed out"))
        self.assertEqual(code, 0)
        self.assertIn("TimeoutError", output)
        self.assertEqual(len(self.read_out()["events"]), 1)

    def test_an_html_error_page_is_soft(self):
        self.seed()
        code, output = self.run_main(
            raises=json.JSONDecodeError("Expecting value", "<html>503</html>", 0)
        )
        self.assertEqual(code, 0)
        self.assertIn("FETCH FAILED", output)
        self.assertEqual(self.read_out()["events"], SEED["events"])

    def test_stale_since_is_stamped_once_and_never_bumped(self):
        self.seed()
        self.run_main(raises=urllib.error.URLError("down"), now="2026-09-07T04:00:00Z")
        self.assertEqual(self.read_out()["source"]["stale_since"], "2026-09-07T04:00:00Z")
        _code, output = self.run_main(raises=urllib.error.URLError("down"), now="2026-09-07T10:00:00Z")
        self.assertEqual(self.read_out()["source"]["stale_since"], "2026-09-07T04:00:00Z")
        self.assertIn("stale since 2026-09-07T04:00:00Z", output)

    def test_recovery_clears_stale_since(self):
        self.seed()
        self.run_main(raises=urllib.error.URLError("down"))
        self.assertIn("stale_since", self.read_out()["source"])
        code, _output = self.run_main(now="2026-09-07T06:00:00Z")
        self.assertEqual(code, 0)
        self.assertNotIn("stale_since", self.read_out()["source"])
        self.assertEqual(self.read_out()["fetched_at"], "2026-09-07T06:00:00Z")

    def test_a_missing_file_is_not_invented_on_failure(self):
        code, output = self.run_main(raises=urllib.error.URLError("down"))
        self.assertEqual(code, 0)
        self.assertFalse(self.out_file.exists())
        self.assertIn("no cached data/anthropic.json", output)

    def test_an_unparsable_cache_is_left_exactly_as_it_is(self):
        self.out_file.write_text("{truncated", encoding="utf-8")
        code, output = self.run_main(raises=urllib.error.URLError("down"))
        self.assertEqual(code, 0)
        self.assertIn("unreadable", output)
        self.assertEqual(self.out_file.read_text(encoding="utf-8"), "{truncated")

    def test_a_source_block_of_the_wrong_shape_is_rebuilt(self):
        self.seed(dict(SEED, source="not an object"))
        code, _output = self.run_main(raises=urllib.error.URLError("down"))
        self.assertEqual(code, 0)
        self.assertTrue(self.read_out()["source"]["stale_since"])

    def test_a_disk_error_while_stamping_is_reported_not_raised(self):
        self.seed()
        with mock.patch.object(fetch_anthropic, "write_json", side_effect=OSError("no space")):
            code, output = self.run_main(raises=urllib.error.URLError("down"))
        self.assertEqual(code, 0)
        self.assertIn("could not stamp stale_since", output)

    def test_stamp_stale_marks_the_file_without_fetching_anything(self):
        self.seed()
        with mock.patch.object(fetch_anthropic, "fetch", side_effect=AssertionError("must not fetch")):
            buffer = io.StringIO()
            with mock.patch.object(
                fetch_anthropic, "utc_now", return_value="2026-09-07T04:00:00Z"
            ), contextlib.redirect_stdout(buffer):
                code = fetch_anthropic.main(["--stamp-stale"])
        self.assertEqual(code, 0)
        self.assertEqual(self.read_out()["source"]["stale_since"], "2026-09-07T04:00:00Z")
        self.assertIn("fetcher did not complete", buffer.getvalue())

    # ── shrinking feed ──

    def test_a_truncated_feed_is_refused_and_the_history_survives(self):
        self.seed()
        self.run_main()  # 17 rows on disk, 16 of them tracked
        one_row = {"providers": {"claude": {"events": RAW["providers"]["claude"]["events"][:1]}}}
        code, output = self.run_main(returns=one_row, now="2026-09-07T05:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("cache holds 16 tracked", output)
        self.assertEqual(len(self.read_out()["events"]), 17)
        self.assertTrue(self.read_out()["source"]["stale_since"])

    def test_an_empty_feed_never_erases_a_good_history(self):
        self.seed()
        self.run_main()
        code, output = self.run_main(
            returns={"providers": {"claude": {"events": []}}}, now="2026-09-07T05:00:00Z"
        )
        self.assertEqual(code, 0)
        self.assertIn("upstream returned 0 events", output)
        self.assertEqual(len(self.read_out()["events"]), 17)

    def test_upstream_removing_one_bad_row_is_accepted_and_reported(self):
        self.seed()
        self.run_main()
        trimmed = {"providers": {"claude": {"events": RAW["providers"]["claude"]["events"][:-1]}}}
        code, output = self.run_main(returns=trimmed, now="2026-09-07T05:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("upstream dropped 1 tracked event", output)
        # Refusing a real correction would freeze the column; the row stays on
        # disk but stops counting as tracked.
        self.assertEqual(len(self.read_out()["events"]), 17)
        self.assertNotIn("stale_since", self.read_out()["source"])

    def test_an_empty_feed_writes_when_there_is_nothing_to_lose(self):
        code, _output = self.run_main(returns={"providers": {"claude": {"events": []}}})
        self.assertEqual(code, 0)
        self.assertEqual(self.read_out()["events"], [])

    def test_a_cached_file_whose_events_are_not_a_list_is_survivable(self):
        # A hand edit that turns `events` into an object must cost the seed
        # rows, not the tick: build.py already tolerates the same shape.
        self.seed(dict(SEED, events={"2094856679250919746": {}}))
        code, _output = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.read_out()["events"]), 16)

    def test_the_write_leaves_no_temp_file_behind(self):
        # build.py globs data/*.json every five minutes; a stray half-written
        # sibling must never be visible to it.
        self.run_main()
        self.assertEqual([path.name for path in self.data_dir.iterdir()], ["anthropic.json"])


class LoadPostsTest(unittest.TestCase):
    def test_a_missing_or_corrupt_cache_costs_reverification_and_nothing_else(self):
        with TemporaryDirectory() as name:
            path = Path(name) / "posts.json"
            with mock.patch.object(fetch_anthropic, "POSTS_FILE", path):
                self.assertEqual(fetch_anthropic.load_posts(), {})
                path.write_text("{truncated")
                self.assertEqual(fetch_anthropic.load_posts(), {})
                path.write_text('{"posts": "not a map"}')
                self.assertEqual(fetch_anthropic.load_posts(), {})
                path.write_text('{"posts": {"1": {"verified": true}}}')
                self.assertEqual(fetch_anthropic.load_posts(), {"1": {"verified": True}})


class WriteJsonTest(unittest.TestCase):
    def test_a_failed_serialisation_leaves_no_half_file_for_build_py(self):
        # build.py globs data/*.json every five minutes and json.loads every
        # hit, so a temp file left behind by a failed write would crash the
        # next tick even though this file was never replaced.
        with TemporaryDirectory() as name:
            target = Path(name) / "anthropic.json"
            with self.assertRaises(TypeError):
                fetch_anthropic.write_json(target, {"events": object()})
            self.assertEqual(list(Path(name).iterdir()), [])


class ParseTimestampTest(unittest.TestCase):
    def test_only_an_aware_iso_string_is_a_timestamp(self):
        self.assertIsNotNone(fetch_anthropic.parse_timestamp("2026-09-04T20:08:45Z"))
        for value in (None, "", 20260904, ["2026-09-04T20:08:45Z"]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    fetch_anthropic.parse_timestamp(value)


class ShortReasonTest(unittest.TestCase):
    def test_a_long_message_is_trimmed_for_one_log_line(self):
        reason = fetch_anthropic.short_reason(ValueError("x" * 400))
        self.assertTrue(reason.startswith("ValueError: "))
        self.assertLess(len(reason), 200)

    def test_an_empty_message_still_names_the_type(self):
        self.assertEqual(fetch_anthropic.short_reason(TimeoutError()), "TimeoutError")


if __name__ == "__main__":
    unittest.main()
