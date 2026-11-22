#!/usr/bin/env bash
# cmux-chat.sh — Have a conversation with a cmux workspace agent
# Usage: bash cmux-chat.sh [workspace-name]
#   If no name given, lists workspaces and prompts you to pick one.

CMUX="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
POLL_INTERVAL=3
TIMEOUT=300

# Shared inject guard so we never clobber a draft or a mid-turn agent in the
# TARGET workspace (you may be typing there, or a workstream may be running).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/cmux-inject-guard.sh"

# --- Find workspace ---
pick_workspace() {
    echo "Available workspaces:"
    echo ""
    local i=1
    local refs=()
    local names=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local ref name
        ref=$(echo "$line" | grep -oE 'workspace:[0-9]+' || true)
        [[ -z "$ref" ]] && continue
        name=$(echo "$line" | sed -E 's/^[* ]*workspace:[0-9]+  //' | sed 's/ *\[selected\]$//')
        echo "  $i) $name"
        refs+=("$ref")
        names+=("$name")
        ((i++))
    done <<< "$("$CMUX" list-workspaces 2>/dev/null)"

    echo ""
    read -rp "Pick a workspace (number): " choice
    local idx=$((choice - 1))
    if [[ $idx -lt 0 || $idx -ge ${#refs[@]} ]]; then
        echo "Invalid choice."
        exit 1
    fi
    WS_REF="${refs[$idx]}"
    WS_NAME="${names[$idx]}"
}

resolve_workspace() {
    local target="$1"
    local workspace_listing
    workspace_listing=$("$CMUX" list-workspaces 2>/dev/null)
    WS_REF=$(printf '%s\n' "$workspace_listing" | grep -i "$target" | grep -oE 'workspace:[0-9]+' | head -1)
    WS_NAME="$target"
    if [[ -z "$WS_REF" ]]; then
        echo "No workspace matching '$target' found."
        exit 1
    fi
}

get_surface() {
    local tree_output
    tree_output=$("$CMUX" tree --workspace "$WS_REF" 2>/dev/null)
    SURFACE=$(printf '%s\n' "$tree_output" | grep -oE 'surface:[0-9]+' | head -1)
    if [[ -z "$SURFACE" ]]; then
        echo "No surface found for $WS_REF"
        exit 1
    fi
}

read_screen() {
    "$CMUX" read-screen --workspace "$WS_REF" --surface "$SURFACE" --lines "${1:-80}" 2>/dev/null
}

is_idle() {
    cig_safe "$WS_REF"
}

wait_for_response() {
    local elapsed=0
    # First, wait a beat for the agent to start working
    sleep 2

    while (( elapsed < TIMEOUT )); do
        if is_idle; then
            return 0
        fi
        sleep "$POLL_INTERVAL"
        ((elapsed += POLL_INTERVAL))
        # Show a dot so you know it's still waiting
        printf "." >&2
    done
    echo "" >&2
    echo "(timed out after ${TIMEOUT}s)" >&2
    return 1
}

# --- Main ---
if [[ -n "${1:-}" ]]; then
    resolve_workspace "$1"
else
    pick_workspace
fi
get_surface

echo "💬 Chatting with: $WS_NAME ($WS_REF)"
echo "   Type your message and press Enter. Ctrl+C to quit."
echo ""

# Snapshot current screen so we only show NEW output after each message
LAST_SCREEN=$(read_screen 200)

while true; do
    read -rp "you> " msg
    [[ -z "$msg" ]] && continue

    # Send the message — guarded so we don't land on top of a draft you left
    # in this workspace or a turn the agent is still running.
    send_rc=0
    cig_safe_send "$WS_REF" "$msg" 8 3 || send_rc=$?
    case "$send_rc" in
        0) ;;
        3) echo "  Delivery outcome unknown. Verify the target thread before sending again." >&2; exit 3 ;;
        *) echo "  Not sent: $WS_NAME is busy, has a draft, or cannot be read. Send again when idle." >&2; continue ;;
    esac

    # Wait for agent to finish
    printf "  waiting" >&2
    wait_for_response
    echo "" >&2

    # Read new screen and diff against last snapshot
    NEW_SCREEN=$(read_screen 200)

    # Show only the lines that are new
    diff_output=$(comm -13 <(echo "$LAST_SCREEN" | sort) <(echo "$NEW_SCREEN" | sort) 2>/dev/null)
    if [[ -n "$diff_output" ]]; then
        # Better approach: find where old screen ends in new screen
        # Just show the full new screen minus the old line count
        old_lines=$(echo "$LAST_SCREEN" | wc -l | tr -d ' ')
        new_lines=$(echo "$NEW_SCREEN" | wc -l | tr -d ' ')
        if (( new_lines > old_lines )); then
            skip=$((new_lines - (new_lines - old_lines)))
            echo "$NEW_SCREEN" | tail -n +$((old_lines + 1))
        else
            # Screen wrapped, just show last 40 lines
            echo "$NEW_SCREEN" | tail -30
        fi
    fi
    echo ""

    LAST_SCREEN="$NEW_SCREEN"
done
