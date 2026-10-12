# Reset attribution: was it our reset or the vendor's?

**Bottom line.** A clear on one of our accounts can never, by itself, prove a
vendor reset. Each vendor has a way for an account holder to reset their own
limit that no quota API shows: OpenAI's paid instant reset, and Anthropic's
personal limit reset redeemed on the web. Our probes can positively identify
every OTHER own-cause (spent banked credit, account switch, plan change, the
Claude Code reset menu opened on this machine). They call a reset the vendor's
only when a second, independent witness agrees: another of our accounts
clearing the same window within the hour, or a trusted vendor post saying the
reset already happened, posted between one hour before and three hours after
the clear. Every historical clear since 2026-08-30 lands on its true cause
under this rule once the signals it needs are recorded (table below);
historical rows lack them, so the live log keeps those rows `unresolved`.

Written 2026-10-11 after the owner's decisions on the four open items in
`docs/robustness-plan.md` "Owner decisions". The owner's words, verbatim:

> 我觉得第一点可以做，关键就是我们要确认这个到底是我自己做的重置，还是厂家的重置，这点要判断清楚。第二个我觉得可以发邮件。

Meaning: (1) a single-account clear may start an incident, but only once we
can tell the owner's own reset from the vendor's; (2) Claude probe-only
incidents may email subscribers. (3) The IMAP hint channel and (4) the
Antigravity sign-in were questions, not decisions; see "Not in scope".

One account cannot tell those two apart (above), so decision (1) is built as:
one account's clear may CONFIRM a vendor post, and only two accounts together
may START an incident. Decision (2) is built as the same two-account rule for
Claude as for Codex.

Built the same day. Where the implementation settled a point this contract
left open, or differs from its first draft, the sections below describe the
implementation, and "Where the build differs from the first draft" lists each
change.

## What can reset a window

Researched 2026-10-11 against vendor documentation, the installed Codex
0.162.1 and Claude Code 2.1.283 binaries, and this host's own logs.

| Cause | Vendor | Probe can see it? | Signal |
|---|---|---|---|
| Banked reset redeemed (CLI `/usage`, desktop, web, mobile, any device) | OpenAI | yes | `rateLimitResetCredits.availableCount` falls; a credit id leaves `credits[]`. The fall can land one poll BEFORE the window clears (2026-10-04: 02:43:52 vs 02:44:52) |
| Paid instant reset (Plus/Pro personal accounts, web or desktop) | OpenAI | **no** | touches neither the bank nor `credits.balance`; the receipt is the only trace |
| Personal limit reset, redeemed on claude.ai or Desktop | Anthropic | **no** | absent from `/api/oauth/usage`; keeps the weekly anchor, so it looks exactly like a vendor reset |
| Personal limit reset from the Claude Code limit menu | Anthropic | partly | the profile's `history.jsonl` records `/rate-limit-options` or `/limit-reset`, on this machine only |
| Re-login to another account or org | both | yes | Codex `accountId`; Claude `oauthAccount` org and account ids in the profile's `.claude.json` |
| Plan change (incl. the 2026-09-19 Pro to Pro Lite switch) | both | yes | `plan_type` |
| Purchased usage credits | OpenAI | n/a | never lowers `used_percent` |
| Vendor reset | both | yes | what we are trying to identify |

Sources: help.openai.com/en/articles/20001498 (banked resets), the OpenAI
paid-reset help page, openai/codex#27915 and PR #28154 (CLI redemption),
support.claude.com/en/articles/17007452 (limit resets), the Codex app-server
schema (`codex app-server generate-json-schema`), and Claude Code's bundled
`/limit-reset` command (programs `juniper_tide`, 5-hour only, and
`cedar_ember`, grant-based, weekly).

## Every historical clear, attributed

The cause column was established on 2026-10-11 from the probe logs, Codex
session rollouts of both Codex homes, `codex-login.log`, shell history, Claude
profile histories and `.claude.json` backups. "Rule" is what the rule below
assigns when the signals it needs are recorded.

