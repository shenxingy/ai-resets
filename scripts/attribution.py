#!/usr/bin/env python3
"""Whose reset was it? The two-account rule, written in one place.

docs/reset-attribution.md, "The rule". A clear on one account, early, with
every own-cause its probe can see checked and excluded, is `unattributed` —
and stays private, because OpenAI's paid instant reset and Anthropic's
web-redeemed limit reset leave nothing any probe can read. One account alone
can never say the reset was the vendor's. Two of our accounts can: on
2026-10-02 and 2026-10-11 the Codex Pro account and the separate Pro Lite
account both cleared, while on 2026-10-04 and 2026-10-10, when the Pro account
spent its own banked credit, the Pro Lite account did not move.

Both exporters and the raw-log reader (scripts/groundtruth.py) call this
module and nothing else to decide that, so the site, the announcement
annotations and the email cannot disagree about which clear was the vendor's.

It is pure — raw event records in, copies out — and it never believes an
attribution already written on a record. The event log is evidence, not a
verdict: a record that arrives claiming a witness is re-derived like any
other, and loses the claim if the rule does not give it one.

Deliberately imports nothing from scripts/: quota_probe imports this module,
so the strings it shares with the probe are restated below and
tests/test_attribution.py asserts they stay equal.
"""

from __future__ import annotations

from typing import Any, Iterable

# ─── Shared vocabulary (restated; see the module docstring) ──────────────────

EVENT_CLEAR = "clear"
EVENT_RETRACTION = "retraction"
CLASS_UNATTRIBUTED = "unattributed"
VERDICT_VENDOR_RESET = "vendor_reset"

# ─── The rule's two numbers ──────────────────────────────────────────────────

# Another account's clear on the same window family counts as a witness when
# the two clears' upper bracket bounds are at most this far apart. The
# contract's number: a vendor rollout reaches separate accounts within
# minutes, while an owner resetting two accounts by hand inside the same hour
# would have to do it on purpose.
PAIR_WINDOW_SECONDS = 60 * 60

# The contract's exception: if EVERY clear in a pair was at or above this
# level before it cleared, the pair stays unattributed. An account at its
# limit is exactly the one its holder would pay to reset, and two such
# accounts reset together is what an owner short of quota does.
AT_LIMIT_PERCENT = 95.0

# The only windows a pair may form on: each vendor's PRIMARY weekly window,
# as the probes name it (quota_probe.window_family() for Codex's base limit,
# claude_probe.window_key() for Claude's all-models window;
# tests/test_attribution.py asserts both spellings). A vendor reset clears the
# per-model and secondary weekly windows too (`weekly_scoped:<model>`,
# Codex's `codex_bengalfox`), often a poll apart, and each of those pairing
# on its own made one reset two incidents and two mails. They also let a pair
# both at its limit on the primary window escape AT_LIMIT_PERCENT through a
# scoped window that was not. One reset, one window it is judged on.
PAIRING_FAMILIES = frozenset({"codex/10080", "weekly_all"})

# What attribute() stamps on a clear it upgrades. Every one of these is
# removed from the input first, so a value already on a record can never
# survive into the output on its own say-so.
ATTRIBUTION_TWO_ACCOUNTS = "two_accounts"
STAMPED_FIELDS = (
    "attribution",
    "witness",
    "witness_account",
    "witness_at",
    "witness_used_before",
    "witness_used_after",
)


# ─── Reading one record ──────────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    """A non-empty string, or None. Fingerprints, labels and ids are all this."""
    return value if isinstance(value, str) and value else None


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _is_clear(event: dict[str, Any]) -> bool:
    # `kind` is absent on records written before it existed; those predate
    # fingerprints too, so they can never pass the identity test below.
    return event.get("kind") in (None, EVENT_CLEAR)


def retracted_ids(events: Iterable[Any]) -> set[str]:
    """The event ids a retraction record has withdrawn."""
    return {
        event_id
        for event in events
        if isinstance(event, dict)
        and event.get("kind") == EVENT_RETRACTION
        and (event_id := _text(event.get("event_id"))) is not None
    }


