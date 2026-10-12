# AI Reset Watch

A public record of when usage limits move on the coding assistants people
actually run: **Claude Code (Anthropic)**, **Codex / ChatGPT (OpenAI)** and
**Gemini (Google)**.

Most trackers can only repeat what a vendor said. This one also **measures**:
probes read the maintainer's own subscription quota straight from the vendor
APIs, so a window that clears can be told apart from a window that merely
expired on schedule. When an announcement and a measurement agree, the site
can report the timing. One account's early clear never starts a public reset
on its own, because an account holder can reset their own limit in ways no
quota API shows. It takes a second witness: another of our accounts, or the
vendor's own post.

Live at the address in [`site.config.json`](site.config.json).

- [Why measurement matters](#why-measurement-matters)
- [What the verdicts mean](#what-the-verdicts-mean)
- [Layout](#layout)
- [Data sources](#data-sources)
- [Running it locally](#running-it-locally)
- [Deploying](#deploying)
- [Email subscriptions](#email-subscriptions)
- [Tests](#tests)
- [Licence](#licence)

## Why measurement matters

An announcement feed is one source, and a semantically unreliable one. Of the
tracked announcements, most are retrospective ("we have reset"), a large
minority are **forecasts** with stated windows from ten minutes to "the next
few hours", and some grant a *banked credit* rather than resetting anything.
`announced_at` is the post's timestamp, so for the last two kinds it is not the
reset time at all.

Two measured examples, both from this project's own logs:

| What happened | Gap |
|---|---|
| An account clear observed before a reset announcement post | 149 s early |
| A banked-credit announcement, versus the credit actually landing | 3 h 41 m late |

Announcements also go missing. A reset announced somewhere other than the
account the tracker watches is invisible to a feed-only tracker, and that is
not a hypothetical: it is why the probes exist.

So `quota_probe.py` (OpenAI) and `claude_probe.py` (Anthropic) read the
operator's own authenticated quota — metadata only, consuming no tokens and no
reset credit — and time account clears directly. The probes are
**observation-only and never send mail** themselves; the notifier mails a
probe-first reset only when two of our accounts saw it (see the verdicts below).

Three measured facts drive the detection logic:

1. **The two vendors have different window contracts.** Codex re-anchors
   `resets_at` on a real clear; Anthropic keeps a fixed per-account weekly
   lattice. The detector records which happened at detection time, so a single
   revert rule serves both. Getting this wrong once caused a persistent clear to
   be retracted.
2. **The discriminator is the clear time against the window's active anchor.**
   A clear at or after the scheduled expiry is the window ending; a clear
   materially before it was forced.
3. **A forced clear is still ambiguous** between personal reset use and a
   vendor action. OpenAI's Bank count is a balance, not a redemption history:
   grants, spending and expiry can overlap between polls. Claude's personal
   limit resets can restore weekly limits too, and its usage response does not
   expose a redemption ledger. One account can only rule out the own-causes it
   can see, hence the [second witness](#what-the-verdicts-mean).

A clear must persist across two polls before it is trusted: on one occasion a
weekly window went 91% to 0% in 3.7 s, held for 12 h 41 m, and then reverted
to 92%.

**Known limit: a handful of accounts on two vendors.** This times when a reset
reached *these* accounts, which is not the same as when a fleet-wide rollout
completes. Google has no probe yet.

## What the verdicts mean

Confirmation across polls proves that a clear persisted, not who caused it.
Each probe first rules out every cause on the account's own side that it can
see: an account or plan switch, a spent banked credit, the Claude Code reset
menu. Two causes stay invisible: OpenAI's paid instant reset and a Claude
limit reset redeemed on the web. So an early clear counts as the vendor's
only with a **second witness**:

- **another of our accounts.** It must be a different account, cleared the
  same primary weekly window early within the hour, and passed the same
  checks; or
- **a trusted vendor post** saying the reset already happened, posted between
  one hour before and three hours after the clear. The post is marked
  confirmed, and the page draws one line under it, "Seen on our account 2 min
  before this post". The single account's clear stays private.

| Verdict | Meaning | Published |
|---|---|---|
| `natural_expiry` | Cleared at its scheduled expiry | yes |
| `vendor_reset` | Two of our accounts cleared the same primary weekly window early within an hour, each `unattributed`, not both at 95% or more | yes, beside its witness |
| `unattributed` | Cleared early, with every own-cause the probe can see checked and excluded | **no**, only as the timing on a post it confirms |
| `account_change` | The account or plan differs across the clear; not a reset | **no** |
| `self_applied` | A banked credit left (Codex), or the Claude Code reset menu was opened in the hour before (Claude) | **no** |
| `unresolved` | Cleared early but a check could not run, or no expiry anchor on record | **no** |

A `vendor_reset` pair with no post becomes one incident, mailed once after a
120-minute hold. `scripts/attribution.py` decides it for the site, the
announcement annotations and the email alike. Legacy `global_candidate`
records read as `unresolved` and carry no account fingerprint, so they never
pair. A banked-credit grant is a separate event from a clear, and timing one
near an announcement proves nothing about anyone's quota. Announcements stay
visible as their sources' claims, with the scope those sources state. Private
samples and event logs are kept for review. The full rule and every historical
clear, attributed:
[docs/reset-attribution.md](docs/reset-attribution.md).

Official contracts: [OpenAI reset Bank and redemption](https://learn.chatgpt.com/docs/app-server)
and [Claude personal limit resets](https://support.claude.com/en/articles/17007452-what-is-a-limit-reset).
Neither probe redeems a credit while monitoring.

## Layout

```
data/            one JSON file per vendor — the tracked announcements
scripts/
  fetch_openai.py     pulls a public reset-tracking API
  fetch_anthropic.py  pulls a public reset-tracking site, verifies each post
  discover_posts.py   announcement discovery
  quota_probe.py      ground truth: our Codex accounts' own quota (b when configured)
  claude_probe.py     ground truth: our two Claude accounts' own quota
  antigravity_probe.py  ground truth for Google (needs one interactive login)
  attribution.py      the two-account rule: when a clear is the vendor's
  groundtruth.py      joins observations to announcements
  incidents.py        classifies an announcement (reset / banked / policy)
  build.py            data feed + crawlable HTML and schema snapshots
  check_site.py       deterministic SEO and static-asset checks
  subscriptions.py    double-opt-in API and new-event email notifier
  coverage_gate.py    stdlib-only line coverage with per-module floors
site/            index.template.html + static assets; generated output ignored
infra/           units/ templates, install-units.sh, nginx example, publish.sh
site.config.json this deployment's URL and maintainer — a fork edits this
```

Everything is the Python **standard library**. There are no runtime
dependencies to install and no lockfile to keep current.

## Data sources

| Vendor | Source | Automation |
|---|---|---|
| OpenAI | a public, unauthenticated reset-tracking API | fully automated, every five minutes |
| Anthropic | a public reset-tracking site, each post verified against its original | fully automated, every five minutes |
| Google | vendor blogs | curated by hand in `data/google.json`, a few times a year |

Google's changes are discrete permanent policy shifts rather than recurring
resets, which is why it has no fetcher.

## Running it locally

```bash
python3 scripts/fetch_openai.py       # refresh data/openai.json
python3 scripts/build.py              # merge data/*.json -> site/data.json
python3 scripts/check_site.py         # the checks that gate a publish
python3 -m http.server 8000 --directory site
```

`build.py` renders the ignored `site/index.html` and `site/analytics.js` from
tracked templates, so a checkout that serves the site never goes dirty on a
cron tick.

**Analytics is off unless you switch it on.** `site/analytics.template.js` has
no key in it. Set `AI_RESETS_POSTHOG_KEY` to your own project key and `build.py`
renders the beacon; leave it unset and it writes an inert file so the pages that
reference it do not 404. On a host that publishes from cron, put the key in
`/etc/ai-resets/publish.env` (see `infra/publish.env.example`) rather than on
the cron line. The beacon uses no SDK, cookies, local storage, session
replay, query strings, form values, persistent profile or GeoIP enrichment, and
honours GPC and DNT. `check_site.py` enforces all of that on every publish.

## Deploying

Any host with nginx, Python 3.11+ and systemd. Nothing is specific to one
machine: the units are templates and the installer fills them in.

```bash
# 1. point the site at itself
$EDITOR site.config.json
# the canonical URL also appears in the hand-written pages that build.py does
# not template — methodology.html, privacy.html, llms.txt, robots.txt

# 2. install the systemd units, rendered for this host
./infra/install-units.sh --show      # what it would write, and the diff
sudo -E ./infra/install-units.sh

# 3. serve it
sudo cp infra/nginx.conf.example /etc/nginx/sites-available/ai-resets   # then edit
sudo nginx -t && sudo systemctl reload nginx
```

`install-units.sh` derives the user, the repository path, the Codex binary and
homes, and the credential paths from the host it runs on. Override any of them by
exporting the matching variable first; `--show` prints what it resolved.

A cron entry refreshes the site every five minutes. `flock` prevents
overlapping refreshes when an upstream request stalls:

```
*/5 * * * * flock -n /tmp/ai-resets-publish.lock env AI_RESETS_DEPLOY_TARGET=/srv/ai-resets bash /path/to/ai-resets/infra/publish.sh >> /path/to/ai-resets/publish.log 2>&1
```

A fetcher that fails does not take the site down: the publish keeps the last
good data, stamps the source as stale, prints one line and exits 0.

### The Codex probe

`ai-resets-probe.service` polls every 60 s and appends to
`quota_samples.jsonl`, `quota_events.jsonl` and `upstream_snapshots.jsonl`
under its systemd `StateDirectory`. It shells out to the Codex CLI, so that
binary must exist; `install-units.sh --show` reports whether it found one.
It also reads a second Codex account, the witness the two-account rule needs,
when `AI_RESETS_CODEX_HOME_B` names its home: the installer defaults it to
`~/.codex-profiles/main1` when that home holds an `auth.json`, and exporting
it empty turns the second account off. Its rows carry a `b:` prefix and its
health block is `codex:b` ([docs/reset-attribution.md](docs/reset-attribution.md)).

### The Claude probe

`ai-resets-claude-probe.service` reads the editor's own OAuth token, **read
only**. It never refreshes the token: refresh tokens rotate, and refreshing one
here would sign the operator out of their own editor. The endpoint throttles
per token, so **never poll faster than 300 s per account** — the unit and the
code both enforce this floor.

### The Google probe

`antigravity_probe.py` speaks to the Antigravity CLI, which has no login
subcommand: it opens a Google consent page and waits for an authorization code
bound to that process. `scripts/antigravity_login.sh` splits that so
the only human step is the browser. Until someone completes it, the probe
prints one actionable line and does nothing else.

## Email subscriptions

The page posts to a small Python API bound to `127.0.0.1:8787` behind nginx.
Subscriber state lives in a SQLite database under the service's state
directory. The Resend key and the HMAC signing secret are stored as
host-encrypted systemd credentials, never in a file in this repository.

1. A visitor chooses one or more providers, enters an email address, and
   receives a 24-hour confirmation link.
2. The link opens a review page; a deliberate POST activates alerts. This keeps
   email-security scanners from confirming subscriptions by accident.
3. Historical events are never backfilled. The notify timer checks the built
   feed every five minutes and sends only new matching provider events.
4. Every alert carries signed provider-preference and unsubscribe links plus
   RFC 8058 one-click unsubscribe headers.
5. Signed webhooks suppress addresses after bounces, complaints or provider
   suppressions. Webhook IDs are stored idempotently.

Setup, after installing the units:

```bash
# the sending identity — REQUIRED, there is no built-in default
sudo install -m 0644 infra/service.env.example /etc/ai-resets/service.env
sudo $EDITOR /etc/ai-resets/service.env

# where operational alerts go — optional; without it they stay in the journal
sudo install -m 0644 infra/notify.env.example /etc/ai-resets/notify.env
sudo $EDITOR /etc/ai-resets/notify.env

# the secrets, as host-encrypted credentials
systemd-creds encrypt --name=RESEND_API_KEY - /etc/credstore.encrypted/RESEND_API_KEY
systemd-creds encrypt --name=SUBSCRIPTION_SECRET - /etc/credstore.encrypted/SUBSCRIPTION_SECRET
systemd-creds encrypt --name=RESEND_WEBHOOK_SECRET - /etc/credstore.encrypted/RESEND_WEBHOOK_SECRET
```

Run the baseline once before opening the public form, so existing events are
not mailed to the first subscriber:

```bash
sudo systemctl start ai-resets-notify.service
sudo systemctl enable --now ai-resets-subscribe.service ai-resets-notify.timer
```

`notify --dry-run` prints what would be sent without sending or signing
anything, and works without the encrypted credentials.

## Tests

There is no hosted CI. A full local run is the gate:

```bash
python3 -m unittest discover -s tests    # the suite
python3 scripts/coverage_gate.py         # the suite plus per-module floors
```

Tests use only temporary directories, temporary SQLite databases and a fake
mailer. Nothing in the suite reaches the network or touches a real credential.

## Licence

Code is MIT — see [LICENSE](LICENSE). The tracked announcements are quoted from
their authors and are not covered by it; see [NOTICE](NOTICE).
