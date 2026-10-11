# Reset evidence status improvement plan

Recorded October 10, 2026, Pacific Time. Status: planned; implementation has
not started.

The October 10 Codex reset announcement was collected and published. The
follow-up audit found two clarity problems: an account clear with an unknown
cause becomes `not_seen` on the announcement, and the provider badge says
`Ground truth · verified` when it only establishes a recent successful probe
reading. Improve those meanings while preserving the current privacy and
evidence rules.

## Current behavior

- [Announcement annotation](../scripts/build.py) accepts only `vendor_reset`
  as reset confirmation. An account clear classified as `unresolved` therefore
  falls through to `not_seen`, conflating an unverified cause with no clear.
- [Probe classification](../scripts/quota_probe.py) deliberately maps legacy
  `global_candidate` records to `unresolved`. Repeated readings confirm that
  a clear persisted; they do not establish who caused it.
- The healthy probe badge in [the static builder](../scripts/build.py) and
  [the browser code](../site/app.js) uses the last successful reading to say
  `verified`. This does not verify any particular reset announcement.

## 1 Clarify observation and confirmation states

- [ ] Keep account measurements, their cause, and vendor announcements as
  separate facts. In the private operator view, an `unresolved` clear should
  read “Account cleared; cause unverified,” rather than “not seen.”
- [ ] Give public announcements an explicit “not independently confirmed”
  status when no admissible confirmation exists. Its wording and serialized
  fields must not reveal whether a private clear occurred.
- [ ] Distinguish unavailable readings from negative evidence. An offline
  probe, first run, or insufficient usage to detect a clear must not claim
  that no clear occurred. Review the announcement and notification consumers
  together so they use consistent meanings.
- [ ] Keep natural expiry, personal redemption, an unknown-cause clear, a
  banked credit grant, and a limit change distinct. Temporal proximity to an
  announcement is a possible association, not proof of causation.

Acceptance: synthetic replay of an unknown-cause clear followed by an
announcement preserves the private clear and reports the public announcement
as not independently confirmed. It never upgrades the clear to `vendor_reset`
or `confirmed`. Public output must not reveal private clear timestamps,
percentages, credit balances, account identifiers, or private-event presence.
Tests must also cover absent/stale probes, insufficient usage, and expiry or
personal-redemption events near an announcement.

## 2 Describe probe health without implying event confirmation

- [ ] Replace the healthy badge with wording such as
  “Probe online · last successful reading … ago.”
- [ ] Keep static HTML and browser-rendered wording and freshness thresholds
  consistent, including the transition to stale or offline.
- [ ] Preserve distinct unknown, absent, and offline states. Document that
  `last_verified_at` represents a successful probe reading; changing that API
  field is unnecessary for this wording improvement.

Acceptance: a healthy probe with no independently confirmed reset shows only
probe health. The same data produces equivalent static and browser badges;
missing readings and timestamps older than the freshness threshold cannot
produce an online badge. Verify both desktop and mobile rendering.

## Implementation and delivery

Handle the two items as independently reviewable changes. Use synthetic
fixtures for the audit scenario and retain the protections in
[the reset scope regression tests](../tests/test_reset_scope.py). Run the
affected status, build, notification, and rendering checks, then the
repository's documented CI gates before any future publication. Verify the
deployed feed and page when a deployment is authorized.

This plan does not change collection cadence, reset detection thresholds,
notification delivery behavior, or public access to private account evidence.
Recording it does not start implementation or authorize publication.
