#!/usr/bin/env bash
# Refresh data + publish the static site.
#
# Intended to run from a five-minute user cron on the host serving the site.
# Override AI_RESETS_DEPLOY_TARGET for another docroot; put anything else the
# host needs in /etc/ai-resets/publish.env, which is sourced below if present.
#
# Step policy (P0d, extended in P3): the EXPORT and FETCH steps are non-fatal,
# the BUILD and CHECK steps stay fatal. A third-party tracker being unreachable
# is normal — publish.log holds 52 fetcher tracebacks, and under the old
# unconditional `set -e` every one of them killed the tick before build.py ran,
# so Anthropic and Google went stale on the live site over an OpenAI tracker
# outage. The probe export is local and cannot hang, but the same rule applies
# for the same reason: a state directory that is unreadable this minute must
# cost the observation column, not the whole site. A broken build is the
# opposite case: build.py and check_site.py run BEFORE the rsync, so aborting
# there leaves the previously published docroot exactly as it was.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Host settings for the publish itself, if the host has any. The cron line
# passes one variable and should not grow into a place where configuration
# accumulates; systemd units read /etc/ai-resets/service.env, and this is the
# equivalent for the half of the system that runs from cron rather than under
# systemd. Optional by design: absent on a laptop, absent in CI, absent in the
# tests, and the publish behaves identically without it.
PUBLISH_ENV="${AI_RESETS_PUBLISH_ENV:-/etc/ai-resets/publish.env}"
if [ -r "$PUBLISH_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$PUBLISH_ENV"
  set +a
fi
DEPLOY_TARGET="${AI_RESETS_DEPLOY_TARGET:-/srv/ai-resets}"
# Path (not a command line) so the override cannot smuggle in extra arguments;
# tests/test_publish.py points it at a stub to exercise the failure branch.
FETCH_OPENAI="${AI_RESETS_FETCH_OPENAI:-scripts/fetch_openai.py}"
EXPORT_OBSERVED="${AI_RESETS_EXPORT_OBSERVED:-scripts/quota_probe.py}"
# The gitignored directory build.py reads observations from. Never a second
# data/<vendor>.json: load_vendors() globs data/*.json and would treat it as an
# announcement seed with no events and clobber the real one.
OBSERVED_TARGET="data/observed/openai.json"

cd "$REPO_ROOT"

# First, because it is the only local step: it reads /var/lib/ai-resets and
# makes no RPC and no network call, so it must not wait behind a tracker fetch
# that can sit on its 15-second timeout. This is what carries the probe's
# verdict — natural expiry, our own banked credit, or a clear nothing this
# account did explains — out of the state directory and onto the site.
echo "[publish] export observed: $EXPORT_OBSERVED -> $OBSERVED_TARGET"
export_status=0
python3 "$EXPORT_OBSERVED" --export "$OBSERVED_TARGET" || export_status=$?
if [ "$export_status" -ne 0 ]; then
  # build.py skips a missing or unreadable observed/<vendor>.json and publishes
  # the announcement columns anyway, so the site loses the observation rows for
  # this tick and nothing else. Loud, in the same shape as the fetch failure,
  # because a silent one would make "no observations" look like "no resets".
  echo "[publish] export observed FAILED (exit $export_status) — building with the previous $OBSERVED_TARGET"
fi

# Every optional source is wired through this one shape: skipped silently when
# the script is not present (a phase that has not landed yet), run non-fatally
# when it is, and loud on failure. Adding a source must never become a way to
# take the site down.
optional_step() {
  local what="$1" script="$2"; shift 2
  [ -f "$script" ] || return 0
  echo "[publish] $what: $script"
  local status=0
  python3 "$script" "$@" || status=$?
  if [ "$status" -ne 0 ]; then
    echo "[publish] $what FAILED (exit $status) — continuing with the previous data"
  fi
}

# The Claude probe's own export, once P2 ships it. build.py reads any
# data/observed/<vendor>.json, so this is the whole wiring.
optional_step "export observed anthropic" "scripts/claude_probe.py" \
  --export "data/observed/anthropic.json"

# Announcement discovery for Anthropic, once P1 ships it. Without it,
# data/anthropic.json is whatever was last hand-seeded.
optional_step "fetch anthropic" "scripts/fetch_anthropic.py"

# Hint sources (P5): Bluesky mirrors and, when a credential exists, the owner's
# X notification mailbox. Hints never mail anyone; they surface a post id for
# the verification path to confirm or drop.
optional_step "discover hints" "scripts/discover_posts.py"

echo "[publish] fetch openai: $FETCH_OPENAI"
fetch_status=0
python3 "$FETCH_OPENAI" || fetch_status=$?
if [ "$fetch_status" -ne 0 ]; then
  # fetch_openai.py already exits 0 on a soft failure and keeps the cached
  # file, so reaching here means something harder broke (a syntax error, a
  # full disk, a missing interpreter). Still not a reason to stop the site:
  # build.py reads whatever data/openai.json is on disk.
  echo "[publish] fetch openai FAILED (exit $fetch_status) — building with cached data/openai.json"
  # The fetcher stamps source.stale_since itself on a soft failure. Reaching
  # here means it never got that far, so stamp it from outside: a column that
  # stops updating has to be visible in the data, not only in this log.
  python3 scripts/fetch_openai.py --stamp-stale || \
    echo "[publish] could not stamp stale_since either"
fi

echo "[publish] build"
python3 scripts/build.py

echo "[publish] check"
python3 scripts/check_site.py

echo "[publish] deploy -> $DEPLOY_TARGET"
mkdir -p "$DEPLOY_TARGET"
rsync -a --delete --exclude 'index.template.html' --exclude 'analytics.template.js' site/ "$DEPLOY_TARGET/"

echo "published site/ -> $DEPLOY_TARGET"
