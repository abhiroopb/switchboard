#!/usr/bin/env bash
# read-all-workspaces.sh — Read the last N lines from all cmux workspaces
# Usage: bash read-all-workspaces.sh [lines=50]

CMUX="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"
LINES="${1:-50}"

WORKSPACE_LISTING=$("$CMUX" list-workspaces 2>/dev/null)

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    WS=$(printf '%s\n' "$line" | grep -oE 'workspace:[0-9]+' || true)
    [ -z "$WS" ] && continue

    WS_NAME=$(printf '%s\n' "$line" | sed -E 's/^[* ]*workspace:[0-9]+  //' | sed 's/ *\[selected\]$//')
    TREE_OUTPUT=$("$CMUX" tree --workspace "$WS" 2>/dev/null)
    SURFACE=$(printf '%s\n' "$TREE_OUTPUT" | grep -oE 'surface:[0-9]+' | head -1)

    [ -z "$SURFACE" ] && continue

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📋 $WS_NAME ($WS)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    "$CMUX" read-screen --workspace "$WS" --surface "$SURFACE" --lines "$LINES" 2>/dev/null
    echo ""
done <<< "$WORKSPACE_LISTING"
