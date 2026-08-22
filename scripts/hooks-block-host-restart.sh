#!/bin/bash
# PreToolUse hook: an agent may not restart the service it is running inside.
#
# Serena's chat panes are children of serena-mobile-host. Restarting that
# service kills every open terminal, including the one issuing the command, so
# the agent's own `systemctl restart` never returns and the pane hangs forever
# on a command that cannot finish. It has also stranded panes: since each one
# now lives in its own systemd scope, a host restart leaves the child process
# running with nobody holding the other end of its PTY, unreachable and still
# holding memory. One survived 22 hours that way.
#
# Restarting is fine, it is just not an agent's call to make mid-work. Raghav
# does it when a stretch of coding is finished, or an agent asks him for it.
#
# This file lives in the synced ~/.claude folder and runs on BOTH machines, so
# keep it cross-platform: no jq (absent in Windows git-bash), python for JSON.
#
# Deliberate escape hatch for the end-of-work restart:
#   SERENA_ALLOW_HOST_RESTART=1 systemctl --user restart serena-mobile-host

input=$(cat)
PY=$(command -v python3 || command -v python) || exit 0
tool=$(printf '%s' "$input" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('tool_name') or '')" 2>/dev/null)

[ "$tool" = "Bash" ] || exit 0
[ "${SERENA_ALLOW_HOST_RESTART:-}" = "1" ] && exit 0

cmd=$(printf '%s' "$input" | "$PY" -c "import json,sys;print((json.load(sys.stdin).get('tool_input') or {}).get('command') or '')" 2>/dev/null)

# The command itself carries the opt-in, so an explicitly authorised restart
# still gets through even though the hook's own environment lacks the flag.
echo "$cmd" | grep -q 'SERENA_ALLOW_HOST_RESTART=1' && exit 0

# The services whose lifetime owns the panes. serena-desk spawns agent work the
# same way, so it gets the same protection.
GUARDED='serena-mobile-host|serena-desk'

# Match at command position only: start of line or after a shell separator, so
# the words are not blocked when they appear as data inside a quoted argument
# (writing docs, grepping logs, editing this hook).
A='(^|[;&|(`]|&&|\|\|)[[:space:]]*'

fix='Restarting it kills every open pane, including yours, so the command never returns. Ask Raghav to restart it when the current work is finished, or run it with SERENA_ALLOW_HOST_RESTART=1 if he has explicitly asked for the restart now.'

if echo "$cmd" | grep -qE "${A}(sudo[[:space:]]+)?systemctl([[:space:]]+--user)?[[:space:]]+(restart|stop|kill|try-restart|reload-or-restart)([[:space:]]+[^[:space:]]+)*[[:space:]]+[^[:space:]]*(${GUARDED})"; then
  echo "BLOCKED — do not restart the service your own terminal runs inside. $fix" >&2
  exit 2
fi

# The loop form, which is how this actually happened:
#   for s in serena-work-supervisor serena-mobile-host; do
#     timeout 90 systemctl --user restart $s
#   done
# The service name reaches systemctl as a variable, so the literal never sits
# next to the verb. Catch a restart verb and a guarded name in one command.
# No command-position anchor here on purpose: in a loop the call sits after
# "do", not after a shell separator, which is precisely how the real one got
# through. Needing BOTH a systemctl restart verb AND a guarded service name in
# the same command keeps this from firing on prose or on reading a unit file.
if echo "$cmd" | grep -qE "systemctl([[:space:]]+--user)?[[:space:]]+(restart|stop|kill|try-restart|reload-or-restart)([[:space:]]|$)" \
   && echo "$cmd" | grep -qE "(${GUARDED})"; then
  echo "BLOCKED — this restarts the service your own terminal runs inside. $fix" >&2
  exit 2
fi

# The same suicide by another route.
if echo "$cmd" | grep -qE "${A}(sudo[[:space:]]+)?(pkill|killall)([[:space:]]+-[^[:space:]]+)*[[:space:]]+.*(mobile_host|core\.mobile_host)"; then
  echo "BLOCKED — that kills the host process your terminal is a child of. $fix" >&2
  exit 2
fi

exit 0
