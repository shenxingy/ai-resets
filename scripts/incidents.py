#!/usr/bin/env python3
"""Reading a vendor announcement: what kind of thing is it, and did it happen yet.

Every announcement used to reach subscribers as "New {vendor} reset update",
which is how seven people were told on 2026-09-04 at 5:47 PM PDT that a Codex
reset had happened when the post they were being sent said it would "land end
of day" — and it still had not landed on this account four hours later.

The distinction this module draws is the one the old subject line erased:

  retrospective  the vendor says it already happened
  forecast       the vendor says it is going to happen
  policy         the limits themselves changed; nothing was reset

Precedence matters and is decided by real posts, not by taste. Two of the 52
tracked Codex posts carry both tenses at once — "We have reset the usage
limits for everyone in Codex, ... and the team will continue to work on the
underlying cause" (1995988609896513743) and "We have reset usage limits across
Codex and ChatGPT Work. And another one will come later in the day"
(2075641131002700120). Both are retrospective: something already happened, and
that is the part a subscriber acts on. So an explicit past-tense reset claim
always wins over future-tense language in the same post.

The kind itself is NOT re-derived from the text. Upstream classifiers already
label these feeds, and vendor announcements are not written to be parsed —
"Never slept better and feeling reseted. Brand new me and brand new usage for
all ChatGPT Work and Codex users" (2093014447833116908) is a real reset
announcement with no reset verb in it. This module trusts the feed's `kind`
and `upstream_reset_type` for WHAT it is, and reads the text only for WHEN.
Deciding whether a post is trustworthy at all belongs to the discovery layer,
which does not exist yet (see docs/robustness-plan.md, P1).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

# ─── Vendor identity ─────────────────────────────────────────────────────────

VENDOR_ORG = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
}
VENDOR_PRODUCT = {
    "anthropic": "Claude Code",
    "openai": "Codex",
    "google": "Gemini",
}

# The only vendor with a running ground-truth probe. The Claude probe is P2 and
# Antigravity is P6; until they exist the email must say so rather than imply
# that silence means nothing happened.
PROBED_VENDORS = ("openai",)


def vendor_org(vendor: str) -> str:
    return VENDOR_ORG.get(vendor, vendor.title())


def vendor_product(vendor: str) -> str:
    return VENDOR_PRODUCT.get(vendor, vendor.title())


def vendor_label(vendor: str) -> str:
    return f"{vendor_org(vendor)} / {vendor_product(vendor)}"


# ─── Reading the text ────────────────────────────────────────────────────────

# "We have reset", "we've reset", "we are reseting" (their spelling), "we have
# now reset", "we just reset". The optional adverb slot is deliberately narrow:
# widening it to "any word" would swallow "we will do the full banked reset".
RETROSPECTIVE_RE = re.compile(
    r"\bwe(?:'ve|’ve|\s+have|\s+are|\s+just)?\s+(?:now\s+|just\s+|also\s+)*"
    r"reset(?:ting|ing|ed)?\b"
    r"|\b(?:have|has|had|were|was)\s+(?:now\s+|just\s+)?(?:been\s+)?reset\b"
    r"|\breset\s+(?:has|have)\s+landed\b",
    re.IGNORECASE,
)

FORECAST_RE = re.compile(
    r"\bwill\b|\blands?\b|\blanding\b|\bend of day\b|\bshortly\b|\bsoon\b"
    r"|\bin\s*~?\s*\d+\s*(?:hour|hr|minute|min)|\blater (?:today|in the day)\b"
    r"|\bcoming (?:up|later)\b",
    re.IGNORECASE,
)

# Kinds the feeds use for things that are not resets at all.
POLICY_KINDS = {"boost", "increase", "decrease", "policy"}

STAGE_RETROSPECTIVE = "retrospective"
STAGE_FORECAST = "forecast"

KIND_RESET = "reset"
KIND_BANKED = "banked"
KIND_POLICY = "policy"
# A feed row whose kind we do not recognise. It gets a subject that claims
# nothing: defaulting an unlabelled post to "reset" would be the same
# over-claim this module exists to remove.
KIND_UNKNOWN = "unknown"


def classify_text(text: str) -> str:
    """When does this post say the thing happens? Past tense wins ties."""
    body = text or ""
    if RETROSPECTIVE_RE.search(body):
        return STAGE_RETROSPECTIVE
    if FORECAST_RE.search(body):
        return STAGE_FORECAST
    return STAGE_RETROSPECTIVE


def event_kind(event: dict[str, Any]) -> str:
    """reset | banked | policy | unknown, from the feed's own labels."""
    label = str(event.get("kind", "")).strip().lower()
    if label in POLICY_KINDS:
        return KIND_POLICY
    if label not in ("reset", ""):
        return KIND_UNKNOWN
    if str(event.get("upstream_reset_type", "")).lower() == "banked":
        return KIND_BANKED
    if not label:
        return KIND_UNKNOWN
    return KIND_RESET


