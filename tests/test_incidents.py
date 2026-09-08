"""What an announcement is, and whether it has happened yet.

Every case here is a real post from data/openai.json, data/anthropic.json or
the 2026-09-04 Claude reset, because the failure this module exists to prevent
was caused by treating all of them alike.
"""

import unittest

from datetime import datetime, timedelta, timezone

from scripts.groundtruth import STATUS_OK
from scripts.incidents import (
    scope_covers_us,
    INCIDENT_STATUSES,
    KIND_BANKED,
    KIND_POLICY,
    KIND_RESET,
    KIND_UNKNOWN,
    OBSERVATION_VERDICTS,
    PROBE_STATUS_OK,
    ROLE_CURATED,
    ROLE_OFFICIAL,
    ROLE_STAFF,
    STAGE_FORECAST,
    STAGE_RETROSPECTIVE,
    STATUS_ANNOUNCED,
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_FORECAST,
    STATUS_FORECAST_LAPSED,
    STATUS_HISTORICAL,
    STATUS_OBSERVED,
    STATUS_OBSERVED_PENDING,
    STATUS_OBSERVED_SINGLE,
    STATUS_RETRACTED,
    STATUS_REVERTED,
    _moment,
    announcer_from_url,
    announcer_role,
    classify_text,
    derive_status,
    describe_event,
    event_kind,
    event_stage,
    is_explicit_reset_witness,
    is_reviewed_announcer,
    is_trusted_announcement,
    announcer_handle,
    normalise_handle,
    paragraphs,
    probe_is_healthy,
    scope_covers_us,
    strip_tracking_urls,
)

# 2026-09-05T00:39:25Z — the post that was mailed to seven people as a reset.
BANKED_FORECAST = (
    "Because we are beyond happy to have Astra rolled out today ahead of "
    "schedule and you have been super patient with us (not really, but it’s "
    "ok!)… we will do the full banked reset today too for all Plus, Pro and "
    "Business users. Lands end of day.\n\nHappy Astra day"
)
# 2026-08-31T02:29:25Z — the reset this account observed 2.5 minutes early.
RETROSPECTIVE = (
    "What I wanted to say yesterday is that we hit 25M active users and to "
    "celebrate we have now reset usage for all paid subscriptions for ChatGPT "
    "Work and Codex.\n\nSee you see for more news from The Reset Company. "
    "https://t.co/7Yc5znAJIc"
)
# Both tenses in one post; the past tense is the part a subscriber acts on.
MIXED_TENSE = (
    "We have reset the usage limits for everyone in Codex, which helps us "
    "mitigate the issue the fastest and the team will continue to work on the "
    "underlying cause."
)
MIXED_TENSE_2 = (
    "Hello beautiful people! We have reset usage limits across Codex and "
    "ChatGPT Work. And another one will come later in the day. Rejoice."
)
# A real reset announcement with no reset verb in it at all.
OBLIQUE = (
    "Never slept better and feeling reseted. Brand new me and brand new usage "
    "for all ChatGPT Work and Codex users. Regaining my youth one button press "
    "at a time.\n\nHappy Thursday"
)
LYDIA = (
    "We've just reset weekly limits for everyone on a Claude Max plan.\n\n"
    "With Fable 5.1 out and a long weekend ahead for many of you, we wanted to "
    "keep you building. I'd love to see what you build!"
)


class ClassifyTextTests(unittest.TestCase):
    def test_past_tense_reset_is_retrospective(self):
        self.assertEqual(classify_text(RETROSPECTIVE), STAGE_RETROSPECTIVE)
        self.assertEqual(classify_text(LYDIA), STAGE_RETROSPECTIVE)
        self.assertEqual(
            classify_text("We are reseting usage for all paid users of Codex."),
            STAGE_RETROSPECTIVE,
        )
        self.assertEqual(
            classify_text("5-hour and weekly rate limits have been reset."),
            STAGE_RETROSPECTIVE,
        )

    def test_future_tense_is_a_forecast(self):
        self.assertEqual(classify_text(BANKED_FORECAST), STAGE_FORECAST)
        self.assertEqual(
            classify_text("First one will land in ~ 3 hours."), STAGE_FORECAST
        )
        self.assertEqual(
            classify_text("we've got you covered with a banked reset. Lands by end of day"),
            STAGE_FORECAST,
        )

    def test_past_tense_wins_when_a_post_carries_both(self):
        # The whole precedence rule, on the only two posts that need it.
        self.assertEqual(classify_text(MIXED_TENSE), STAGE_RETROSPECTIVE)
        self.assertEqual(classify_text(MIXED_TENSE_2), STAGE_RETROSPECTIVE)

    def test_a_post_with_no_tense_marker_is_treated_as_already_done(self):
        # The feed already decided this is a reset; with no future language the
        # safe reading is that it has happened, not that it is pending.
        self.assertEqual(classify_text(OBLIQUE), STAGE_RETROSPECTIVE)


