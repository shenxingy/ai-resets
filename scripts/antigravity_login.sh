#!/usr/bin/env bash
#
# One interactive Google sign-in for the Antigravity CLI, split so that the only
# part a human must do is the part only a human can do.
#
# `agy` has no login subcommand. It authenticates by opening a Google consent
# page and waiting for an authorization code to be pasted back into the SAME
# live process: the code is redeemed against a PKCE challenge generated at
# startup, so a code obtained from one run cannot be given to another. That is
# what makes this un-scriptable end to end, and it is also why the two halves
# below must share one long-lived process.
#
# The non-interactive form (`agy -p ... --output-format json`) is worse for this
# purpose, not better: measured on 2026-09-07 it waits only 60 s for the code
# and then exits 1. The interactive form holds the prompt open, so a person has
# time to actually open a browser. This script therefore drives the interactive
# form inside tmux and reads the pane.
#
#   ./scripts/antigravity_login.sh start        # prints the URL to open
#   ./scripts/antigravity_login.sh code <CODE>  # pastes the code back
#   ./scripts/antigravity_login.sh status       # is this host signed in?
#   ./scripts/antigravity_login.sh cancel       # tear the session down
#
set -euo pipefail

SESSION="${AI_RESETS_AGY_SESSION:-ai-resets-agy-login}"
AGY="${AI_RESETS_AGY:-agy}"
export PATH="$HOME/.local/bin:$PATH"

die() { printf 'antigravity login: %s\n' "$1" >&2; exit 1; }

command -v tmux >/dev/null 2>&1 || die "tmux is required to hold the prompt open"
command -v "$AGY" >/dev/null 2>&1 || die \
  "$AGY is not on PATH — install it with \`curl -fsSL https://antigravity.google/cli/install.sh | bash\`"

pane() { tmux capture-pane -J -p -t "$SESSION" 2>/dev/null || true; }

# Reassemble the consent URL from the pane.
#
# `capture-pane -J` joins lines that TMUX wrapped, and these are not those: the
# CLI itself emits one newline per display row to fit its box, so the URL
# arrives as six separate real lines with a leading space each. Rebuild it by
# concatenating from the https:// line until the box border or the next prose
# line, and let the caller check the result is whole.
consent_url() {
  awk '
    /https:\/\/accounts\.google\.com\/o\/oauth2\/auth/ { collecting = 1 }
    collecting {
      line = $0
      gsub(/[[:space:]]/, "", line)
      # The box border, a blank row, or the next instruction ends the URL.
      if (line == "" || line ~ /^[^A-Za-z0-9]/ || line ~ /^After/) { exit }
      printf "%s", line
    }
  ' | head -c 4096
}
alive() { tmux has-session -t "$SESSION" 2>/dev/null; }

# Does an authenticated call actually work? This is the only honest test of
# "signed in": a token file can exist and be expired, refused, or for the wrong
# account, and none of that shows up in a directory listing.
#
# Bounded, because the unauthenticated case is the SLOW one. `agy -p` reacts to
# a missing login by printing a consent URL and waiting a full 60 s for a code
# that stdin will never supply — redirecting from /dev/null does not shorten it.
# So an unbounded check makes `status` on a signed-out host, the exact host this
# script exists for, hang for a minute per call. A signed-in `/usage` answers in
# seconds; anything that outlasts this budget is the auth prompt, not an answer.
CHECK_TIMEOUT="${AI_RESETS_AGY_CHECK_TIMEOUT:-25}"

signed_in() {
  local out
  out="$(timeout "$CHECK_TIMEOUT" "$AGY" -p "/usage" --output-format json </dev/null 2>/dev/null || true)"
  [ -n "$out" ] && ! printf '%s' "$out" | grep -q '"status" *: *"ERROR"'
}

# Read the URL off a live session and print it with the instructions. Split out
# so that re-running `start` on an already-open session SHOWS the URL again
# instead of refusing: the prompt can sit for hours, and the person who comes
# back to it later should not have to know that a tmux session is what is
# holding their place.
show_url() {
  local url
  url="$(pane | consent_url)"
  # Validated, not merely non-empty. The CLI draws the URL inside a box and
  # HARD-WRAPS it across six lines, so a naive grep returns the first line and
  # nothing warns: `...apps.googleus` is a perfectly non-empty string and a
  # completely dead link. `state` is the last parameter in the query, so a URL
  # carrying both of these is one that was reassembled all the way to the end.
  case "$url" in
    *client_id=*state=*) : ;;
    *) die "could not read the whole consent URL off the pane (the CLI's layout may have changed).
Attach and copy it by hand:  tmux attach -t $SESSION   (detach with ctrl-b d)" ;;
  esac
  cat <<TXT

Open this in a browser signed in to the Google account that holds the
Antigravity subscription, approve it, and copy the code it shows:

$url

Then paste it back with:

  $0 code <CODE>

The prompt stays open in tmux session '$SESSION' until you do. The code is
bound to this process, so do not restart anything in between.
TXT
}

case "${1:-}" in
  start)
    if alive; then
      echo "a login session is already open; here is its URL again." >&2
      show_url
      exit 0
    fi
    signed_in && { echo "already signed in; nothing to do"; exit 0; }
    tmux new-session -d -s "$SESSION" -x 220 -y 50 "$AGY"
    # Wait for the login menu rather than sleeping a guessed interval: the
    # binary is 200 MB and its cold start is not a constant.
    for _ in $(seq 1 40); do
      pane | grep -q "Select login method" && break
      sleep 1
    done
    pane | grep -q "Select login method" || { tmux kill-session -t "$SESSION"; die "no login menu appeared in 40s"; }
    # "1. Google OAuth" is preselected. Option 2 is a Google Cloud project,
    # which bills a GCP quota rather than reading the subscription quota this
    # project measures — the wrong ground truth, so never select it here.
    tmux send-keys -t "$SESSION" Enter
    for _ in $(seq 1 30); do
      pane | grep -q "accounts.google.com" && break
      sleep 1
    done
    show_url
    ;;

  code)
    [ -n "${2:-}" ] || die "usage: $0 code <CODE>"
    alive || die "no login session is open — run \`$0 start\` first"
    tmux send-keys -t "$SESSION" "$2"
    tmux send-keys -t "$SESSION" Enter
    # Bounded. The code is redeemed in one round trip, so if it has not taken
    # within a couple of checks it is not going to, and a long poll here just
    # holds the operator at a prompt that will never change.
    for _ in $(seq 1 5); do
      signed_in && break
      sleep 2
    done
    if signed_in; then
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      echo "signed in — \`$AGY -p '/usage' --output-format json\` now answers"
      echo "next: save that first answer as tests/fixtures/antigravity-usage.json"
      echo "      and re-check extract_buckets against the real shape"
    else
      echo "still not signed in. The pane says:" >&2
      pane | tail -12 >&2
      exit 1
    fi
    ;;

  status)
    if signed_in; then echo "signed in"; else echo "not signed in"; exit 1; fi
    ;;

  cancel)
    alive && tmux kill-session -t "$SESSION" && echo "login session closed" || echo "no login session was open"
    ;;

  *)
    sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