def event_stage(event: dict[str, Any]) -> str:
    if event_kind(event) == KIND_POLICY:
        # A policy change is in force when it is announced; there is nothing
        # pending to observe.
        return STAGE_RETROSPECTIVE
    return classify_text(str(event.get("text", "")))


TCO_RE = re.compile(r"https?://t\.co/\S+")


def strip_tracking_urls(text: str) -> str:
    """Drop t.co redirectors, keep everything else.

    The site strips every URL from the card text, which is right for a dense
    grid and wrong for an email: a subscriber reading a policy post needs the
    documentation link the vendor put in it. Only the shortener — which carries
    no information and looks like tracking to a mail client — is removed.
    """
    return re.sub(r"[ \t]{2,}", " ", TCO_RE.sub("", text or "")).strip()


def paragraphs(text: str) -> list[str]:
    """Split on blank lines, preserving the author's breaks.

    The old email rendered the whole post inside one <p>, so every paragraph
    break in a long announcement collapsed into a run-on sentence.
    """
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text or "")]
    return [block for block in blocks if block]


# ─── What the subscriber is told this is ─────────────────────────────────────

_HEADLINES = {
    (KIND_RESET, STAGE_RETROSPECTIVE): "{product}: usage limits were reset.",
    (KIND_RESET, STAGE_FORECAST): "{product}: a reset was announced, and has not landed yet.",
    (KIND_BANKED, STAGE_RETROSPECTIVE): "{product}: a banked reset credit was granted.",
    (KIND_BANKED, STAGE_FORECAST): "{product}: a banked reset was announced, and has not landed yet.",
    (KIND_POLICY, STAGE_RETROSPECTIVE): "{product}: the usage limits themselves changed.",
    (KIND_POLICY, STAGE_FORECAST): "{product}: a change to the usage limits was announced.",
    (KIND_UNKNOWN, STAGE_RETROSPECTIVE): "{product}: the vendor posted a usage-limits update.",
    (KIND_UNKNOWN, STAGE_FORECAST): "{product}: the vendor announced a usage-limits update.",
}

_SUBJECTS = {
    (KIND_RESET, STAGE_RETROSPECTIVE): "{product}: usage limits reset",
    (KIND_RESET, STAGE_FORECAST): "{product}: reset announced — not yet landed",
    (KIND_BANKED, STAGE_RETROSPECTIVE): "{product}: banked reset credit granted",
    (KIND_BANKED, STAGE_FORECAST): "{product}: banked reset announced — not yet landed",
    # Used when our own probe has already seen the forecast thing happen, so
    # the subject cannot contradict the sentence below it.
    (KIND_BANKED, "landed"): "{product}: the announced banked reset landed",
    (KIND_RESET, "landed"): "{product}: the announced reset landed",
    # Policy posts must never carry the word "reset": a Google quota CUT went
    # out as a "reset update" under the old single subject line.
    (KIND_POLICY, STAGE_RETROSPECTIVE): "{product}: usage limits changed",
    (KIND_POLICY, STAGE_FORECAST): "{product}: usage limit change announced",
    (KIND_UNKNOWN, STAGE_RETROSPECTIVE): "{product}: usage limits update",
    (KIND_UNKNOWN, STAGE_FORECAST): "{product}: usage limits update announced",
}