class EventKindTests(unittest.TestCase):
    def test_banked_reset_type_is_its_own_kind(self):
        self.assertEqual(
            event_kind({"kind": "reset", "upstream_reset_type": "banked"}), KIND_BANKED
        )

    def test_policy_labels_are_never_resets(self):
        for label in ("boost", "increase", "decrease"):
            self.assertEqual(event_kind({"kind": label}), KIND_POLICY)

    def test_plain_reset_is_a_reset(self):
        self.assertEqual(
            event_kind({"kind": "reset", "upstream_reset_type": "regular"}), KIND_RESET
        )

    def test_missing_or_unrecognised_label_claims_nothing(self):
        # Defaulting to "reset" is the over-claim this module removes.
        self.assertEqual(event_kind({}), KIND_UNKNOWN)
        self.assertEqual(event_kind({"kind": ""}), KIND_UNKNOWN)
        self.assertEqual(event_kind({"kind": "sweepstakes"}), KIND_UNKNOWN)

    def test_a_policy_post_is_never_pending(self):
        self.assertEqual(
            event_stage({"kind": "decrease", "text": "Limits will drop next week."}),
            STAGE_RETROSPECTIVE,
        )


class DescribeEventTests(unittest.TestCase):
    def test_the_banked_forecast_that_was_mailed_as_a_reset(self):
        reading = describe_event(
            {
                "vendor": "openai",
                "kind": "reset",
                "upstream_reset_type": "banked",
                "text": BANKED_FORECAST,
                "announced_at": "2026-09-05T00:39:25Z",
            }
        )
        self.assertTrue(reading["is_forecast"])
        self.assertEqual(
            reading["subject"], "Codex: banked reset announced — not yet landed"
        )
        self.assertEqual(reading["eyebrow"], "OpenAI / Codex · Banked credit · Forecast")
        self.assertIn("not landed yet", reading["headline"])

    def test_a_real_reset_reads_as_one(self):
        reading = describe_event(
            {"vendor": "openai", "kind": "reset", "text": RETROSPECTIVE}
        )
        self.assertFalse(reading["is_forecast"])
        self.assertEqual(reading["subject"], "Codex: usage limits reset")

    def test_a_google_quota_cut_never_says_reset(self):
        reading = describe_event(
            {
                "vendor": "google",
                "kind": "decrease",
                "text": "Gemini CLI stops serving individual accounts.",
            }
        )
        self.assertNotIn("reset", reading["subject"].lower())
        self.assertEqual(reading["subject"], "Gemini: usage limits changed")

    def test_claude_reset_names_the_product_a_subscriber_recognises(self):
        reading = describe_event(
            {"vendor": "anthropic", "kind": "reset", "text": LYDIA}
        )
        self.assertEqual(reading["subject"], "Claude Code: usage limits reset")
        self.assertEqual(reading["org"], "Anthropic")
        self.assertFalse(reading["probed"])  # no Claude probe exists yet

    def test_the_quoted_text_keeps_paragraphs_and_drops_the_shortener(self):
        reading = describe_event(
            {"vendor": "openai", "kind": "reset", "text": RETROSPECTIVE}
        )
        self.assertNotIn("t.co", reading["text"])
        self.assertEqual(len(paragraphs(reading["text"])), 2)


class TextHelpersTests(unittest.TestCase):
    def test_only_the_shortener_is_stripped(self):
        text = "See https://platform.openai.com/docs and https://t.co/abc123"
        cleaned = strip_tracking_urls(text)
        self.assertIn("https://platform.openai.com/docs", cleaned)
        self.assertNotIn("t.co", cleaned)

    def test_paragraphs_survive_and_blank_runs_collapse(self):
        self.assertEqual(paragraphs("one\n\n\n  \n two "), ["one", "two"])
        self.assertEqual(paragraphs(""), [])


