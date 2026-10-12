#!/usr/bin/env python3
"""What our own accounts actually show, in one sentence an email can carry.

The probe has been reading this account's real Codex quota every 60 seconds
since 2026-08-31, and nothing consumed it: the site never mentioned it and the
notifier never looked. So subscribers were told about announcements with no
statement of whether the thing had reached a real account — including a
forecast that had not landed, and including the 2026-08-30 reset that this
account SAW three minutes before the announcement was posted.

This module turns the probe's on-disk state into that statement, and refuses to
overstate it. Every branch below is a claim we can defend from a file:

  observed / not observed   the probe is healthy and the window is readable
  too empty to tell         our own usage is under the detection floor
  could not verify          the probe is blind, throttled, or its state is gone
  not observable            we run no probe for that vendor yet

"Not observed" is never rendered as "no reset happened". A single account on a
single plan tier cannot say that, and saying it is how a tracker loses trust.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import attribution, incidents
from scripts.timefmt import describe_age, format_epoch_pacific
from scripts.quota_probe import (
    ACCOUNT_LABEL,
    VERDICT_CREDIT_GRANTED,
    VERDICT_LIMIT_CHANGE,
    VERDICT_OF_CLASS,
    record_account,
    window_name,
)

# The same variable, and the same default, the probes write under. build.py
# reads its announcement annotations and incident fusion from here, so a site
# built against a copied state directory (a migration rehearsal, a second
# host) reads that copy instead of the live one.
DEFAULT_STATE_DIR = Path(os.environ.get("AI_RESETS_STATE", "/var/lib/ai-resets"))

HEALTH_FILENAME = "probe_health.json"
CURSOR_FILENAME = "quota_cursor.json"
EVENTS_FILENAME = "quota_events.jsonl"

# A probe that has not written its health file this recently is treated as
# blind. Two missed 60-second polls plus slack; the notifier runs every five
# minutes, so anything tighter would alert on ordinary scheduling jitter.
PROBE_STALE_SECONDS = 15 * 60

# Mirrors quota_probe.CLEAR_DROP_MIN. A window used less than this cannot show
# a clear at all, so "we did not see it" would be dishonest — we could not have.
# tests/test_groundtruth.py asserts the two constants stay equal.
DETECTION_FLOOR_PERCENT = 10.0

# The only slot that has ever carried real usage on this account. The 5-hour
# slot (a second limit id the account exposes) has read 0% for its entire
# history, so quoting it
# would say nothing.
CODEX_WEEKLY_SLOT = "codex/10080"

STATUS_OK = "ok"
STATUS_BLIND = "blind"
STATUS_ABSENT = "absent"
STATUS_THROTTLED = "throttled"
STATUS_TOKEN_STALE = "token_stale"
STATUS_UNPROBED = "unprobed"

# The public vocabulary for what an observed clear was. The probe computes the
# internal classification; these are the only words a reader ever sees.
VERDICT_NATURAL = "natural_expiry"
VERDICT_SELF = "self_applied"
VERDICT_VENDOR = "vendor_reset"
VERDICT_UNRESOLVED = "unresolved"
# The raw-log consumer and both exporters must interpret legacy clears alike.
# In particular, global_candidate is an unresolved account clear, not a reset
# receipt. Keeping a separate map here previously bypassed the export guard.
_INTERNAL_TO_VERDICT = VERDICT_OF_CLASS

# How far from an announcement an observation may sit and still be the same
# event. Our own account saw the 2026-08-30 reset 2.5 minutes BEFORE the post,
# so the window has to be symmetric: a rollout reaching us first is the normal
# case, not an anomaly.
MATCH_WINDOW_SECONDS = 6 * 3600



# ─── Which vendors we actually measure ───────────────────────────────────────


@dataclass(frozen=True)
class ProbeSpec:
    """Where one vendor's ground truth lives on disk, and how to describe it.

    Keyed by vendor rather than hard-coded so adding the Claude probe is a data
    change here and nothing else: build.py already renders observations for any
    vendor that has them, and the email sentence below reads this table.
    """

    vendor: str
    labels: tuple[str, ...]
    events_filename: str
    account_phrase: str
    weekly_kinds: tuple[str, ...]
    script: str
    # Whether this probe records a usage percentage we can quote. Without one,
    # "not observed" is still sayable but "the window was too empty to tell" is
    # not, so the two must not share a branch.
    has_usage_reading: bool = False
    # What this probe checked and found clean on a clear it could not explain,
    # in the words the email uses. Each probe excludes different own-causes
    # (docs/reset-attribution.md, "The rule"), so the sentence is per probe.
    own_causes_clause: str = ""
    product: str = ""
    # What the same sentence may say about a banked-credit ledger, strongest
    # first, for a probe that has one; bank_clause() picks the strongest the
    # clears earn. "Spent" needs every credit itemised on both sides of every
    # clear quoted: an unchanged COUNT cannot show that nothing left, because
    # a grant and a redemption between two polls cancel in a count (privacy
    # review). Without the list, the count is all that may be quoted.
    bank_clauses: tuple[str, ...] = ()

    def exists(self, root: Path | None = None) -> bool:
        """Does this probe actually ship? A spec for a script that is not here
        would make the email claim a measurement nothing can produce."""
        base = root or Path(__file__).resolve().parent
        return (base / self.script).is_file()


PROBE_SPECS = {
    "openai": ProbeSpec(
        vendor="openai",
        labels=("codex",),
        events_filename="quota_events.jsonl",
        account_phrase="our Pro account",
        weekly_kinds=("codex/10080",),
        script="quota_probe.py",
        has_usage_reading=True,
        own_causes_clause="the account did not change",
        product="Codex",
        bank_clauses=(
            "no banked credit was spent",
            "the banked credit count was unchanged",
            "the banked credit count did not fall",
        ),
    ),
    "anthropic": ProbeSpec(
        vendor="anthropic",
        # Two accounts on one operator machine, no longer on one plan: account
        # a moved from Max to a Team org on 2026-10-11, so the phrase names no
        # plan. Only the labels are ever written or shown; the organisations
        # behind them are not ours to name.
        labels=("claude:a", "claude:b"),
        events_filename="claude_events.jsonl",
        account_phrase="our two Claude accounts",
        weekly_kinds=("weekly_all",),
        script="claude_probe.py",
        own_causes_clause=(
            "the account did not change and no reset menu was opened on this machine"
        ),
        product="Claude",
    ),
}


def probed_vendors(
    state_dir: Path | str = DEFAULT_STATE_DIR, *, now: int | None = None
) -> tuple[str, ...]:
    """The vendors a probe is actually running for, read off the health file.

    Derived rather than declared, because the two states have to be told apart
    honestly and the transition happens without a code change. Before the probe
    exists the email says "we do not run one for this vendor"; the moment it
    starts writing health, the same email starts saying observed or not
    observed. A declared list would have to be edited at exactly the right
    moment, and would lie on either side of the mistake.
    """
    health = load_probe_health(state_dir)
    return tuple(
        vendor
        for vendor, spec in PROBE_SPECS.items()
        if spec.exists()
        and any(isinstance(health.get(label), dict) for label in spec.labels)
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def load_probe_health(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict[str, Any]:
    return _read_json(Path(state_dir) / HEALTH_FILENAME) or {}


def load_cursor(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict[str, Any]:
    return _read_json(Path(state_dir) / CURSOR_FILENAME) or {}


def clear_observations(
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    vendor: str = "openai",
    include_retracted: bool = False,
) -> list[dict[str, Any]]:
    """Every window clear the probe recorded, with what it concluded and why.

    Read from the probe's own append-only event log rather than from the
    exported file, so the email keeps working when an export has not run.
    Retracted clears are dropped by default: a clear we have already withdrawn
    is not evidence of anything. The fusion layer passes include_retracted and
    gets them back flagged with `retracted`: a withdrawn clear under a standing
    announcement is itself a lifecycle state (reverted_on_our_account), which
    is a different thing from evidence.
    """
    spec = PROBE_SPECS.get(vendor)
    path = Path(state_dir) / (spec.events_filename if spec else EVENTS_FILENAME)
    records = read_event_log(path)
    if records is None:
        return []
    # The two-account rule runs over the whole log first, exactly as the
    # exporters run it: this reader must never decide a different verdict
    # for the same record than the published feed did.
    records = attribution.attribute(records)
    by_id = attribution.latest_clears(records)
    clears: dict[str, dict[str, Any]] = {}
    retracted: set[str] = set()
    for event in records:
        kind = event.get("kind")
        key = event.get("event_id") or f"{event.get('slot')}:{event.get('detected_at')}"
        if kind == "retraction":
            retracted.add(key)
        elif kind in (None, "clear") and event.get("classification"):
            # kind is absent on records written before it existed; a
            # classification is what makes a record a clear.
            clears[key] = event
    out = []
    for key, event in clears.items():
        withdrawn = key in retracted
        if (withdrawn and not include_retracted) or not event.get("confirmed"):
            continue
        vendor_reset = attribution.is_vendor_reset(event)
        if event.get("concordance") == "accounts_disagree" and not vendor_reset:
            # One account's weekly window cleared while another readable
            # account's did not. The Claude probe already withholds this from
            # its own export for that reason; reading the raw event log here
            # would walk straight around that gate and publish the
            # announcement as confirmed. A withhold only one consumer honours
            # is not a withhold. A paired clear is the exception: the first of
            # a pair is written before the other account clears, so it reads
            # `disagree` about the very reset its witness confirms.
            continue
        moment = event.get("detected_at")
        if not isinstance(moment, int):
            continue
        row = {
            "key": key,
            "t": moment,
            "verdict": VERDICT_VENDOR
            if vendor_reset
            else _INTERNAL_TO_VERDICT.get(str(event.get("classification")), VERDICT_UNRESOLVED),
            "used_before": event.get("used_before"),
            "used_after": event.get("used_after"),
            "early_by_seconds": event.get("early_by_seconds"),
            "credits_before": event.get("credits_before"),
            "credits_after": event.get("credits_after"),
            "credits_carried": bool(event.get("credits_carried")),
            # Whether the bank listed its credits on both sides (quota_probe
            # writes it): how much a sentence about the bank may claim.
            "credit_itemised": event.get("credit_itemised") is True,
            # What fusion needs to key a probe-first incident and the email
            # needs to say which account saw it. Labels and times only: the
            # fingerprints stay in the log.
            "account": event.get("account"),
            "window_minutes": event.get("window_minutes"),
            # Which window, as attribution names it: fusion lets a withdrawn
            # clear speak against a post only on a window that could pair.
            "window_family": event.get("window_family"),
            "clear_bracket_lo": event.get("clear_bracket_lo"),
            "clear_bracket_hi": event.get("clear_bracket_hi"),
        }
        if vendor_reset:
            for field in attribution.STAMPED_FIELDS:
                row[field] = event.get(field)
            # The witness's own bank, read off its own record: the email
            # speaks for both accounts' credits, not only this one's.
            witness = by_id.get(str(event.get("witness"))) or {}
            row["witness_credit_itemised"] = witness.get("credit_itemised") is True
            row["witness_credits_before"] = witness.get("credits_before")
            row["witness_credits_after"] = witness.get("credits_after")
        if include_retracted:
            row["retracted"] = withdrawn
        out.append(row)
    return sorted(out, key=lambda o: o["t"])


def withdrawn_pair_clears(
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    vendor: str = "openai",
) -> list[list[dict[str, Any]]]:
    """Each pair that stood and a retraction has since broken, as its clears.

    Which groups those are is attribution.withdrawn_pairs' answer, including
    its rule that a pair must have stood before the revert (was_live).

    Read straight from the log rather than from clear_observations, whose
    evidence gates rightly skip what is left of the pair (a lone clear that
    read `disagree`): the people mailed about the pair are owed the
    correction whatever the surviving half now proves.
    """
    spec = PROBE_SPECS.get(vendor)
    records = read_event_log(Path(state_dir) / spec.events_filename) if spec else None
    if not records:
        return []
    retracted = attribution.retracted_ids(records)
    clears = attribution.latest_clears(records)
    groups = []
    for members in attribution.withdrawn_pairs(records):
        groups.append([
            {
                "key": key,
                "t": clears[key].get("detected_at"),
                "verdict": VERDICT_VENDOR,
                "retracted": key in retracted,
                "account": clears[key].get("account"),
                "window_minutes": clears[key].get("window_minutes"),
                "window_family": clears[key].get("window_family"),
                "clear_bracket_lo": clears[key].get("clear_bracket_lo"),
                "used_before": clears[key].get("used_before"),
                "used_after": clears[key].get("used_after"),
            }
            for key in members
        ])
    return groups


def probe_group_key(
    vendor: str, members: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """(incident key, members earliest first) for one group of paired clears.

    The key is incidents.probe_incident_key from the earliest member's lower
    bracket, so a pair keeps one key from its first build to its retraction.
    The fusion layer keys incidents with this, and the correction mail finds
    its withdrawn group with it, so the two cannot disagree.
    """
    ordered = sorted(
        members,
        key=lambda clear: (clear.get("clear_bracket_lo") or clear["t"], clear["key"]),
    )
    first = ordered[0]
    key = incidents.probe_incident_key(
        vendor, window_name(first.get("window_minutes")), first.get("clear_bracket_lo")
    )
    return key, ordered


def _accounts(clears: list[dict[str, Any]]) -> str:
    """`account a`, `account a and account b`: labels only."""
    names = [
        f"account {clear['account']}"
        if isinstance(clear.get("account"), str) and clear.get("account")
        else "one of our accounts"
        for clear in clears
    ]
    return " and ".join(names)


WITHDRAWAL_CLOSE = (
    "We are correcting our own observation here, not any vendor's announcement."
)


def withdrawal_line(
    event: dict[str, Any],
    *,
    state_dir: Path | str = DEFAULT_STATE_DIR,
) -> str:
    """What a correction says it withdraws: whose clear reverted, and which claim falls.

    Read from the raw log, never from the feed's retracted row, which carries
    nothing of the clears. A half-withdrawn pair used to be corrected with
    "so the clear did not happen" under a quote of the OTHER account's clear,
    which still stands (review case [06b]). One account's clear standing alone
    is not wrong; the two-account claim built on it is what is withdrawn.
    """
    vendor = str(event.get("vendor") or "")
    key = str(event.get("incident_key") or "")
    reverted = (
        "went back to the level it held before, on the anchor it had before"
    )
    for members in withdrawn_pair_clears(state_dir, vendor=vendor):
        group_key, ordered = probe_group_key(vendor, members)
        if group_key != key:
            continue
        gone = [clear for clear in ordered if clear.get("retracted")]
        standing = [clear for clear in ordered if not clear.get("retracted")]
        if standing:
            return (
                f"{_capital(_accounts(gone))}'s clear reverted: its window "
                f"{reverted}. {_capital(_accounts(standing))}'s clear still "
                "stands, but one account alone cannot tell its holder's own reset "
                "from the vendor's, so our claim that two of our accounts saw a "
                f"vendor reset is withdrawn. {WITHDRAWAL_CLOSE}"
            )
        return (
            f"The clears on {_accounts(gone)} {'both' if len(gone) == 2 else 'all'} "
            f"reverted: each window {reverted}, "
            "so none of those clears happened, and our claim that two of our "
            f"accounts saw a vendor reset is withdrawn. {WITHDRAWAL_CLOSE}"
        )
    return (
        f"A clear this observation rested on reverted: its window {reverted}. "
        f"Without it, what we reported no longer stands. {WITHDRAWAL_CLOSE}"
    )


def read_event_log(path: Path) -> list[dict[str, Any]] | None:
    """Every JSON object in an append-only event log, or None when unreadable.

    A torn final line (the probe appends while this reads) costs that line,
    not the log.
    """
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    records.append(event)
    except OSError:
        return None
    return records


def incident_observations(
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    vendor: str = "openai",
) -> list[dict[str, Any]]:
    """The per-vendor observation stream the fusion layer matches against.

    clear_observations answers "what cleared"; fusion also needs the two
    records that corroborate the other announcement kinds — a credit grant
    confirms a banked reset and a rescale confirms a policy change (the
    CONFIRMING_VERDICTS table in incidents.py). Retracted clears stay in,
    flagged, because a withdrawn clear under a standing announcement is the
    reverted_on_our_account state, not an absence of evidence.
    """
    spec = PROBE_SPECS.get(vendor)
    if spec is None:
        return []
    rows = clear_observations(state_dir, vendor=vendor, include_retracted=True)
    path = Path(state_dir) / spec.events_filename
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("kind")
                if kind == "credit_granted":
                    moment, verdict = event.get("t"), VERDICT_CREDIT_GRANTED
                elif kind == "limit_change":
                    moment = event.get("clear_bracket_hi") or event.get("detected_at")
                    verdict = VERDICT_LIMIT_CHANGE
                else:
                    continue
                if not isinstance(moment, int):
                    continue
                rows.append(
                    {
                        "key": event.get("event_id") or f"{kind}:{moment}",
                        "t": moment,
                        "verdict": verdict,
                        "retracted": False,
                    }
                )
    except OSError:
        pass
    return sorted(rows, key=lambda o: o["t"])


def observation_near(
    announced_at: int | None,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    *,
    window_seconds: int = MATCH_WINDOW_SECONDS,
    vendor: str = "openai",
) -> dict[str, Any] | None:
    """The clear closest to an announcement, if one sits inside the window.

    Only a vendor_reset is returned. A natural expiry says nothing about the
    vendor, and a self-applied clear says what the OWNER did with their own
    banked credit — publishing that as corroboration would both mislead and
    leak.
    """
    if announced_at is None:
        return None
    candidates = [
        o
        for o in clear_observations(state_dir, vendor=vendor)
        if o["verdict"] == VERDICT_VENDOR
        and abs(o["t"] - announced_at) <= window_seconds
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda o: abs(o["t"] - announced_at))


def corroborating_clear(
    vendor: str,
    *,
    kind: str | None,
    is_forecast: bool,
    announced_at: int | None,
    trusted: bool,
    state_dir: Path | str = DEFAULT_STATE_DIR,
) -> dict[str, Any] | None:
    """The clear that confirms this post under incidents.corroborates_at, if any.

    The same rule the site's `confirmed` status is derived from, so the email
    and the page cannot disagree about whether our account saw the reset: a
    two-account `vendor_reset` within +-6 h, or one account's `unattributed`
    clear between three hours before and one hour after a trusted
    retrospective reset post. Nearest to the post wins.
    """
    if not isinstance(announced_at, int) or kind is None:
        return None
    stage = incidents.STAGE_FORECAST if is_forecast else incidents.STAGE_RETROSPECTIVE
    candidates = [
        clear
        for clear in clear_observations(state_dir, vendor=vendor)
        # The same +-6 h match window build.py bounds every candidate with:
        # the rule gives a forecast no upper bound, the match does.
        if abs(clear["t"] - announced_at) <= MATCH_WINDOW_SECONDS
        and incidents.corroborates_at(
            kind, stage, clear["verdict"], clear["t"] - announced_at, trusted=trusted
        )
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda clear: (abs(clear["t"] - announced_at), clear["key"]))


def _relative_to_post(moment: int, announced_at: int) -> str:
    """`2 min before this post` / `23 s after this post`."""
    lead = announced_at - moment
    if lead == 0:
        return "the same second as this post"
    return f"{describe_age(abs(lead))} {'before' if lead > 0 else 'after'} this post"


def _epoch_of(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        moment = incidents._moment(value)
        return int(moment.timestamp()) if moment is not None else None
    return None


def _level(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _one_clear(clear: dict[str, Any]) -> str:
    """`account b at Oct 11, 2026, 8:58 PM PDT (96% to 0%)`, from a clear summary."""
    account = clear.get("account")
    who = f"account {account}" if isinstance(account, str) and account else "one account"
    moment = _epoch_of(clear.get("observed_at"))
    when = format_epoch_pacific(moment) if moment is not None else "a time we never recorded"
    before, after = clear.get("used_before"), clear.get("used_after")
    levels = f" ({before:.0f}% to {after:.0f}%)" if _level(before) and _level(after) else ""
    return f"{who} at {when}{levels}"


def _capital(clause: str) -> str:
    return f"{clause[:1].upper()}{clause[1:]}"


def _sentence(clause: str) -> str:
    return f"{_capital(clause)}." if clause else ""


def pair_summaries(clear: dict[str, Any]) -> list[dict[str, Any]]:
    """A paired clear row and its witness, as two summaries, earliest first."""
    summaries = [
        {
            "account": clear.get("account"),
            "observed_at": clear.get("t"),
            "used_before": clear.get("used_before"),
            "used_after": clear.get("used_after"),
            "credit_itemised": clear.get("credit_itemised") is True,
            "credits_before": clear.get("credits_before"),
            "credits_after": clear.get("credits_after"),
        }
    ]
    if clear.get("verdict") == VERDICT_VENDOR:
        summaries.append(
            {
                "account": clear.get("witness_account"),
                "observed_at": clear.get("witness_at"),
                "used_before": clear.get("witness_used_before"),
                "used_after": clear.get("witness_used_after"),
                "credit_itemised": clear.get("witness_credit_itemised") is True,
                "credits_before": clear.get("witness_credits_before"),
                "credits_after": clear.get("witness_credits_after"),
            }
        )
    return sorted(summaries, key=lambda c: _epoch_of(c.get("observed_at")) or 0)


def bank_clause(spec: ProbeSpec | None, clears: list[dict[str, Any]]) -> str:
    """`No banked credit was spent and the account did not change.`, earned.

    The strongest of spec.bank_clauses the quoted clears support: every
    clear itemised on both sides; else every count read and equal; else
    only that the count did not fall, which every `unattributed` clear's
    credit check already established. A probe with no bank says its own
    clause unchanged.
    """
    rest = (spec.own_causes_clause if spec else "") or "nothing our probe can see explained it"
    if spec is None or not spec.bank_clauses:
        return _sentence(rest)
    strongest, unchanged, not_lower = spec.bank_clauses
    if clears and all(clear.get("credit_itemised") is True for clear in clears):
        bank = strongest
    elif clears and all(
        _level(clear.get("credits_before"))
        and clear.get("credits_before") == clear.get("credits_after")
        for clear in clears
    ):
        bank = unchanged
    else:
        bank = not_lower
    return _sentence(f"{bank} and {rest}")


def _timed(clears: list[dict[str, Any]], announced_at: int) -> str:
    """`account a at ... (80% to 0%), 2 min before this post`, each its own.

    Two clears share one phrase only when it is the same phrase ("both 14
    min before this post"); a list of timings after a list of accounts read
    "14 min before this post and 14 min before this post".
    """
    timings = [
        _relative_to_post(moment, announced_at) if moment is not None else ""
        for moment in (_epoch_of(clear.get("observed_at")) for clear in clears)
    ]
    if len(clears) > 1 and len(set(timings)) == 1 and timings[0]:
        quantifier = "both" if len(clears) == 2 else "all"
        return f"{' and '.join(_one_clear(c) for c in clears)}, {quantifier} {timings[0]}"
    return ", and ".join(
        f"{_one_clear(clear)}, {timing}" if timing else _one_clear(clear)
        for clear, timing in zip(clears, timings)
    )


def observed_line(
    spec: ProbeSpec, clear: dict[str, Any], announced_at: int, *, is_forecast: bool
) -> dict[str, Any]:
    """What our own account saw of a post we are confirming, both times if two.

    Says when, relative to the post, and what the probe checked: that is the
    only basis on which one account's clear could have confirmed anything.
    """
    window = window_name(clear.get("window_minutes"))
    clears = pair_summaries(clear)
    clause = bank_clause(spec, clears)
    if len(clears) > 1:
        line = (
            f"Seen on two of our {spec.product} accounts: "
            f"{_timed(clears, announced_at)}. On each the {window} window cleared "
            f"early. {clause}"
        )
    else:
        early = clear.get("early_by_seconds")
        early_phrase = (
            f" {describe_age(early)} before its scheduled expiry"
            if _level(early) and early > 0
            else " early"
        )
        line = (
            f"Seen on our {spec.product} {_timed(clears, announced_at)}: the "
            f"{window} window cleared{early_phrase}. {clause}"
        )
    result: dict[str, Any] = {
        "status": STATUS_OK,
        "observed": True,
        "verdict": clear["verdict"],
        "line": line,
    }
    if is_forecast:
        # The forecast thing happened: the subject must not still say
        # "not yet landed" above a body saying it was seen.
        result["landed"] = True
    return result


def probe_first_line(
    event: dict[str, Any],
    *,
    state_dir: Path | str = DEFAULT_STATE_DIR,
) -> dict[str, Any]:
    """The ground-truth block of an incident our own accounts started.

    There is no post to compare against, so "Not observed" would contradict the
    very clear the email reports. This describes the clears instead: each
    account, when, and what it read, then why two of them were enough. The
    clears come from the row the fusion layer stamped (so the mail matches the
    page that announced it), else from the event log, else from the row's own
    reading — never from nothing.
    """
    vendor = str(event.get("vendor") or "")
    spec = PROBE_SPECS.get(vendor)
    product = spec.product if spec else incidents.vendor_product(vendor)
    logged = [
        row for row in clear_observations(state_dir, vendor=vendor) if row["verdict"] == VERDICT_VENDOR
    ]
    clears = sorted(
        (clear for clear in (event.get("clears") or []) if isinstance(clear, dict)),
        key=lambda c: _epoch_of(c.get("observed_at")) or 0,
    )
    if not clears:
        seen = _epoch_of(event.get("observed_at"))
        near = [
            row
            for row in logged
            if seen is not None and abs(row["t"] - seen) <= MATCH_WINDOW_SECONDS
        ]
        if near and seen is not None:
            clears = pair_summaries(min(near, key=lambda row: (abs(row["t"] - seen), row["key"])))
    if not clears:
        clears = [
            {
                "account": event.get("account"),
                "observed_at": event.get("observed_at"),
                "used_before": event.get("used_before"),
                "used_after": event.get("used_after"),
            }
        ]
    # The published row quotes each clear but not its bank, which stays in
    # the log: each clear's own record says how much the clause may claim,
    # and a clear the log no longer holds earns only the weakest clause.
    by_moment = {(row.get("account"), row["t"]): row for row in logged}
    clause = bank_clause(spec, [
        by_moment.get((clear.get("account"), _epoch_of(clear.get("observed_at")))) or clear
        for clear in clears
    ])
    window = str(event.get("window") or "") or "usage"
    described = " and ".join(_one_clear(clear) for clear in clears)
    if len(clears) > 1:
        line = (
            f"Seen on two of our {product} accounts before any source we could verify "
            "announced it: "
            f"{described}. On each the {window} window cleared early. {clause} One "
            "account alone cannot tell its holder's own reset from the vendor's; two "
            "separate accounts clearing within the hour is why we are writing."
        )
    else:
        line = f"Seen on our {product} {described}: the {window} window cleared early. {clause}"
    return {"status": STATUS_OK, "observed": True, "verdict": VERDICT_VENDOR, "line": line}


def credit_grant_since(
    since: int | None, state_dir: Path | str = DEFAULT_STATE_DIR
) -> dict[str, Any] | None:
    """The first banked-credit grant the probe recorded after `since`.

    A banked reset does not clear the window. It drops a credit into the bank
    for the account holder to spend, so the landing signal is the credit count
    rising, which the probe records as a credit_granted observation. Reading
    the window instead is how an email came to say "not landed" at 10:16 PM
    while carrying "2 banked credits" in the same sentence — the grant had
    landed at 9:20 PM, three hours and forty-one minutes after the post.

    The log is append-only and small (one line per event, not per sample), so
    it is read whole. Grants are emitted once per limit window, so the
    earliest matching record is the grant. Account a's only: the sentence
    this feeds names "our Pro account" and quotes a's weekly window and bank
    beside it, and account b's grant into its own bank would put the second
    account's numbers under the first account's name.
    """
    if since is None:
        return None
    path = Path(state_dir) / EVENTS_FILENAME
    best: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("kind") != "credit_granted":
                    continue
                if record_account(event) != ACCOUNT_LABEL:
                    continue
                moment = event.get("t")
                if not isinstance(moment, int) or moment < since:
                    continue
                if best is None or moment < best["t"]:
                    best = event
    except OSError:
        return None
    return best


def probe_status(
    health: dict[str, Any],
    label: str,
    now: int,
    *,
    stale_seconds: int = PROBE_STALE_SECONDS,
) -> dict[str, Any]:
    """Health of one probe, with the age that justifies the verdict.

    A missing file and a stale file are deliberately different states: the
    first means the probe has never run here, the second means it stopped.
    Neither may be read as "nothing happened".
    """
    entry = health.get(label)
    if not isinstance(entry, dict):
        return {"status": STATUS_ABSENT, "label": label, "age_seconds": None}

    updated_at = entry.get("updated_at")
    age = None if not isinstance(updated_at, (int, float)) else max(0, now - int(updated_at))
    result = {
        "status": STATUS_OK,
        "label": label,
        "age_seconds": age,
        "blind_since": entry.get("blind_since"),
        "consecutive_failures": entry.get("consecutive_failures") or 0,
        "last_ok_at": entry.get("last_ok_at"),
        "detectable_now": entry.get("detectable_now") or {},
    }

    throttled_until = entry.get("throttled_until")
    if age is None or age > stale_seconds:
        result["status"] = STATUS_BLIND
    elif entry.get("blind_since"):
        result["status"] = STATUS_BLIND
    elif isinstance(throttled_until, (int, float)) and int(throttled_until) > now:
        result["status"] = STATUS_THROTTLED
    elif entry.get("token_stale"):
        result["status"] = STATUS_TOKEN_STALE
    return result


def codex_weekly_reading(state_dir: Path | str = DEFAULT_STATE_DIR) -> dict[str, Any] | None:
    """The newest weekly-window sample the probe recorded, or None."""
    slots = load_cursor(state_dir).get("slots")
    if not isinstance(slots, dict):
        return None
    slot = slots.get(CODEX_WEEKLY_SLOT)
    last = slot.get("last") if isinstance(slot, dict) else None
    if not isinstance(last, dict) or not isinstance(last.get("used_percent"), (int, float)):
        return None
    return {
        "used_percent": float(last["used_percent"]),
        "credits_available": last.get("credits_available"),
        "t": last.get("t"),
        "resets_at": last.get("resets_at"),
    }


def _credit_phrase(credits: Any) -> str:
    if not isinstance(credits, int):
        return ""
    return f", {credits} banked credit" + ("" if credits == 1 else "s")


def ground_truth_line(
    vendor: str,
    *,
    is_forecast: bool,
    now: int,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    probed_vendors: tuple[str, ...] | None = None,
    kind: str | None = None,
    announced_at: int | None = None,
    trusted: bool = False,
) -> dict[str, Any]:
    """One sentence about our own accounts, plus the status that produced it.

    `kind` and `announced_at` matter for a forecast: what counts as "landed"
    depends on what was forecast. A banked reset lands as a credit in the
    bank; a plain reset lands as the window clearing. `trusted` is whether the
    post passed incidents.is_trusted_announcement: one account's clear may
    only ever confirm a trusted post, so it defaults to the answer that
    confirms less.
    """
    # Three different states, and conflating any two of them produces a false
    # sentence: we run no probe for this vendor at all; we run one and it has
    # never reported; we run one and it is currently blind. Only the first is
    # "we do not measure this vendor".
    spec = PROBE_SPECS.get(vendor)
    if spec is not None and not spec.exists():
        spec = None  # declared but not shipped yet
    account = spec.account_phrase if spec else "our account"
    label = spec.labels[0] if spec else "codex"
    if probed_vendors is not None:
        known = vendor in probed_vendors
    else:
        known = spec is not None
    if not known or spec is None:
        return {
            "status": STATUS_UNPROBED,
            "line": (
                "We do not yet run a ground-truth probe for this vendor, so this "
                "update is the announcement only, not a confirmed observation."
            ),
        }

    # The observation is checked FIRST, before the probe's health or any usage
    # reading: a clear already in the log is evidence whether or not the probe
    # is healthy this minute, and the page's `confirmed` status is derived from
    # the same rule without asking either.
    match = corroborating_clear(
        vendor,
        kind=kind,
        is_forecast=is_forecast,
        announced_at=announced_at,
        trusted=trusted,
        state_dir=state_dir,
    )
    if match is not None and isinstance(announced_at, int):
        return observed_line(spec, match, announced_at, is_forecast=is_forecast)

    health = load_probe_health(state_dir)
    status = probe_status(health, label, now)
    for other in spec.labels[1:]:
        # With more than one account, the probe is only healthy when they all
        # are: a blind half cannot be reported as a clean look.
        if probe_status(health, other, now)["status"] != STATUS_OK:
            status = probe_status(health, other, now)
            break
    if status["status"] in (STATUS_ABSENT, STATUS_BLIND):
        since = status.get("blind_since") or status.get("last_ok_at")
        when = (
            f" since {format_epoch_pacific(since)}"
            if isinstance(since, (int, float))
            else ""
        )
        return {
            "status": status["status"],
            "line": (
                f"Could not verify on {account}: "
                + (
                    f"the probe has been offline{when}."
                    if when
                    else "no probe reading is on record."
                )
            ),
        }

    # Nothing above confirmed the post. An unresolved clear (a check that
    # could not run) or a lone unattributed one outside the tight window says
    # nothing about the vendor, so the reading below is all there is to quote.
    # Bank-credit grants retain their separate, timing-only handling.
    reading = codex_weekly_reading(state_dir) if spec.has_usage_reading else None
    if reading is None and spec.has_usage_reading:
        return {
            "status": STATUS_ABSENT,
            "line": (
                f"Could not verify on {account}: no weekly-window reading is "
                "on record."
            ),
        }
    if reading is None:
        # A probe with no usage reading to quote can still say the honest
        # thing: it was watching and it saw nothing.
        verified = status.get("last_ok_at")
        seen = (
            f" as of {format_epoch_pacific(verified)}"
            if isinstance(verified, (int, float))
            else ""
        )
        return {
            "status": STATUS_OK,
            "line": (
                f"Not observed on {account}{seen}. One operator's accounts on one "
                "plan tier cannot rule a reset out."
            ),
        }

    used = reading["used_percent"]
    when = (
        format_epoch_pacific(reading["t"])
        if isinstance(reading["t"], (int, float))
        else "the last reading"
    )
    credits = _credit_phrase(reading["credits_available"])

    if is_forecast:
        if kind == "banked":
            grant = credit_grant_since(announced_at, state_dir)
            if grant is not None:
                delay = (
                    f", {describe_age(grant['t'] - announced_at)} after the post"
                    if isinstance(announced_at, int)
                    else ""
                )
                after = grant.get("credits_after")
                bank = _credit_phrase(after).lstrip(", ") or "the bank rose"
                return {
                    "status": STATUS_OK,
                    "landed": True,
                    "line": (
                        f"Landed on {account} at "
                        f"{format_epoch_pacific(grant['t'])}{delay}: {bank}. "
                        "A banked reset is a credit to spend, so the weekly "
                        f"window still reads {used:.0f}% used. This match is by "
                        "timing alone: a standing daily grant would look the same."
                    ),
                }
            return {
                "status": STATUS_OK,
                "line": (
                    f"Not landed on {account} as of {when}: the bank still "
                    f"holds{credits or ' no banked credit'} and the weekly window "
                    f"reads {used:.0f}% used. We will not send a second email "
                    "unless it lands."
                ),
            }
        return {
            "status": STATUS_OK,
            "line": (
                f"Not landed on {account} as of {when}: the weekly window "
                f"reads {used:.0f}% used{credits}. We will not send a second "
                "email unless it lands."
            ),
        }
    if used < DETECTION_FLOOR_PERCENT:
        return {
            "status": STATUS_OK,
            "line": (
                f"Our own weekly window was only {used:.0f}% used at {when}, too "
                "empty for a reset to be visible on it. This is not evidence "
                "either way."
            ),
        }
    return {
        "status": STATUS_OK,
        "line": (
            f"Not observed on {account} as of {when}: the weekly window "
            f"still reads {used:.0f}% used{credits}. One account on one plan "
            "cannot rule a reset out."
        ),
    }


def stale_age_line(status: dict[str, Any]) -> str:
    age = status.get("age_seconds")
    if not isinstance(age, (int, float)):
        return "never"
    return describe_age(age)