_KIND_WORDS = {
    KIND_RESET: "Reset",
    KIND_BANKED: "Banked credit",
    KIND_POLICY: "Policy change",
    KIND_UNKNOWN: "Update",
}
_STAGE_WORDS = {
    STAGE_RETROSPECTIVE: "Announced",
    STAGE_FORECAST: "Forecast",
}


def describe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Everything the email needs about an announcement, as plain fields.

    Nothing here is free text derived from the post: the subject and headline
    are looked up from (kind, stage) so a malformed or hostile announcement can
    never write the subject line. The vendor's own words appear only inside the
    quoted block, escaped.
    """
    vendor = str(event.get("vendor", ""))
    kind = event_kind(event)
    stage = event_stage(event)
    product = vendor_product(vendor)
    # The announcer is read from the post URL rather than the feed's
    # provider-level account: claude-resets.com files the 2026-09-04 reset under
    # provider account ClaudeDevs while the URL is x.com/lydiahallie/status/...,
    # and the meta line has to name the person who actually posted it.
    announcer = announcer_handle(event.get("announcer")) or announcer_from_url(
        event.get("url")
    )
    scope = str(event.get("scope") or "")
    return {
        "vendor": vendor,
        "org": vendor_org(vendor),
        "product": product,
        "kind": kind,
        "stage": stage,
        "is_forecast": stage == STAGE_FORECAST,
        "subject": _SUBJECTS[(kind, stage)].format(product=product),
        "headline": _HEADLINES[(kind, stage)].format(product=product),
        "eyebrow": " · ".join(
            (vendor_label(vendor), _KIND_WORDS[kind], _STAGE_WORDS[stage])
        ),
        "announced_at": event.get("announced_at"),
        "text": strip_tracking_urls(str(event.get("text", ""))),
        "url": event.get("url"),
        "probed": vendor in PROBED_VENDORS,
        # Added in P1. Every key above is unchanged; these are additive so the
        # notifier's six-block email can name who said it and to whom it
        # applied without re-deriving either from the post text.
        "announcer": announcer,
        "announcer_role": announcer_role(announcer),
        "scope": scope,
        "covers_us": scope_covers_us(scope),
        "text_verified": bool(event.get("text_verified")),
        "trusted": is_trusted_announcement(event),
    }


def landed_subject(kind: str, product: str, fallback: str) -> str:
    """The subject for a forecast our own probe has since seen happen."""
    template = _SUBJECTS.get((kind, "landed"))
    return template.format(product=product) if template else fallback


# ─── Who is allowed to announce ──────────────────────────────────────────────
#
# A reset is something that happens to accounts; an announcement is somebody
# saying so. Before P1 the site trusted whichever handle a tracker happened to
# attach to a row, which is how the 2026-09-04 reset — announced by a staff
# member, not by the product account — sat unnoticed while the 2026-09-01 one
# from @ClaudeDevs was missed entirely. The list below is the reviewed answer
# to "whose word do we repeat", and adding a handle to it is a reviewed commit,
# not a runtime decision (docs/robustness-plan.md, "Announcement witnesses").

ROLE_OFFICIAL = "official"  # the vendor's own product account
ROLE_STAFF = "staff"  # a named employee with at least one evidenced reset post
ROLE_CURATED = "curated"  # nobody we have reviewed; only a tracker's word

# Tier A: vendor product accounts.
TIER_A_OFFICIAL = frozenset({"claudedevs", "openaidevs", "googleaidevs"})

# Tier B: staff who have each posted a reset that this project has a record of.
# @lydiahallie announced 2095967323412930677 (2026-09-04) and @thsottiaux the
# 2026-08-30 reset this account measured 2.5 to 3.5 minutes before the post.
TIER_B_STAFF = frozenset(
    {
        "lydiahallie",
        "trq212",
        "bcherny",
        "thsottiaux",
        "embirico",
        "_mohansolo",
        "officiallogank",
    }
)

# Path segments that are X's own routes rather than a person's handle, so
# https://x.com/i/status/123 does not become the announcer "i".
_RESERVED_HANDLE_SEGMENTS = frozenset(
    {"i", "home", "intent", "search", "hashtag", "status", "notifications", "messages"}
)


def announcer_handle(value: Any) -> str:
    """`@ClaudeDevs ` -> `ClaudeDevs`. Anything that is not a bare handle is ``.

    Case is PRESERVED here and folded only for comparison. The site and the
    email print this string, and X shows @ClaudeDevs, not @claudedevs; a file
    holding both spellings for one account — which is what happens when a
    verified post supplies the author for some rows and a lowercased URL for
    the rest — reads as two announcers.
    """
    handle = str(value or "").strip().lstrip("@").strip()
    return handle if handle.replace("_", "").isalnum() else ""


def normalise_handle(value: Any) -> str:
    """The comparison form: `@LydiaHallie ` -> `lydiahallie`."""
    return announcer_handle(value).lower()


def announcer_from_url(url: Any) -> str:
    """The handle in a post URL, or `` when there is not one.

    claude-resets.com attributes a row with its `account` field AND with the
    URL, and the two disagree: the provider block says ClaudeDevs while the
    2026-09-04 row's URL is x.com/lydiahallie/status/2095967323412930677.
    Attributing that post to the product account would put words in the
    vendor's mouth, so the URL — which names the author of that exact post —
    wins over the feed's provider-level account.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text if "//" in text else f"//{text}")
    except ValueError:
        # urlsplit raises on a malformed IPv6 literal. This function is called
        # from describe_event, which runs inside the notifier's loop over
        # pending deliveries BEFORE the per-send try/except, so one bad url in
        # one feed row would abort the whole run for every remaining
        # subscriber. Every other helper here swallows bad input; so does this.
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments or segments[0].lower() in _RESERVED_HANDLE_SEGMENTS:
        return ""
    return announcer_handle(segments[0])