# ─── Who is allowed to announce ──────────────────────────────────────────────


class AnnouncerTests(unittest.TestCase):
    def test_the_two_tiers_have_different_roles(self):
        self.assertEqual(announcer_role("ClaudeDevs"), ROLE_OFFICIAL)
        self.assertEqual(announcer_role("@OpenAIDevs"), ROLE_OFFICIAL)
        self.assertEqual(announcer_role("googleaidevs"), ROLE_OFFICIAL)
        # The handle that announced the 2026-09-04 Claude reset. Staff, not the
        # product account, which is the case the allow-list exists for.
        self.assertEqual(announcer_role("lydiahallie"), ROLE_STAFF)
        self.assertEqual(announcer_role("thsottiaux"), ROLE_STAFF)
        self.assertEqual(announcer_role("_mohansolo"), ROLE_STAFF)

    def test_anyone_not_reviewed_is_curated_not_trusted(self):
        for handle in ("some_random_person", "", None, "  ", 42, "claudedevs_fan"):
            with self.subTest(handle=handle):
                self.assertEqual(announcer_role(handle), ROLE_CURATED)
                self.assertFalse(is_reviewed_announcer(handle))

    def test_reviewed_handles_are_reviewed(self):
        self.assertTrue(is_reviewed_announcer("@ClaudeDevs"))
        self.assertTrue(is_reviewed_announcer("LydiaHallie"))

    def test_a_handle_is_folded_for_comparison_only(self):
        self.assertEqual(normalise_handle(" @LydiaHallie "), "lydiahallie")
        self.assertEqual(normalise_handle("_mohansolo"), "_mohansolo")
        # Display keeps X's own casing: a file holding both @ClaudeDevs and
        # @claudedevs for one account reads as two announcers.
        self.assertEqual(announcer_handle(" @ClaudeDevs "), "ClaudeDevs")
        self.assertEqual(announcer_handle("not a handle"), "")
        # Anything that is not a bare handle is refused rather than guessed at.
        for value in ("x.com/lydiahallie", "a b", "", None, "@"):
            with self.subTest(value=value):
                self.assertEqual(normalise_handle(value), "")

    def test_the_announcer_is_read_from_the_post_url(self):
        self.assertEqual(
            announcer_from_url("https://x.com/lydiahallie/status/2095967323412930677"),
            "lydiahallie",
        )
        self.assertEqual(announcer_from_url("https://x.com/ClaudeDevs"), "ClaudeDevs")
        self.assertEqual(announcer_from_url("https://twitter.com/thsottiaux/status/1?s=20"), "thsottiaux")
        self.assertEqual(announcer_from_url("x.com/bcherny/status/1"), "bcherny")

    def test_xs_own_routes_are_not_people(self):
        # https://x.com/i/status/123 must not produce the announcer "i".
        for url in ("https://x.com/i/status/123", "https://x.com/", "", None, "https://x.com"):
            with self.subTest(url=url):
                self.assertEqual(announcer_from_url(url), "")


# ─── Scope ───────────────────────────────────────────────────────────────────


class ScopeTests(unittest.TestCase):
    def test_scopes_that_reach_a_probed_account(self):
        # Every string here is a real claude-resets.com scope value.
        for scope in ("all", "paid plans", "Pro + Max", "Max", "all users"):
            with self.subTest(scope=scope):
                self.assertTrue(scope_covers_us(scope))

    def test_a_scope_naming_only_other_plans_is_out_of_scope(self):
        # Without this the site says "announced, not observed on our accounts",
        # which reads as evidence the vendor over-claimed. It is not evidence of
        # anything: the reset was never aimed at a plan we hold.
        for scope in ("Plus and Business", "Team", "seat-based Enterprise", "free tier"):
            with self.subTest(scope=scope):
                self.assertFalse(scope_covers_us(scope))

    def test_a_scope_naming_a_fraction_of_users_is_out_of_scope(self):
        for scope in ("about 3% of users", "affected users", "some users", "a subset of accounts"):
            with self.subTest(scope=scope):
                self.assertFalse(scope_covers_us(scope))

    def test_a_plan_of_ours_inside_a_longer_list_still_counts(self):
        self.assertTrue(scope_covers_us("all Plus, Pro and Business users"))
        self.assertTrue(scope_covers_us("Pro, Max, Team and seat-based Enterprise"))

    def test_an_unstated_or_unrecognised_scope_defaults_to_covering_us(self):
        # The honest failure is "announced, not observed", which invites a look.
        # Returning False would silently excuse a claim we could not check.
        for scope in ("", None, "   ", "subscribers in the EU"):
            with self.subTest(scope=scope):
                self.assertTrue(scope_covers_us(scope))

    def test_the_plan_list_is_injectable_and_beats_the_other_plan_list(self):
        # The named-plan check runs BEFORE the "plans nobody here holds" list,
        # so adding a tier to `plans` is enough to bring its scopes back in.
        self.assertTrue(scope_covers_us("seat-based Enterprise", plans={"enterprise"}))
        self.assertFalse(scope_covers_us("seat-based Enterprise", plans={"max"}))


