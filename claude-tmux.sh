#!/bin/bash
# claude-tmux: Run Claude Code in tmux with auto-attach to team mode agents
# Only attaches to swarms spawned by THIS Claude process, not pre-existing ones.
# Usage: claude-tmux [claude args...]

set -e

CLAUDE_ARGS=("$@")
SESSION_NAME="claude-$$"
UID_NUM=$(id -u)
SOCKET_DIR="${TMUX_TMPDIR:-/tmp/tmux-${UID_NUM}}"

# Snapshot existing swarm sockets BEFORE launching Claude
declare -A PRE_EXISTING
for sock in "$SOCKET_DIR"/claude-swarm-*; do
    [ -S "$sock" ] && PRE_EXISTING["$(basename "$sock")"]=1
done

# If already inside tmux, run Claude directly and just watch for swarms
if [ -n "$TMUX" ]; then
    OUTER_PANE=$(tmux display-message -p '#{pane_id}')
    OUTER_SESSION=$(tmux display-message -p '#{session_name}')

    # Start watcher in background
    (
        ATTACHED=""
        while true; do
            # Check if Claude's pane still exists
            tmux list-panes -t "$OUTER_SESSION" -F '#{pane_id}' 2>/dev/null | grep -q "$OUTER_PANE" || break

            for sock in "$SOCKET_DIR"/claude-swarm-*; do
                [ -S "$sock" ] || continue
                SWARM_NAME=$(basename "$sock")

                # Skip pre-existing swarms
                [ "${PRE_EXISTING[$SWARM_NAME]}" = "1" ] && continue

                if [ "$ATTACHED" != "$SWARM_NAME" ]; then
                    ATTACHED="$SWARM_NAME"
                    tmux split-window -t "$OUTER_PANE" -h \
                        "tmux -L $SWARM_NAME a 2>/dev/null; echo 'Agents finished. Press enter to close.'; read"
                    tmux resize-pane -t "$OUTER_PANE" -x '40%' 2>/dev/null || true
                fi
            done

            # If swarm socket gone and we had attached, reset
            if [ -n "$ATTACHED" ] && [ ! -S "$SOCKET_DIR/$ATTACHED" ]; then
                ATTACHED=""
            fi

            sleep 2
        done
    ) &
    WATCHER_PID=$!

    # Run Claude in the current pane
    claude "${CLAUDE_ARGS[@]}"

    # Cleanup watcher
    kill $WATCHER_PID 2>/dev/null
    wait $WATCHER_PID 2>/dev/null
    exit 0
fi

# Not in tmux — create a new tmux session
tmux new-session -d -s "$SESSION_NAME" -x "$(tput cols)" -y "$(tput lines)"

# Send the claude command to the session
ESCAPED_ARGS=""
for arg in "${CLAUDE_ARGS[@]}"; do
    ESCAPED_ARGS+=" $(printf '%q' "$arg")"
done
tmux send-keys -t "$SESSION_NAME" "claude${ESCAPED_ARGS}; exit" Enter

# Start background watcher for swarm sessions
(
    ATTACHED=""
    while tmux has-session -t "$SESSION_NAME" 2>/dev/null; do
        for sock in "$SOCKET_DIR"/claude-swarm-*; do
            [ -S "$sock" ] || continue
            SWARM_NAME=$(basename "$sock")

            # Skip pre-existing swarms
            [ "${PRE_EXISTING[$SWARM_NAME]}" = "1" ] && continue

            if [ "$ATTACHED" != "$SWARM_NAME" ]; then
                ATTACHED="$SWARM_NAME"
                tmux split-window -t "$SESSION_NAME" -h \
                    "tmux -L $SWARM_NAME a 2>/dev/null; echo 'Agents finished. Press enter to close.'; read"
                tmux resize-pane -t "${SESSION_NAME}:.0" -x '40%' 2>/dev/null || true
            fi
        done

        # Reset if swarm socket gone
        if [ -n "$ATTACHED" ] && [ ! -S "$SOCKET_DIR/$ATTACHED" ]; then
            ATTACHED=""
        fi

        sleep 2
    done
) &
WATCHER_PID=$!

# Attach to the session
tmux attach -t "$SESSION_NAME"

# Cleanup
kill $WATCHER_PID 2>/dev/null
wait $WATCHER_PID 2>/dev/null