def announcer_role(handle: Any) -> str:
    """official | staff | curated. Never raises; `` is `curated`."""
    name = normalise_handle(handle)
    if name in TIER_A_OFFICIAL:
        return ROLE_OFFICIAL
    if name in TIER_B_STAFF:
        return ROLE_STAFF
    return ROLE_CURATED


def is_reviewed_announcer(handle: Any) -> bool:
    return announcer_role(handle) in (ROLE_OFFICIAL, ROLE_STAFF)


# ─── Does this announcement's scope reach our accounts ───────────────────────
#
# Without this, a post scoped to "Plus and Business" or to "about 3% of users"
# produced the sentence "not observed on our accounts", which reads as evidence
# that the vendor over-claimed. It is not evidence of anything: the reset was
# never aimed at a plan we hold. Both scope strings below are real
# claude-resets.com rows ("affected users", 2067802163498352929).

# What the probed accounts actually hold. Two Max 20x accounts on one operator
# machine for Claude; the Codex probe's account carries Pro.
OUR_PLANS = frozenset({"max", "pro"})

# Checked FIRST: a scope naming a fraction of the user base tells us nothing
# about whether this account was in it, so it can never be read as "not seen".
PARTIAL_SCOPE_RE = re.compile(
    r"\d+\s*%|\baffected\b|\bimpacted\b|\bsome\b|\bsubset\b|\bcertain\b|\beligible\b"
    # A scope can name a fraction without naming a number: @ClaudeDevs'
    # 2026-06-19 post read "about 3% of Max and Pro users", but "a small
    # percentage of accounts" is the same claim with the digit left out.
    r"|\bpercentage\b|\bfraction\b|\bportion\b|\bhandful\b|\bminority\b",
    re.IGNORECASE,
)
UNIVERSAL_SCOPE_RE = re.compile(
    r"\b(?:all|every|everyone|everybody)\b|\bpaid\b|\bsubscribers?\b",
    re.IGNORECASE,
)
# Plans nobody on this machine holds. Only consulted when no plan of ours is
# named, so "all Plus, Pro and Business users" still covers us.
OTHER_PLAN_RE = re.compile(
    r"\b(?:plus|business|team|enterprise|free|edu|education|api)\b", re.IGNORECASE
)