# ─── Trust ───────────────────────────────────────────────────────────────────


def announcement(**overrides):
    event = {
        "vendor": "anthropic",
        "kind": "reset",
        "scope": "Max",
        "text": LYDIA,
        "announcer": "lydiahallie",
        "text_verified": True,
        "announced_at": "2026-09-04T20:08:45Z",
        "url": "https://x.com/lydiahallie/status/2095967323412930677",
    }
    event.update(overrides)
    return event


class TrustTests(unittest.TestCase):
    def test_a_verified_post_from_a_reviewed_handle_is_trusted(self):
        self.assertTrue(is_trusted_announcement(announcement()))
        self.assertTrue(is_trusted_announcement(announcement(announcer="ClaudeDevs")))

    def test_a_trackers_paraphrase_is_never_trusted(self):
        # The email blockquotes this text. Quoting a third party's summary as
        # the vendor's words is a fabricated quotation, so text_verified gates
        # trust before the allow-list is even consulted.
        self.assertFalse(is_trusted_announcement(announcement(text_verified=False)))

    def test_a_reply_is_not_an_announcement(self):
        self.assertFalse(is_trusted_announcement(announcement(post_is_reply=True)))

    def test_an_unreviewed_handle_is_not_trusted(self):
        self.assertFalse(is_trusted_announcement(announcement(announcer="a_random_dev")))

    def test_a_non_event_is_not_trusted(self):
        for value in (None, "", [], 3):
            with self.subTest(value=value):
                self.assertFalse(is_trusted_announcement(value))
                self.assertFalse(is_explicit_reset_witness(value))

    def test_a_witness_needs_verified_words_that_say_a_reset_happened(self):
        self.assertTrue(is_explicit_reset_witness(announcement(announcer="a_random_dev")))
        self.assertFalse(
            is_explicit_reset_witness(announcement(text="limits look weird again today"))
        )
        self.assertFalse(is_explicit_reset_witness(announcement(text_verified=False)))
        self.assertFalse(is_explicit_reset_witness(announcement(post_is_reply=True)))


class ProbeHealthTests(unittest.TestCase):
    def test_the_healthy_string_matches_the_probe_reader(self):
        # incidents.py must not import groundtruth (it would pull a state-file
        # reader into a pure module), so the shared string is asserted equal
        # here instead. If groundtruth renames it, this fails rather than
        # silently making every forecast look lapsed.
        self.assertEqual(PROBE_STATUS_OK, STATUS_OK)

    def test_only_an_ok_probe_is_healthy(self):
        self.assertTrue(probe_is_healthy({"status": "ok"}))
        for status in ("blind", "absent", "throttled", "token_stale"):
            with self.subTest(status=status):
                self.assertFalse(probe_is_healthy({"status": status}))
        for value in (None, {}, "ok", []):
            with self.subTest(value=value):
                self.assertFalse(probe_is_healthy(value))


class MomentTests(unittest.TestCase):
    def test_what_a_third_party_feed_can_hand_us(self):
        self.assertEqual(
            _moment("2026-09-04T20:08:45Z"),
            datetime(2026, 9, 4, 20, 8, 45, tzinfo=timezone.utc),
        )
        self.assertEqual(_moment(1757016525), datetime.fromtimestamp(1757016525, timezone.utc))
        aware = datetime(2026, 9, 4, tzinfo=timezone.utc)
        self.assertEqual(_moment(aware), aware)

    def test_anything_without_a_zone_is_refused_rather_than_assumed_utc(self):
        # Guessing a zone is how a reset lands seven hours from where it was.
        for value in (
            "2026-09-04T20:08:45",
            "2026-09-04",
            datetime(2026, 9, 4, 20, 8, 45),
            "whenever",
            "",
            None,
            True,
            [],
        ):
            with self.subTest(value=value):
                self.assertIsNone(_moment(value))