| When (UTC) | Account | Cause | Deciding evidence | Rule |
|---|---|---|---|---|
| 08-31 02:26 | Codex Pro | vendor | bank 1→1; staff post 3 min later | unattributed + post → confirmed |
| 09-02 04:10 | Codex Pro | own banked credit | bank 1→0 | self_applied |
| 09-05 05:32 | Codex Pro | own banked credit | bank 2→1 | self_applied |
| 09-08 01:22 | Codex Pro | vendor | bank 1→1; post 34 min later | confirmed |
| 09-12 08:09 | Codex Pro | vendor | bank 1→1; post in the same minute | confirmed |
| 09-13 19:32 | Codex Pro | own banked credit | bank 1→0 | self_applied |
| 09-19 03:44 | Codex | account switch | plan pro → prolite | account_change |
| 09-20 19:47 | Codex | account switch | plan prolite → pro, `codex login` 14 s earlier | account_change |
| 09-26 16:59 | Codex Pro | vendor | bank 1→1; post 79 min later | confirmed |
| 09-30 18:18 | Claude b | own limit reset | `/rate-limit-options` 30 min earlier; account a did not clear | self_applied |
| 10-02 21:13 | Codex Pro | vendor | bank 2→2; Pro Lite account also cleared | vendor_reset |
| 10-04 02:44 | Codex Pro | own banked credit | bank 2→1 ONE POLL BEFORE the clear | self_applied (needs the lookback) |
| 10-05 00:46 | Codex Pro | own banked credit | bank 1→0 | self_applied |
| 10-07 03:28 | Codex Pro | vendor | bank 0→0; post 6 min later | confirmed |
| 10-10 05:45 | Codex Pro | own banked credit | bank 1→0; Pro Lite did not clear | self_applied |
| 10-11 03:58 | Codex Pro | vendor | bank 0→0; Pro Lite also cleared; post 2 min later | vendor_reset |
| 10-11 21:03 | Claude a | org switch | `/login`, Max org → Team org, plan max → team | account_change |

Historical rows carry no account fingerprint, so the probe itself cannot
re-derive "unattributed" for them: they stay `unresolved` forever. The rule
applies to clears recorded after this change ships.

Replayed through the implementation on 2026-10-11, from copies of the live
sample logs:

- **As recorded.** The account switches (09-19, 09-20, Claude a 10-11) come
  out `account_change` and the own resets come out `self_applied`, including
  10-04 through the lookback and Claude b 09-30 through its history. Every
  vendor row stays `unresolved`.
- **With a fingerprint per plan injected, and one reading per minute
  restored.** The sample log writes a row only when a value moves, so its
  brackets are wider than the live ones. All seven Codex vendor rows become
  `unattributed`, and no row of any other cause does.
