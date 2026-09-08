# AI Reset Watch

A public record of when usage limits move on the coding assistants people
actually run: **Claude Code (Anthropic)**, **Codex / ChatGPT (OpenAI)** and
**Gemini (Google)**.

Most trackers can only repeat what a vendor said. This one also **measures**:
probes read the maintainer's own subscription quota straight from the vendor
APIs, so a window that clears can be told apart from a window that merely
expired on schedule. When an announcement and a measurement agree, the site
says so; when a reset is measured that nobody announced, it still shows up.

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
| A vendor reset observed on this account before the announcement post | 149 s early |
| A banked-credit announcement, versus the credit actually landing | 3 h 41 m late |

Announcements also go missing. A reset announced somewhere other than the
account the tracker watches is invisible to a feed-only tracker, and that is
not a hypothetical: it is why the probes exist.

So `quota_probe.py` (OpenAI) and `claude_probe.py` (Anthropic) read the
operator's own authenticated quota — metadata only, consuming no tokens and no
reset credit — and time resets directly. They are **observation-only and never
send mail**.

Three measured facts drive the detection logic:

1. **The two vendors have different window contracts.** Codex re-anchors
   `resets_at` on a real clear; Anthropic keeps a fixed per-account weekly
   lattice. The detector records which happened at detection time, so a single
   revert rule serves both. Getting this wrong once caused a confirmed reset to
   be retracted.
2. **The discriminator is the clear time against the window's active anchor.**
   A clear at or after the scheduled expiry is the window ending; a clear
   materially before it was forced.
3. **A forced clear is still ambiguous** between "the operator spent a banked
   credit" and "the vendor reset everyone". The credit counter in the live API
   decrements only in the first case, so the two separate locally.

A clear must persist across two polls before it is trusted: on one occasion a
weekly window went 91% to 0% in 3.7 s, held for 12 h 41 m, and then reverted
to 92%.

**Known limit: a handful of accounts on two vendors.** This times when a reset
reached *these* accounts, which is not the same as when a fleet-wide rollout
completes. Google has no probe yet.

## What the verdicts mean

Every observed clear gets exactly one verdict, and only some are ever
published:

| Verdict | Meaning | Published |
|---|---|---|
| `vendor_reset` | Cleared early, no banked credit was spent — a real reset | yes |
| `natural_expiry` | Cleared at or after the scheduled expiry | yes |
| `unresolved` | Cleared, but no anchor was on record to judge against | yes |
| `self_applied_credit` | The operator spent their own banked credit | **no** |

The fourth is withheld because it describes what one person did with their own
account and says nothing about the vendor. Publishing it would be both wrong
and private.

## Layout

```
data/            one JSON file per vendor — the tracked announcements
scripts/
  fetch_openai.py     pulls a public reset-tracking API
  fetch_anthropic.py  pulls a public reset-tracking site, verifies each post
  discover_posts.py   announcement discovery
  quota_probe.py      ground truth: this account's own Codex quota
  claude_probe.py     ground truth: this account's own Claude quota
  antigravity_probe.py  ground truth for Google (needs one interactive login)
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
reference it do not 404. The beacon uses no SDK, cookies, local storage, session
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
the credential paths from the host it runs on. Override any of them by
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