def scope_covers_us(scope: Any, plans: Iterable[str] = OUR_PLANS) -> bool:
    """Could a reset with this stated scope have landed on a probed account?

    An unstated or unrecognised scope returns True on purpose: the honest
    failure is "announced, not observed on our accounts", which invites a look.
    Returning False would silently excuse the vendor from a claim we simply
    could not check.
    """
    text = str(scope or "").strip()
    if not text:
        return True
    # A NAMED plan we hold settles it, before anything else gets a say.
    # "all eligible Max users" was reading as out of scope because `eligible`
    # is in the partial-scope list, so the site said "not observable: scoped
    # to all eligible Max users" — contradicted by the very string it quoted.
    for plan in plans:
        if re.search(rf"\b{re.escape(str(plan))}\b", text, re.IGNORECASE):
            return True
    if PARTIAL_SCOPE_RE.search(text):
        return False
    # A scope naming ONLY plans nobody here holds is out of scope even when it
    # also carries a universal word. "all Plus users" is not about us, and
    # letting `all` rescue it produced "not observed on our accounts" — which
    # reads as evidence the vendor over-claimed, from a post that never
    # mentioned our tier.
    if OTHER_PLAN_RE.search(text):
        return False
    return True


# ─── Incident status ─────────────────────────────────────────────────────────

STATUS_CANDIDATE = "candidate"
STATUS_ANNOUNCED = "announced"
STATUS_CONFIRMED = "confirmed"
STATUS_FORECAST = "forecast"
STATUS_FORECAST_LAPSED = "forecast_lapsed"
STATUS_OBSERVED_PENDING = "observed_pending"
STATUS_OBSERVED = "observed"
STATUS_OBSERVED_SINGLE = "observed_single"
STATUS_RETRACTED = "retracted"
STATUS_REVERTED = "reverted_on_our_account"
STATUS_HISTORICAL = "historical"

INCIDENT_STATUSES = (
    STATUS_CANDIDATE,
    STATUS_ANNOUNCED,
    STATUS_CONFIRMED,
    STATUS_FORECAST,
    STATUS_FORECAST_LAPSED,
    STATUS_OBSERVED_PENDING,
    STATUS_OBSERVED,
    STATUS_OBSERVED_SINGLE,
    STATUS_RETRACTED,
    STATUS_REVERTED,
    STATUS_HISTORICAL,
)

# The probe's public verdicts (SHARED VOCABULARY). `self_applied` says what the
# OWNER did with their own account and is never published, but it is named here
# so a caller passing it gets the same "this is not vendor evidence" treatment
# as a natural expiry rather than an unrecognised-string surprise.
VERDICT_VENDOR_RESET = "vendor_reset"
VERDICT_NATURAL_EXPIRY = "natural_expiry"
VERDICT_SELF_APPLIED = "self_applied"
VERDICT_UNRESOLVED = "unresolved"
VERDICT_LIMIT_CHANGE = "limit_change"
VERDICT_CREDIT_GRANTED = "credit_granted"