def is_candidate(event: Any, retracted: set[str]) -> bool:
    """May this clear take part in a pair at all?

    Only a confirmed, non-retracted `unattributed` clear: every other class
    already names a cause (or admits it could not check one). Its identity has
    to be on record on both sides and unchanged — a legacy row with no
    fingerprint never pairs, because "a different account" is exactly what
    this rule cannot assume. A record with no event id cannot be named as
    anyone's witness, so it cannot be one. Only a clear on a PAIRING_FAMILIES
    window qualifies: a per-model or secondary window stays `unattributed`
    and private whatever the other account's did.
    """
    if not isinstance(event, dict) or not _is_clear(event):
        return False
    if not event.get("confirmed") or event.get("classification") != CLASS_UNATTRIBUTED:
        return False
    event_id = _text(event.get("event_id"))
    if event_id is None or event_id in retracted:
        return False
    after = _text(event.get("account_fp_after"))
    if after is None or _text(event.get("account_fp_before")) != after:
        return False
    if _text(event.get("account")) is None:
        return False
    # _text first: a malformed (unhashable) value must read as "not one".
    if _text(event.get("window_family")) not in PAIRING_FAMILIES:
        return False
    return _epoch(event.get("clear_bracket_hi")) is not None


def at_limit(event: dict[str, Any]) -> bool:
    """Was this account at its limit before it cleared?

    An unreadable level counts as at the limit: the exception exists to keep
    an owner's paid reset private, and a level we never read cannot show it
    was not one.
    """
    used = event.get("used_before")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return True
    return float(used) >= AT_LIMIT_PERCENT


def can_witness(event: dict[str, Any], other: dict[str, Any]) -> bool:
    """Is `other` an independent second sighting of the clear in `event`?

    Both must already be candidates. A different fingerprint is the contract's
    test; a different probe label is required as well, because one probe slot
    that re-logged into a second account within the hour is one operator at
    one keyboard, not two accounts that each saw something.
    """
    if event.get("window_family") != other.get("window_family"):
        return False
    if event.get("account") == other.get("account"):
        return False
    if event.get("account_fp_after") == other.get("account_fp_after"):
        return False
    gap = abs(event["clear_bracket_hi"] - other["clear_bracket_hi"])
    if gap > PAIR_WINDOW_SECONDS:
        return False
    return not (at_limit(event) and at_limit(other))


# ─── The rule over a whole log ───────────────────────────────────────────────


def latest_clears(events: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """event_id -> the last clear record carrying it, as groundtruth reads it."""
    clears: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict) or not _is_clear(event):
            continue
        event_id = _text(event.get("event_id"))
        if event_id is not None:
            clears[event_id] = event
    return clears


def witnesses(events: Iterable[Any], *, ignore_retractions: bool = False) -> dict[str, str]:
    """event_id -> the event_id of the clear that witnesses it.

    The nearest qualifying clear by bracket time wins, ties broken by event id
    so the answer never depends on log order. `ignore_retractions` answers a
    different question — which pair WOULD this withdrawn clear have belonged
    to — and exists only so the fusion layer can tell the people it mailed
    about a pair that one half of it has since been taken back.
    """
    records = [event for event in events if isinstance(event, dict)]
    retracted = set() if ignore_retractions else retracted_ids(records)
    candidates = {
        event_id: event
        for event_id, event in latest_clears(records).items()
        if is_candidate(event, retracted)
    }
    found: dict[str, str] = {}
    for event_id, event in candidates.items():
        others = [
            (abs(event["clear_bracket_hi"] - other["clear_bracket_hi"]), other_id)
            for other_id, other in candidates.items()
            if other_id != event_id and can_witness(event, other)
        ]
        if others:
            found[event_id] = min(others)[1]
    return found


def pair_components(pairs: dict[str, str | None]) -> list[tuple[str, ...]]:
    """One group per incident: the event ids joined through the witness each names.

    Two accounts make one pair and one incident; three accounts clearing in
    the same hour are still one reset, not three. Groups are sorted tuples in
    a sorted list, so the answer never depends on the order of the log.
    """
    parent = {key: key for key in pairs}

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for key, other in pairs.items():
        if other in parent:
            parent[root(key)] = root(other)
    groups: dict[str, list[str]] = {}
    for key in pairs:
        groups.setdefault(root(key), []).append(key)
    return sorted(tuple(sorted(members)) for members in groups.values())


