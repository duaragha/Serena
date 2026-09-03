#!/usr/bin/env bash
# Restart a Serena host unit without dying halfway through it.
#
# Chat panes are children of serena-mobile-host, so a restart issued from a
# pane kills the shell running the command. Sometimes systemd finishes anyway,
# sometimes the restart is left half applied, and either way the caller never
# learns the outcome: it just gets SIGKILL. On 2026-08-21 three restarts in a
# row came back as exit 137 with no way to tell what had actually happened.
#
# systemd-run puts the work in its own transient unit, outside the target's
# cgroup. The pane still dies, which is unavoidable, but the restart survives
# it, runs to completion, and writes its result to a log that can be read once
# the new host is up.
#
# Usage:  scripts/serena-host-restart.sh [unit] [delay-seconds]
set -uo pipefail

UNIT="${1:-serena-mobile-host.service}"
DELAY="${2:-2}"
STATE="$HOME/.local/state/serena"
LOG="$STATE/host-restart.log"
mkdir -p "$STATE"

# This script would otherwise be a way around the PreToolUse guard, since its
# command line carries no restart verb for the hook to match. The rule it
# enforces is Raghav's, not the hook's, so the script honours it directly:
# restarts happen at the end of a stretch of work, and he calls it.
if [ "${SERENA_ALLOW_HOST_RESTART:-0}" != "1" ]; then
  echo "refusing: restarting ${UNIT} kills every open pane." >&2
  echo "Raghav calls this one. Re-run with SERENA_ALLOW_HOST_RESTART=1 once he has." >&2
  exit 3
fi

# Only the units whose lifetime owns the panes. Anything else should be
# restarted directly, where the caller can see the result immediately.
case "$UNIT" in
  serena-mobile-host.service | serena-desk.service | serena-dot-overlay.service) ;;
  *)
    echo "refusing to detach-restart $UNIT; restart it directly instead" >&2
    exit 2
    ;;
esac

# The delay is what lets the caller print a final message and flush its
# transcript before the pane is torn out from under it.
systemd-run --user --collect --quiet \
  --unit="serena-host-restart-$$" \
  --description="Detached restart of $UNIT" \
  /bin/bash -c "
    sleep ${DELAY}
    {
      echo \"=== \$(date -Is) restarting ${UNIT} ===\"
      systemctl --user restart '${UNIT}'
      echo \"exit=\$?\"
      sleep 8
      echo \"state:    \$(systemctl --user is-active '${UNIT}')\"
      echo \"restarts: \$(systemctl --user show '${UNIT}' -p NRestarts --value)\"
    } >>'${LOG}' 2>&1
  "

echo "queued a detached restart of ${UNIT} in ${DELAY}s"
echo "read the outcome afterwards with: tail -20 ${LOG}"
