# Reset attribution: was it our reset or the vendor's?

**Bottom line.** A clear on one of our accounts can never, by itself, prove a
vendor reset. Each vendor has a way for an account holder to reset their own
limit that no quota API shows: OpenAI's paid instant reset, and Anthropic's
personal limit reset redeemed on the web. Our probes can positively identify
every OTHER own-cause (spent banked credit, account switch, plan change, the
Claude Code reset menu). They call a reset the vendor's only when a second,
independent witness agrees: another of our accounts clearing in the same hour,
or a vendor announcement within minutes. Every historical clear since
2026-08-30 lands on its true cause under this rule (table below).

Written 2026-10-11 after the owner's decisions on the four open items in
`docs/robustness-plan.md` "Owner decisions". The owner's words, verbatim:

> 我觉得第一点可以做，关键就是我们要确认这个到底是我自己做的重置，还是厂家的重置，这点要判断清楚。第二个我觉得可以发邮件。

Meaning: (1) a single-account clear may start an incident, but only once we
can tell the owner's own reset from the vendor's; (2) Claude probe-only
incidents may email subscribers. (3) The IMAP hint channel and (4) the
Antigravity sign-in were questions, not decisions; see "Not in scope".

## What can reset a window

Researched 2026-10-11 against vendor documentation, the installed Codex
0.162.1 and Claude Code 2.1.283 binaries, and this host's own logs.

| Cause | Vendor | Probe can see it? | Signal |
|---|---|---|---|
| Banked reset redeemed (CLI `/usage`, desktop, web, mobile, any device) | OpenAI | yes | `rateLimitResetCredits.availableCount` falls; a credit id leaves `credits[]`. The fall can land one poll BEFORE the window clears (2026-10-04: 02:43:52 vs 02:44:52) |
| Paid instant reset (Plus/Pro personal accounts, web or desktop) | OpenAI | **no** | touches neither the bank nor `credits.balance`; the receipt is the only trace |
| Personal limit reset, redeemed on claude.ai or Desktop | Anthropic | **no** | absent from `/api/oauth/usage`; keeps the weekly anchor, so it looks exactly like a vendor reset |
| Personal limit reset from the Claude Code limit menu | Anthropic | partly | the profile's `history.jsonl` records `/rate-limit-options` or `/limit-reset` |
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

## The rule

A clear is first classified on its own account, in this order:

1. **`account_change`** — the account fingerprint or the plan differs across
   the clear. Not a reset at all; the slot is re-baselined. Private.
2. **`unresolved`** — no active anchor on record. Private.
3. **`natural_expiry`** — at or after the scheduled expiry. Public.
4. **`self_applied`** — a banked credit left within the lookback (Codex), or
   the Claude Code reset menu was opened within an hour before (Claude).
   Private.
5. **`unattributed`** — early, and every own-cause this probe can observe was
   checked and excluded: same fingerprint on both sides, plan unchanged, bank
   read on both sides and unspent (Codex), no reset menu in the profile's
   history (Claude), and a bracket of at most three polls. Private ON ITS OWN.
6. **`unresolved`** (legacy name `global_candidate` in the raw log) — early,
   but one of the checks in 5 could not be run. The reason names which. Private.

An `unattributed` clear becomes the VENDOR's in exactly two ways:

- **Two accounts.** Another of our accounts, with a different fingerprint,
  has an `unattributed` clear on the same window family within 60 minutes.
  Both become `vendor_reset` (public) and each names the other as its witness.
  Exception: if EVERY clear in the pair was at 95% or more before it cleared,
  the pair stays `unattributed`. An account at its limit is exactly the one
  its holder would pay to reset.
- **An announcement.** A trusted retrospective reset post lands between one
  hour before and three hours after the clear. The announcement is
  `confirmed` and its email says when our account saw it. The observation row
  itself stays private: a single account is shown only as the timing of a
  reset the vendor announced.

`vendor_reset` keeps the plan's ±6 h confirmation window. A single-account
`unattributed` clear uses the tighter window above, because the measured lead
of a clear over its post ranges from 23 s after to 79 min before, while an
owner's own reset can land at any time.

A probe-first incident (no announcement) can only start from `vendor_reset`.
It holds for 120 minutes as `observed_pending`, then becomes `observed`, with
the second account as its witness, and mails `new`. An `unattributed` clear
with no witness never becomes public and never mails.

## Signals each probe records

On every sample row:

- `account` — `a` or `b`, the only names ever written down.
- `account_fp` — the first 12 hex digits of
  `sha256("ai-resets/account:" + id)`. Codex: `accountId` from
  `account/rateLimits/read`. Claude: `organizationUuid + "/" + accountUuid`
  from the profile's `.claude.json` `oauthAccount`. The raw ids are never
  stored, logged or exported. `null` when unreadable.
- `credit_ids` (Codex only) — `[{"id": <12-hex sha256 of the credit id>,
  "expires_at": <epoch or null>}]` for available credits, or `null` when the
  response carried no detail rows.

On every clear event: `account`, `account_fp_before`, `account_fp_after`,
`plan_before`, `plan_after`, `window_family`, and
`checks = {identity, plan, credit, bracket, local_trace}` with values
`same/changed/unread`, `same/changed/unread`,
`unspent/spent/unread/no_bank`, `tight/wide` and `none/found/unread/n_a`.

## Second Codex account

`~/.codex-profiles/main1` holds a second, independent ChatGPT account (Pro
Lite). In the measured data it is the discriminator that works: it cleared on
10-02 and 10-11 (vendor) and did not clear on 10-04 and 10-10 (own credits).
The Codex probe reads it as account `b` through a second `codex app-server`
started with `CODEX_HOME` pointed at that home. Slot keys for `b` are prefixed
`b:` (`b:codex/10080`). Account `a` keeps its unprefixed keys so its cursor,
event ids and incident keys carry on unchanged. Health blocks are `codex`
(a) and `codex:b`.

Token safety. The probe never logs in or out. A `codex app-server` reads
`auth.json` fresh at start, and a process that gets a 401 reloads from disk
before it refreshes (codex-rs `login/src/auth/manager.rs`). So the second home
carries the same exposure the first has carried since August.

## Not in scope

- **The IMAP hint channel** reads, not sends: it would watch the owner's
  inbox for X notification emails about staff posts. Sending subscriber mail
  through Resend already works and needs nothing more. Parked: with probe-first
  incidents now able to mail, a reset that reaches our accounts is caught in
  minutes whoever posts it.
- **Antigravity** is Google's coding agent and CLI (`agy`), the successor for
  consumer accounts after Gemini CLI stopped serving them on 2026-06-18. A
  probe needs a Google AI Pro or Ultra subscription and one browser sign-in.
  Waiting on the owner to say whether such a subscription exists.
- Detecting OpenAI paid resets or web-redeemed Claude resets directly. Neither
  vendor exposes them. The two-witness rule is the substitute.