# ─── Incident status ─────────────────────────────────────────────────────────

NOW = "2026-09-04T21:00:00Z"


def observation(**overrides):
    row = {"verdict": "vendor_reset", "observed_at": "2026-09-04T20:11:00Z"}
    row.update(overrides)
    return row


OK_PROBE = {"status": "ok"}
BLIND_PROBE = {"status": "blind"}


class DeriveStatusTests(unittest.TestCase):
    def status(self, event=None, obs=None, probe=None, now=NOW):
        return derive_status(event, obs, probe, now=now)

    def test_every_result_is_a_status_the_plan_names(self):
        cases = [
            (None, None, None),
            (announcement(), None, OK_PROBE),
            (announcement(), observation(), OK_PROBE),
            (announcement(text_verified=False), observation(), OK_PROBE),
            (None, observation(retracted=True), OK_PROBE),
        ]
        for event, obs, probe in cases:
            with self.subTest(event=event, obs=obs):
                self.assertIn(self.status(event, obs, probe), INCIDENT_STATUSES)

    # ── announcement only ──

    def test_a_trusted_retrospective_with_no_observation_is_announced(self):
        self.assertEqual(self.status(announcement(), None, None), STATUS_ANNOUNCED)

    def test_an_unverified_post_is_only_a_candidate(self):
        # A hint without a verified post id never becomes an announcement, and
        # never reaches a subscriber.
        self.assertEqual(self.status(announcement(text_verified=False)), STATUS_CANDIDATE)
        self.assertEqual(self.status(announcement(announcer="a_random_dev")), STATUS_CANDIDATE)
        self.assertEqual(self.status(None, None, None), STATUS_CANDIDATE)

    def test_an_announcement_found_days_late_is_history_and_never_mailed(self):
        event = announcement(first_seen_at="2026-09-07T12:00:00Z")
        self.assertEqual(self.status(event, None, OK_PROBE, now="2026-09-07T12:00:00Z"), STATUS_HISTORICAL)

    def test_an_announcement_found_the_same_hour_is_not_history(self):
        event = announcement(first_seen_at="2026-09-04T20:20:00Z")
        self.assertEqual(self.status(event, None, OK_PROBE), STATUS_ANNOUNCED)

    # ── announcement plus own-account evidence ──

    def test_a_reset_seen_on_our_account_within_six_hours_is_confirmed(self):
        self.assertEqual(self.status(announcement(), observation(), OK_PROBE), STATUS_CONFIRMED)

    def test_an_observation_slightly_ahead_of_its_post_still_confirms(self):
        # Measured: this account saw the 2026-08-30 reset 2.5 to 3.5 minutes
        # BEFORE the announcement was posted.
        early = observation(observed_at="2026-09-04T20:05:45Z")
        self.assertEqual(self.status(announcement(), early, OK_PROBE), STATUS_CONFIRMED)

    def test_an_observation_a_day_away_does_not_confirm(self):
        far = observation(observed_at="2026-09-05T20:08:45Z")
        self.assertEqual(
            self.status(announcement(), far, OK_PROBE, now="2026-09-05T21:00:00Z"),
            STATUS_ANNOUNCED,
        )

    def test_the_wrong_kind_of_evidence_does_not_confirm(self):
        # A banked reset does not clear the window; it drops a credit in the
        # bank. Asking the other question is what made the 2026-09-04 email say
        # "not landed" while quoting "2 banked credits" in the same sentence.
        banked = announcement(kind="reset", upstream_reset_type="banked", text=BANKED_FORECAST)
        self.assertEqual(event_kind(banked), KIND_BANKED)
        self.assertNotEqual(self.status(banked, observation(), OK_PROBE), STATUS_CONFIRMED)

    def test_a_verdict_that_explains_itself_never_confirms_anything(self):
        for verdict in ("natural_expiry", "self_applied", "unresolved"):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    self.status(announcement(), observation(verdict=verdict), OK_PROBE),
                    STATUS_ANNOUNCED,
                )

    # ── forecasts ──

    def test_a_banked_forecast_is_a_forecast_until_it_lands(self):
        event = announcement(
            vendor="openai",
            kind="reset",
            upstream_reset_type="banked",
            text=BANKED_FORECAST,
            announced_at="2026-09-05T00:39:25Z",
        )
        self.assertEqual(self.status(event, None, OK_PROBE, now="2026-09-05T01:00:00Z"), STATUS_FORECAST)

    def test_the_credit_that_landed_confirms_the_banked_forecast(self):
        # The real 2026-09-04 sequence: forecast at 00:39:25Z, credit granted
        # 3 h 41 min later. A forecast has no upper confirmation bound, because
        # "later" is exactly when the probe should see it.
        event = announcement(
            vendor="openai",
            kind="reset",
            upstream_reset_type="banked",
            text=BANKED_FORECAST,
            announced_at="2026-09-05T00:39:25Z",
        )
        landed = observation(verdict="credit_granted", observed_at="2026-09-05T04:20:00Z")
        self.assertEqual(
            self.status(event, landed, OK_PROBE, now="2026-09-05T04:30:00Z"), STATUS_CONFIRMED
        )

    def test_a_forecast_window_that_passes_with_a_healthy_probe_lapses(self):
        event = announcement(text="We will reset limits later today.")
        self.assertEqual(event_stage(event), STAGE_FORECAST)
        self.assertEqual(
            self.status(event, None, OK_PROBE, now="2026-09-06T20:08:45Z"), STATUS_FORECAST_LAPSED
        )

    def test_a_forecast_never_lapses_while_the_probe_is_blind(self):
        # "Forecast lapsed" must never mean "our probe was off".
        event = announcement(text="We will reset limits later today.")
        for probe in (BLIND_PROBE, None, {"status": "throttled"}):
            with self.subTest(probe=probe):
                self.assertEqual(
                    self.status(event, None, probe, now="2026-09-06T20:08:45Z"), STATUS_FORECAST
                )

    def test_a_stated_deadline_beats_the_default_day(self):
        event = announcement(
            text="We will reset limits later today.", forecast_until="2026-09-04T22:00:00Z"
        )
        self.assertEqual(self.status(event, None, OK_PROBE, now="2026-09-04T21:30:00Z"), STATUS_FORECAST)
        self.assertEqual(
            self.status(event, None, OK_PROBE, now="2026-09-04T22:30:00Z"), STATUS_FORECAST_LAPSED
        )

    def test_a_forecast_with_an_unusable_timestamp_stays_a_forecast(self):
        event = announcement(text="We will reset limits later today.", announced_at="soon")
        self.assertEqual(self.status(event, None, OK_PROBE), STATUS_FORECAST)

    # ── probe-first ──

    def test_a_clear_with_no_announcement_waits_out_the_hold(self):
        self.assertEqual(
            self.status(None, observation(), OK_PROBE, now="2026-09-04T21:00:00Z"),
            STATUS_OBSERVED_PENDING,
        )

    def test_after_the_hold_a_lone_clear_says_so_and_mails_nobody(self):
        self.assertEqual(
            self.status(None, observation(), OK_PROBE, now="2026-09-04T23:00:00Z"),
            STATUS_OBSERVED_SINGLE,
        )

    def test_a_verified_reset_post_from_anyone_is_the_second_witness(self):
        # Not a trusted announcement — the handle is not reviewed — but the
        # post exists, was fetched by id, and says a reset happened.
        witness = announcement(announcer="a_random_dev")
        self.assertEqual(
            self.status(witness, observation(), OK_PROBE, now="2026-09-04T23:00:00Z"),
            STATUS_OBSERVED,
        )

    def test_only_an_unexplained_clear_can_start_an_incident(self):
        # docs/robustness-plan.md, "Never incidents": a natural expiry, our own
        # banked credit, a rescale or an unresolved clear are annotations.
        for verdict in ("natural_expiry", "self_applied", "unresolved", "limit_change", "credit_granted"):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    self.status(None, observation(verdict=verdict), OK_PROBE), STATUS_CANDIDATE
                )

    def test_an_observation_with_no_timestamp_cannot_start_an_incident(self):
        self.assertEqual(self.status(None, {"verdict": "vendor_reset"}, OK_PROBE), STATUS_CANDIDATE)

    def test_every_verdict_the_probe_can_emit_is_handled(self):
        for verdict in OBSERVATION_VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertIn(
                    self.status(None, observation(verdict=verdict), OK_PROBE), INCIDENT_STATUSES
                )

    # ── retractions ──

    def test_retracting_our_own_claim_is_a_retraction(self):
        self.assertEqual(
            self.status(None, observation(retracted=True), OK_PROBE), STATUS_RETRACTED
        )

    def test_a_clear_that_reverted_under_a_vendor_announcement_is_a_footnote(self):
        # We never asserted this one ourselves, so there is nothing to retract:
        # it says what happened on THIS account, not that the vendor took
        # anything back.
        self.assertEqual(
            self.status(announcement(), observation(retracted=True), OK_PROBE), STATUS_REVERTED
        )

    def test_a_retraction_outranks_the_forecast_and_hold_windows(self):
        self.assertEqual(
            self.status(None, observation(retracted=True), OK_PROBE, now="2026-09-04T20:12:00Z"),
            STATUS_RETRACTED,
        )

    # ── the clock ──

    def test_the_clock_defaults_to_now_when_none_is_injected(self):
        fresh = observation(
            observed_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        )
        self.assertEqual(derive_status(None, fresh, OK_PROBE), STATUS_OBSERVED_PENDING)

    def test_a_junk_clock_falls_back_to_the_real_one(self):
        self.assertEqual(derive_status(None, None, None, now="not a time"), STATUS_CANDIDATE)


