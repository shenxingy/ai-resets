#!/usr/bin/env bash
#
# Render infra/units/*.in for THIS host and install them.
#
# The units carry absolute paths — a working directory, a Python entry point, a
# Codex binary, two credential files — because systemd has no `~` and no shell.
# Writing one machine's paths into a public repository is how a repo ends up
# telling everyone its operator's username, so the tracked copies are templates
# and this script fills them in.
#
# Nothing has to be configured for the ordinary case: the repository is found
# from this script's own location, the user from whoever invoked sudo, the
# Codex binary by looking where its installer puts it. Every value can be
# overridden by exporting it first, and `--show` prints what would be used
# without touching the system.
#
#   ./infra/install-units.sh --show          # print the resolved values and diff
#   ./infra/install-units.sh --render NAME   # print one rendered unit, install nothing
#   sudo -E ./infra/install-units.sh         # render, install, daemon-reload
#
set -euo pipefail

UNIT_DIR="/etc/systemd/system"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/infra/units"

die() { printf 'install-units: %s\n' "$1" >&2; exit 1; }

# ─── Resolve this host's values ──────────────────────────────────────────────

# SUDO_USER, not USER: this runs under sudo, and installing units that say
# `User=root` for a service designed to run unprivileged would be a silent
# and serious downgrade.
: "${AI_RESETS_USER:=${SUDO_USER:-$USER}}"
[ "$AI_RESETS_USER" = "root" ] && die \
  "refusing to render User=root; run with sudo from your own account, or set AI_RESETS_USER"

: "${AI_RESETS_GROUP:=$(id -gn "$AI_RESETS_USER")}"
: "${AI_RESETS_REPO:=$REPO_ROOT}"

# The account's home, as the passwd database gives it. $HOME under sudo is
# root's, which would point every path at the wrong tree.
USER_HOME="$(getent passwd "$AI_RESETS_USER" | cut -d: -f6)"
[ -n "$USER_HOME" ] || die "cannot resolve the home directory of $AI_RESETS_USER"

# The probe's Codex home, not the operator's interactive one. A login shell
# that exports CODEX_HOME (e.g. a profile-based setup under ~/.codex-profiles)
# must not redirect the probe: the unit would point ReadWritePaths at a home
# the spawned codex never uses while its real one stays read-only. Override
# with AI_RESETS_CODEX_HOME if the probe's account ever moves.
: "${AI_RESETS_CODEX_HOME:=$USER_HOME/.codex}"
CODEX_HOME="$AI_RESETS_CODEX_HOME"
# The second Codex account's home, read as account b (docs/reset-attribution.md,
# "Second Codex account"). Its own variable for the same reason: an exported
# CODEX_HOME must move neither account. Unset, it is the profile home that
# holds a second login, when one is there; exported empty, there is no second
# account. Empty renders the probe unit without b's lines at all.
if [ -z "${AI_RESETS_CODEX_HOME_B+set}" ]; then
  if [ -f "$USER_HOME/.codex-profiles/main1/auth.json" ]; then
    AI_RESETS_CODEX_HOME_B="$USER_HOME/.codex-profiles/main1"
  else
    AI_RESETS_CODEX_HOME_B=""
  fi
fi
CODEX_HOME_B="$AI_RESETS_CODEX_HOME_B"
# `codex` on PATH is often a wrapper, and systemd's PATH excludes ~/.local/bin
# in any case, so prefer the real binary the standalone package installs. The
# `current` component is a symlink the package updates, so upgrades follow.
if [ -z "${CODEX_BIN:-}" ]; then
  if [ -x "$CODEX_HOME/packages/standalone/current/bin/codex" ]; then
    CODEX_BIN="$CODEX_HOME/packages/standalone/current/bin/codex"
  else
    CODEX_BIN="$(command -v codex || true)"
  fi
fi
: "${CLAUDE_CREDS_A:=$USER_HOME/.claude/.credentials.json}"
: "${CLAUDE_CREDS_B:=$USER_HOME/.claude-profiles/main1/.credentials.json}"