OBSERVATION_VERDICTS = (
    VERDICT_NATURAL_EXPIRY,
    VERDICT_SELF_APPLIED,
    VERDICT_VENDOR_RESET,
    VERDICT_UNRESOLVED,
    VERDICT_LIMIT_CHANGE,
    VERDICT_CREDIT_GRANTED,
)

# Which own-account verdict corroborates which kind of announcement. This is
# the 2026-09-05 production finding turned into a table: the Sep 4 banked reset
# landed as `credit_granted` and the window correctly stayed at 100%, so asking
# "did the weekly window clear" said "not landed" while the same email quoted
# "2 banked credits". A banked reset is confirmed by a credit, a reset by a
# clear, a policy change by a rescale — never by each other.
CONFIRMING_VERDICTS: dict[str, tuple[str, ...]] = {
    KIND_RESET: (VERDICT_VENDOR_RESET,),
    KIND_BANKED: (VERDICT_CREDIT_GRANTED,),
    KIND_POLICY: (VERDICT_LIMIT_CHANGE,),
    # An unlabelled post claims nothing, so any vendor-side verdict corroborates
    # it; it still never becomes an incident on its own.
    KIND_UNKNOWN: (VERDICT_VENDOR_RESET, VERDICT_CREDIT_GRANTED, VERDICT_LIMIT_CHANGE),
}

# Only a clear nothing this account did explains may START an incident. Every
# other verdict annotates an announcement and never originates one
# (docs/robustness-plan.md, "Never incidents").
ORIGINATING_VERDICTS = (VERDICT_VENDOR_RESET,)

# +-6 h between the post and the clear, the window the plan fixes for calling an
# announcement observed. The lower bound is not symmetric by accident: this
# account measured the 2026-08-30 reset 2.5 to 3.5 minutes BEFORE @thsottiaux
# posted it, so an observation slightly ahead of its announcement is the normal
# case, not a mismatch.
CONFIRM_WINDOW_SECONDS = 6 * 3600
# A forecast with no stated deadline is watched for a day. The 2026-09-04
# banked reset forecast "end of day" landed 3 h 41 min after the post.
FORECAST_WINDOW_SECONDS = 24 * 3600
# A probe-first clear waits this long for a witness before the site says
# anything about it at all.
OBSERVED_HOLD_SECONDS = 120 * 60
# Older than this when first seen and it is history, never mail. The same
# number as subscriptions.MAX_NOTIFY_AGE_SECONDS, deliberately: a status the
# notifier would refuse to send must not be called "new" by the site.
HISTORICAL_AGE_SECONDS = 48 * 3600

# The healthy value of scripts/groundtruth.probe_status()["status"]. Duplicated
# rather than imported so this module keeps no dependency on the probe reader;
# tests/test_incidents.py asserts the two strings are equal.
PROBE_STATUS_OK = "ok"