class DescribeEventFieldsTests(unittest.TestCase):
    """P1 additions to describe_event. Every pre-existing key is unchanged."""

    def test_the_meta_line_can_name_the_announcer_and_the_scope(self):
        reading = describe_event(announcement(text_verified=True))
        self.assertEqual(reading["announcer"], "lydiahallie")
        self.assertEqual(reading["announcer_role"], ROLE_STAFF)
        self.assertEqual(reading["scope"], "Max")
        self.assertTrue(reading["covers_us"])
        self.assertTrue(reading["text_verified"])
        self.assertTrue(reading["trusted"])

    def test_the_announcer_falls_back_to_the_post_url(self):
        reading = describe_event(announcement(announcer=None))
        self.assertEqual(reading["announcer"], "lydiahallie")

    def test_an_out_of_scope_post_says_so(self):
        reading = describe_event(announcement(scope="Plus and Business"))
        self.assertFalse(reading["covers_us"])

    def test_the_existing_keys_are_untouched(self):
        reading = describe_event(announcement())
        self.assertEqual(reading["subject"], "Claude Code: usage limits reset")
        self.assertEqual(reading["headline"], "Claude Code: usage limits were reset.")
        self.assertEqual(reading["kind"], KIND_RESET)
        self.assertEqual(reading["stage"], STAGE_RETROSPECTIVE)
        self.assertFalse(reading["probed"])