- **What the replay could not show.** The second Codex account was not
  probed before this change. So 10-02 and 10-11 replay as `unattributed`,
  not as a pair. The Rule column's `vendor_reset` for them comes from the
  Pro Lite account's own session rollouts, which place its clears only
  within hours (10-02 between 18:54 and 21:48 UTC; 10-11 between 02:09 and
  04:05 UTC). That a live probe would have paired them within 60 minutes is
  an inference. On 10-11 the tracked post (04:01:09 UTC, "Put the reset in
  the bag. All propagated.") also falls inside the single-account window, so
  that clear would be `confirmed` either way.

## The rule

A clear is first classified on its own account, in this order:

1. **`account_change`.** The account fingerprint or the plan differs across
   the clear. This is not a reset at all.
   - Each reading is compared with the last value actually READ for that
     slot. A poll with no readable identity in between (a `/login` in
     progress) therefore cannot hide the switch.
   - The probe writes an event of `kind: account_change` that carries
     `seen_before` and `t`, not a clear bracket, so nothing downstream can
     mistake it for a clear. The slot is re-baselined.
   - Private.
2. **`unresolved`**: no active anchor on record. Private.
3. **`natural_expiry`**: at or after the scheduled expiry, or within two
   polls plus 60 s before it. Public.
4. **`self_applied`**. Private.
   - **Codex:** take every bank reading from three polls before the clear's
     bracket to the reading that showed the clear, plus the reading still in
     force when that span opened. The clear is the owner's own if any of
     those readings is higher than the bank after the clear, or if an
     itemised credit id left before its own expiry.
   - **Claude:** the profile's `history.jsonl` records `/rate-limit-options`
     or `/limit-reset` between one hour before the bracket and its end.
   - **A late reading:** a lower credit count read while a clear is still
     being confirmed turns an `unattributed` clear into `self_applied`.
5. **`unattributed`**: early, and every own-cause this probe can observe was
   checked and excluded. Private ON ITS OWN. All five `checks` must pass:
   - **identity:** the same fingerprint on both sides;
   - **plan:** unchanged;
   - **credit, Codex:** the bank COUNT read on both sides, not carried
     forward from an earlier reading, and unspent. When both sides list
     their credits, an id leaving early also counts as spent; the list is
     not required;
   - **credit, Claude:** reads `no_bank`, because Claude has no ledger;
   - **local trace, Claude:** no reset command in the profile's history.
     The history is read for every early weekly clear. A command that cannot
     be parsed or placed in time counts as `unread`, and until the history
     is read the clear stays `unresolved`;
   - **bracket:** at most three polls (180 s for Codex, 900 s for Claude),
     and not recovered across a blind gap.
6. **`unresolved`** (legacy name `global_candidate` in the raw log): early,
   but one of the checks in 5 could not be run. The reason names which. A
   side that could not be read makes a check `unread`, never `changed`: an
   unreadable profile file is not a new account. Private.

Claude's 5-hour window never enters this order. Its ordinary expiry and the
`/limit-reset` command produce the same clear, so it is recorded
`session_only` and is never attributed. Private.

An `unattributed` clear becomes the VENDOR's in exactly two ways:

- **Two accounts.** Another of our accounts has an `unattributed` clear on
  the same window family. The two bracket ends are at most 60 minutes apart.
  The two clears must come from different probe labels as well as different
  fingerprints, and both must be confirmed and not retracted. Both become
  `vendor_reset` (public), and each names the other as its witness.
  - **Only the primary weekly window pairs:** Codex's base limit
    (`codex/10080`) and Claude's all-models window (`weekly_all`). A vendor
    reset clears the per-model and secondary weekly windows too
    (`weekly_scoped:<model>`, `codex_bengalfox`), often a poll later. Paired on
    their own they made one reset two incidents and two mails, and a pair at
    its limit on the primary window escaped the exception below through a
    scoped window that was not. Those windows stay `unattributed`.
  - **Choosing the witness:** the nearest qualifying clear, with a tie going
    to the lower event id. Three or more accounts clearing together form one
    incident.
  - **Exception:** two clears that were both at 95% or more before they
    cleared cannot witness each other. An account at its limit is exactly
    the one its holder would pay to reset. A level that was never read
    counts as at the limit.
  - **Never stored as a verdict:** `scripts/attribution.py` re-derives the
    pairing from the raw log on every export. It first removes any witness
    already written on a record, so a forged witness is never believed.
- **An announcement.** A trusted post (verified, from a reviewed announcer,
  not a reply) of kind reset, which says the reset already happened, lands
  between one hour before and three hours after the clear. The announcement
  becomes `confirmed`, and its email says when our account saw the clear and
  what the probe checked.
  - The observation row itself stays private: a single account is shown only
    as the timing of a reset the vendor announced.
  - A post with no label never takes a single account's word.
  - A Claude clear can be blocked here. If the other Claude account was
    readable and had not cleared (`accounts_disagree`), the clear confirms
    nothing unless it is half of a pair.

`vendor_reset` keeps the plan's ±6 h confirmation window. A single-account
`unattributed` clear uses the tighter window above, because the measured lead
of a clear over its post ranges from 23 s after to 79 min before, while an
owner's own reset can land at any time.

A probe-first incident (no announcement) can only start from `vendor_reset`:

- **Hold:** it holds for 120 minutes as `observed_pending`, counted from the
  LATER clear of the pair. Then it becomes `observed`, with the second account
  as its witness, and mails `new`.
- **What is public during the hold:** the pair's two observation rows are
  public as soon as the pair forms. The hold delays only the incident and its
  mail.
- **Incident key:** `<vendor>:probe-<window>-<lower bracket of the earliest
  clear>`, for example `openai:probe-weekly-1791691072`.
- **A post arriving later:** a trusted post that confirms the pair takes the
  incident over and records it in `merged_from`. Only a post whose own status
  is `confirmed` can: a post that ends up historical, merely announced or
  `reverted_on_our_account` leaves the pair its own incident.
- **A withdrawn clear near a post** makes it `reverted_on_our_account` only if,
  while it stood, it would have confirmed that post (a member of a pair that
  stood counts as the pair), and only on a window that can pair. Standing
  evidence that confirms the post wins over a withdrawal however near.
- **A retraction:** a pair that stood and that a retraction then breaks
  becomes one `retracted` row, so the people who were mailed get the
  correction. A probe retracts a clear only when two consecutive reads show it
  reverted (`REVERT_POLLS`): on 2026-09-09 the vendor served account a's
  pre-reset level on its pre-reset anchor for one poll, and any other read in
  between restarts the count.
  - **It must have stood:** every member confirmed, and each retracted
    member's `reverted_at` strictly later than the last member's
    `confirmed_at`. A clear taken back before its partner was confirmed never
    made a pair, and produces no row at all.
  - **The row carries only** `incident_key`, the published id, `status:
    retracted` and `verdict: null`. No clears, account labels or times:
    `scripts/check_site.py` fails the publish if it carries anything else.
    The correction mail is written from the notifier's own record and the raw
    log, never from this row.
  - **Its key** is the post's when a trusted post took the pair over, else
    the probe key. The notifier never reads a retracted row as a rename, and
    corrects under the incident it filed the pair row under: subscribers
    when that is the probe-first incident, one owner alert when a post holds
    it (the post's own `reverted_on_our_account` raises the same alert, not a
    second one).
  - **A first mail still waiting** (held by the flood cap, or failed and
    waiting for its retry) is read against the feed again before it goes
    out. It is cancelled when the feed marks the incident withdrawn or its
    status no longer mails: the delivery stays `failed` with a `last_error`
    starting `cancelled:` and the reason. A tick in which the incident is
    missing from the feed only defers it, because an absence is not a
    withdrawal.

An `unattributed` clear with no witness never becomes public and never mails.
Nothing alerts the owner for `observed_pending` or for an account change; both
are visible only in the probe logs.

## Signals each probe records

On every sample row:

- `account`: `a` or `b`, the only names ever written down.
- `account_fp`: the first 12 hex digits of
  `sha256("ai-resets/account:" + id)`. `null` when unreadable. The raw ids are
  never stored, logged or exported.
  - **Codex:** the top-level `accountId` of `account/rateLimits/read`.
  - **Claude:** `organizationUuid + "/" + accountUuid` from the profile's
    `.claude.json` `oauthAccount`, read fresh on every poll and never cached.
    Defaults: `~/.claude.json` for a and
    `~/.claude-profiles/main1/.claude.json` for b, overridable with
    `AI_RESETS_CLAUDE_CONFIG_A` / `_B`. The reset-menu history defaults to
    each profile's `history.jsonl`, overridable with
    `AI_RESETS_CLAUDE_HISTORY_A` / `_B`.
- `credit_ids` (Codex only): the available credits, as
  `[{"id": <first 12 hex digits of sha256("ai-resets/credit:" + id)>,
  "expires_at": <epoch or null>}]`.
  - `[]` means itemised and empty.
  - `null` means the response carried no detail rows, or one available
    credit's id could not be read. A list with a hole in it would let that
    credit leave unseen.

On every clear event: `account`, `account_fp_before`, `account_fp_after`,
`plan_before`, `plan_after`, `window_family`, and
`checks = {identity, plan, credit, bracket, local_trace}` with values
`same/changed/unread`, `same/changed/unread`,
`unspent/spent/unread/no_bank`, `tight/wide` and `none/found/unread/n_a`.
Credit-grant events carry `account` too.

A clear also carries `credit_itemised`: `true` only when both sides listed
their credits (`credit_ids` a list, `[]` included). It sets how much any
published sentence or email may say about the bank. Itemised, it may say no
credit left the bank before its expiry. Otherwise only the count was read,
so it says "the banked credit count read N then N" and no more: a grant and
a redemption between two polls cancel in a count. The email follows the same
rule for every clear it quotes (both, for a pair): "No banked credit was
spent" only when all of them were itemised; else "the banked credit count
was unchanged" when every count was read and equal; else "the banked credit
count did not fall".

Published rows carry neither fingerprints nor raw event ids. A row's id is
`obs-<first 12 hex digits of sha256("ai-resets/observation:" + event id)>`,
because a Claude model-scoped slot id can contain an unreleased model's name.
For a model-scoped id the model slug is first replaced by `#<n>`, its rank
among that account's scoped windows clearing at the same moment, sorted by
slug: every other part of the id is on the public row, so a digest of the raw
id let anyone confirm a guessed codename. Ids with no model are unchanged.
Accounts appear as their labels.

## Second Codex account

`~/.codex-profiles/main1` holds a second, independent ChatGPT account (Pro
Lite). In the measured data it is the discriminator that works: it cleared on
10-02 and 10-11 (vendor) and did not clear on 10-04 and 10-10 (own credits).

- **How it is read:** the Codex probe reads it as account `b`, through a
  second `codex app-server` started with `CODEX_HOME` pointed at that home.
  The home comes from `AI_RESETS_CODEX_HOME_B`.
- **Which home:** the installer defaults to `~/.codex-profiles/main1` when
  that home holds an `auth.json`. Exporting the variable empty turns account
  b off. A relative path, or a path that resolves to account a's own home, is
  refused.
- **Keys:** slot keys for `b` are prefixed `b:` (`b:codex/10080`). Account
  `a` keeps its unprefixed keys, so its cursor, event ids and incident keys
  carry on unchanged. Health blocks are `codex` (a) and `codex:b`.
- **Failures:** account a is read and saved before b. A failure on b never
  blinds a and never exits the process; only account a's tenth consecutive
  failure does.
- **Status:** the Codex export reports blind while either account is blind,
  as the Claude export does, so a blind b shows the Codex card as offline
  even while a reads. Account b has its own owner alerts
  (`probe:codex:b:blind` and the rest).
- **Coverage:** the export speaks for b only while a `codex:b` health block
  exists.
- **The email:** when no clear confirms a post, the line about the bank and
  the window is still account a's reading. A pair's line names both
  accounts' clears.

Token safety. The probe never logs in or out. A `codex app-server` reads
`auth.json` fresh at start, and a process that gets a 401 reloads from disk
before it refreshes (codex-rs `login/src/auth/manager.rs`). So the second home
carries the same exposure the first has carried since August. The probe unit
gets write access to that one home (`ReadWritePaths=-<home>`), as it has to
`~/.codex`.

## Where the build differs from the first draft

Each change below is more conservative than the first draft, except where
marked.

1. **A pair needs two different probe labels as well as two different
   fingerprints.** One probe slot that re-logged into a second account within
   the hour is one operator at one keyboard, not two accounts.
2. **An unreadable "used before" level counts as at the limit,** so it can
   never escape the 95% exception.
3. **Account changes compare against the last value read,** not only the
   adjacent row.
4. **The credit lookback includes the reading in force when it opens.**
   Replayed from the sample log, the 2 that preceded the 2026-10-04 spend was
   written 82 minutes before the clear. This can only move a clear towards
   `self_applied`.
5. **Claude reads its history for every early weekly clear,** not only for a
   would-be `unattributed` one: step 4 comes before step 5. This is how 09-30
   b is `self_applied` even with no fingerprints.
6. **The Claude `accounts_disagree` gate.** A single Claude clear that read
   `accounts_disagree` cannot confirm a post. The first draft was silent on
   this.
7. **Published rows carry digests rather than event ids.** See "Signals each
   probe records".
8. **Less conservative: credit ids are an extra test.** "Bank read on both
   sides" means the count. Credit ids add a spend test but are not required.
   The draft's own table treats a count-only 1→1 as unattributed.
9. **Less conservative: a pair's observation rows are public at once.** Only
   the incident and its mail wait out the 120-minute hold.
10. **Less conservative: an observed clear counts even if the probe is blind
    now.** The email's "what our account saw" line is checked before probe
    health. A clear already recorded therefore still confirms a post while
    the probe is blind.
11. **Only each vendor's primary weekly window pairs.** The draft paired any
    window family. See "Two accounts" above.

## Not in scope

- **The IMAP hint channel** reads, not sends: it would watch the owner's
  inbox for X notification emails about staff posts. Sending subscriber mail
  through Resend already works and needs nothing more. Parked: with probe-first
  incidents now able to mail, a reset that reaches two of our accounts is
  caught in minutes whoever posts it.
- **Antigravity** is Google's coding agent and CLI (`agy`), the successor for
  consumer accounts after Gemini CLI stopped serving them on 2026-06-18. A
  probe needs a Google AI Pro or Ultra subscription and one browser sign-in.
  Waiting on the owner to say whether such a subscription exists.
- Detecting OpenAI paid resets or web-redeemed Claude resets directly. Neither
  vendor exposes them. The two-witness rule is the substitute.
- An owner alert for an account change or a pending probe-first incident.
  Neither exists yet; both are in the probe logs.