def _moment(value: Any) -> datetime | None:
    """A timezone-aware datetime, or None for anything unusable.

    Everything reaching here came from a third-party feed or a state file, so a
    None, a date-only string and a naive stamp are all expected inputs. A naive
    one is rejected rather than assumed UTC: guessing a zone is how a reset
    lands seven hours from where it happened.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def probe_is_healthy(status: Any) -> bool:
    """True only when a probe for this vendor is running and current.

    Absent, blind, throttled and token-stale all answer False, because the one
    thing a forecast-lapsed verdict must never mean is "our probe was off".
    """
    return isinstance(status, dict) and status.get("status") == PROBE_STATUS_OK


def is_trusted_announcement(event: Any) -> bool:
    """Verified post, reviewed announcer, not a reply — the plan's three tests.

    `text_verified` is set by the discovery layer only when
    cdn.syndication.twimg.com returned the post for that id; a tracker's
    paraphrase never satisfies it, which is the whole point: the email quotes
    the post verbatim.
    """
    if not isinstance(event, dict):
        return False
    if not event.get("text_verified") or event.get("post_is_reply"):
        return False
    return is_reviewed_announcer(event.get("announcer"))


def is_explicit_reset_witness(event: Any) -> bool:
    """A verified post that says in so many words that a reset happened.

    This is the second witness a probe-first clear needs before the site will
    say "observed". A reviewed announcer is NOT required — if it were, the
    event would already be a trusted announcement and never reach this test.
    """
    if not isinstance(event, dict):
        return False
    if not event.get("text_verified") or event.get("post_is_reply"):
        return False
    return bool(RETROSPECTIVE_RE.search(str(event.get("text") or "")))


def derive_status(
    event: Any,
    observation: Any,
    probe_status: Any,
    *,
    now: Any = None,
) -> str:
    """One of INCIDENT_STATUSES for an announcement, an observation, or both.

    Pure: every input is data and the clock is injectable, so the whole state
    table is testable without a probe, a network or a database. `now` is
    keyword-only so the three positional arguments stay the ones the plan
    names.
    """
    moment = _moment(now) or datetime.now(timezone.utc)
    announcement = event if isinstance(event, dict) else None
    observed = observation if isinstance(observation, dict) else None
    trusted = is_trusted_announcement(announcement)

    # A retraction outranks everything else. Which retraction it is depends on
    # what the incident was built from: retracting our own probe-first claim is
    # a correction we owe the people we mailed, while a clear that reverted on
    # this account under a standing vendor announcement is a footnote about
    # THIS account and not a claim that the vendor took anything back.
    if observed is not None and observed.get("retracted"):
        return STATUS_REVERTED if trusted else STATUS_RETRACTED

    verdict = str(observed.get("verdict") or "") if observed else ""
    observed_at = _moment(observed.get("observed_at")) if observed else None

    if trusted:
        assert announcement is not None  # is_trusted_announcement's precondition
        announced_at = _moment(announcement.get("announced_at"))
        first_seen = _moment(announcement.get("first_seen_at")) or moment
        if (
            announced_at is not None
            and (first_seen - announced_at).total_seconds() > HISTORICAL_AGE_SECONDS
        ):
            # Discovered too late to tell anyone about. Shown, never mailed.
            return STATUS_HISTORICAL

        kind = event_kind(announcement)
        stage = event_stage(announcement)
        corroborates = verdict in CONFIRMING_VERDICTS.get(kind, ())
        in_window = False
        if corroborates and observed_at is not None and announced_at is not None:
            delta = (observed_at - announced_at).total_seconds()
            # A forecast has no upper bound: the thing was announced for later,
            # so "later" is exactly when the probe should see it.
            in_window = delta >= -CONFIRM_WINDOW_SECONDS and (
                stage == STAGE_FORECAST or delta <= CONFIRM_WINDOW_SECONDS
            )
        if in_window:
            return STATUS_CONFIRMED
        if stage == STAGE_FORECAST:
            until = _moment(announcement.get("forecast_until"))
            if until is None and announced_at is not None:
                until = announced_at + timedelta(seconds=FORECAST_WINDOW_SECONDS)
            if until is not None and moment > until and probe_is_healthy(probe_status):
                # Only a healthy probe may say a forecast lapsed. With a blind
                # one the honest answer is that it is still pending.
                return STATUS_FORECAST_LAPSED
            return STATUS_FORECAST
        return STATUS_ANNOUNCED

    # No announcement we may repeat. An own-account clear can still stand on
    # its own, but only the one verdict that means "nothing this account did
    # explains it", and only after the hold has given a witness time to appear.
    if verdict in ORIGINATING_VERDICTS and observed_at is not None:
        if moment < observed_at + timedelta(seconds=OBSERVED_HOLD_SECONDS):
            return STATUS_OBSERVED_PENDING
        if is_explicit_reset_witness(announcement):
            return STATUS_OBSERVED
        return STATUS_OBSERVED_SINGLE
    # A hint: an unverified post, a verdict that explains itself, or nothing.
    return STATUS_CANDIDATE