def reverted_at(events: Iterable[Any]) -> dict[str, int | None]:
    """event_id -> when its clear first reverted, from its retraction records.

    The earliest `reverted_at` on record (the probe stamps the first of the
    revert reads it needed); None for a retraction that carries no readable
    time, which nothing downstream may read as "early enough".
    """
    found: dict[str, int | None] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != EVENT_RETRACTION:
            continue
        event_id = _text(event.get("event_id"))
        if event_id is None:
            continue
        moment = _epoch(event.get("reverted_at"))
        if event_id not in found:
            found[event_id] = moment
        elif found[event_id] is not None and moment is not None:
            found[event_id] = min(found[event_id], moment)
        else:
            # One unreadable record makes the whole answer unreadable: a
            # guess here decides whether a lone clear's time is published.
            found[event_id] = None
    return found


def was_live(
    group: Iterable[str],
    clears: dict[str, dict[str, Any]],
    reverted: dict[str, int | None],
) -> bool:
    """Did this group ever stand as a pair before a retraction broke it?

    Only if every member's clear was confirmed, and every retracted member
    reverted strictly AFTER the last member's confirmation. A clear withdrawn
    before its partner existed never made a pair: grouping with retractions
    ignored still joins the two, and publishing that group's correction
    published a lone `unattributed` clear on the other account, which the
    contract keeps private (privacy review, RetractedBeforeWitness). A time
    that cannot be read cannot show the order, so it answers no.
    """
    confirmed: list[int] = []
    withdrawn: list[int] = []
    for event_id in group:
        clear = clears.get(event_id)
        moment = _epoch(clear.get("confirmed_at")) if clear and clear.get("confirmed") else None
        if moment is None:
            return False
        confirmed.append(moment)
        if event_id in reverted:
            if reverted[event_id] is None:
                return False
            withdrawn.append(reverted[event_id])
    return bool(withdrawn) and min(withdrawn) > max(confirmed)


def withdrawn_pairs(events: Iterable[Any]) -> list[tuple[str, ...]]:
    """The groups the rule formed, stood as pairs, and a retraction then broke.

    The same rule run with retractions ignored, minus every group that still
    has a live member: without a retraction the two runs agree, so only a
    withdrawn clear can leave a group here. A member still live means the
    reset still stands on other accounts, and nobody is owed a correction.
    A group that never stood (was_live) is dropped too: nobody was told about
    it, and its correction would be the only place its clears appeared.
    """
    records = [event for event in events if isinstance(event, dict)]
    live = set(witnesses(records))
    clears = latest_clears(records)
    reverted = reverted_at(records)
    # Every key in a witness map has a witness, and witnessing is symmetric,
    # so each group already holds at least two clears.
    return [
        group
        for group in pair_components(witnesses(records, ignore_retractions=True))
        if not live.intersection(group) and was_live(group, clears, reverted)
    ]


def attribute(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Every record, copied, with the two-account verdict on the clears it upgrades.

    The order and the records are unchanged; only the STAMPED_FIELDS differ.
    Callers map a stamped clear to `vendor_reset` through is_vendor_reset() and
    every other record through their own verdict map, as before.
    """
    records = [event for event in events if isinstance(event, dict)]
    pairs = witnesses(records)
    clears = latest_clears(records)
    retracted = retracted_ids(records)
    out: list[dict[str, Any]] = []
    for event in records:
        copy = {key: value for key, value in event.items() if key not in STAMPED_FIELDS}
        event_id = _text(event.get("event_id"))
        witness_id = pairs.get(event_id) if event_id is not None else None
        if witness_id is not None and is_candidate(event, retracted):
            witness = clears[witness_id]
            copy.update(
                attribution=ATTRIBUTION_TWO_ACCOUNTS,
                witness=witness_id,
                witness_account=witness.get("account"),
                witness_at=witness.get("clear_bracket_hi"),
                witness_used_before=witness.get("used_before"),
                witness_used_after=witness.get("used_after"),
            )
        out.append(copy)
    return out


def is_vendor_reset(event: Any) -> bool:
    """Did attribute() upgrade this record to the vendor's reset?

    Only meaningful on attribute()'s output, which is why that function strips
    these fields from its input: this check reads them, it does not derive them.
    """
    return (
        isinstance(event, dict)
        and _is_clear(event)
        and event.get("classification") == CLASS_UNATTRIBUTED
        and event.get("attribution") == ATTRIBUTION_TWO_ACCOUNTS
        and _text(event.get("witness")) is not None
        and event.get("witness") != event.get("event_id")
    )