PUBLIC_URL="${PUBLIC_URL:-$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["public_url"])' "$REPO_ROOT/site.config.json")}"

render() {
  # With no second Codex home, every line naming it is left out rather than
  # rendered empty: `ReadWritePaths=` with nothing after it RESETS the list,
  # which would take account a's own write path with it and hang its reads.
  local second='/__CODEX_HOME_B__/d'
  if [ -n "$CODEX_HOME_B" ]; then
    second="s|__CODEX_HOME_B__|$CODEX_HOME_B|g"
  fi
  sed \
    -e "$second" \
    -e "s|__AI_RESETS_USER__|$AI_RESETS_USER|g" \
    -e "s|__AI_RESETS_GROUP__|$AI_RESETS_GROUP|g" \
    -e "s|__AI_RESETS_REPO__|$AI_RESETS_REPO|g" \
    -e "s|__CODEX_BIN__|$CODEX_BIN|g" \
    -e "s|__CODEX_HOME__|$CODEX_HOME|g" \
    -e "s|__CLAUDE_CREDS_A__|$CLAUDE_CREDS_A|g" \
    -e "s|__CLAUDE_CREDS_B__|$CLAUDE_CREDS_B|g" \
    -e "s|__PUBLIC_URL__|$PUBLIC_URL|g" \
    "$1"
}

units() { find "$SRC" -maxdepth 1 -name '*.in' -printf '%f\n' | sed 's/\.in$//' | sort; }

report() {
  cat <<TXT
resolved for this host:
  AI_RESETS_USER   $AI_RESETS_USER
  AI_RESETS_GROUP  $AI_RESETS_GROUP
  AI_RESETS_REPO   $AI_RESETS_REPO
  CODEX_BIN        ${CODEX_BIN:-(not found — the Codex probe will not start)}
  CODEX_HOME       $CODEX_HOME
  CODEX_HOME_B     ${CODEX_HOME_B:-(none — the Codex probe reads one account)}
  CLAUDE_CREDS_A   $CLAUDE_CREDS_A
  CLAUDE_CREDS_B   $CLAUDE_CREDS_B
  PUBLIC_URL       $PUBLIC_URL
TXT
}

# ─── Act ─────────────────────────────────────────────────────────────────────

if [ "${1:-}" = "--render" ]; then
  [ -n "${2:-}" ] && [ -f "$SRC/${2}.in" ] || die "usage: $0 --render <unit name>, e.g. ai-resets-probe.service"
  render "$SRC/${2}.in"
  exit 0
fi

if [ "${1:-}" = "--show" ]; then
  report
  echo
  echo "against what is installed now:"
  changed=0
  while read -r name; do
    if [ -f "$UNIT_DIR/$name" ]; then
      if render "$SRC/$name.in" | diff -u "$UNIT_DIR/$name" - >/dev/null; then
        echo "  same     $name"
      else
        echo "  DIFFERS  $name"
        # `|| true` because a difference is the thing being reported, and
        # `set -o pipefail` would otherwise make diff's exit 1 abort the loop
        # at the first unit that differs — the loop would stop exactly when it
        # had something to say.
        render "$SRC/$name.in" | diff -u --label "$UNIT_DIR/$name" --label "rendered" "$UNIT_DIR/$name" - | sed 's/^/    /' || true
        changed=1
      fi
    else
      echo "  new      $name"
      changed=1
    fi
  done < <(units)
  [ "$changed" = 0 ] && echo "  nothing to install"
  exit 0
fi

[ "$(id -u)" = 0 ] || die "installing into $UNIT_DIR needs root: sudo -E $0"
[ -n "${CODEX_BIN:-}" ] || echo "install-units: warning: no codex binary found; the Codex probe will fail to start" >&2

report
while read -r name; do
  render "$SRC/$name.in" > "$UNIT_DIR/$name"
  chmod 0644 "$UNIT_DIR/$name"
  echo "installed $UNIT_DIR/$name"
done < <(units)
systemctl daemon-reload
echo "daemon-reload done. Enable what this host should run, e.g.:"
echo "  systemctl enable --now ai-resets-probe ai-resets-subscribe ai-resets-notify.timer"