class ScopeOrderingTests(unittest.TestCase):
    """Which clause wins decides what a reader is told they can trust.

    Both orderings were wrong in opposite directions, and each produced a
    reader-facing sentence contradicted by the very scope it was quoting.
    """

    def test_a_universal_word_cannot_rescue_a_scope_that_names_only_other_plans(self):
        # "all Plus users" is not about a Pro/Max account. Treating it as ours
        # produced "not observed on our accounts", which reads as evidence the
        # vendor over-claimed, from a post that never mentioned our tier.
        for scope in ("all Plus users", "all Business users", "every Team seat"):
            with self.subTest(scope=scope):
                self.assertFalse(scope_covers_us(scope))

    def test_a_named_plan_we_hold_beats_an_eligibility_qualifier(self):
        # "eligible" sits in the partial-scope list, so these were reported out
        # of scope while naming exactly the plan being probed.
        for scope in ("all eligible Max users", "eligible Pro and Max accounts"):
            with self.subTest(scope=scope):
                self.assertTrue(scope_covers_us(scope))

    def test_a_fraction_of_users_is_still_out_of_scope(self):
        for scope in ("about 3% of users", "a small percentage of accounts"):
            with self.subTest(scope=scope):
                self.assertFalse(scope_covers_us(scope))

    def test_an_unstated_scope_invites_a_look_rather_than_excusing_the_vendor(self):
        for scope in ("", None, "   "):
            with self.subTest(scope=scope):
                self.assertTrue(scope_covers_us(scope))


if __name__ == "__main__":
    unittest.main()
