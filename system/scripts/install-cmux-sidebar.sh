#!/usr/bin/env bash
# Install the switchboard cmux sidebar from its tracked source.
#
# The switchboard sidebar (system/cmux-sidebars/switchboard.swift) renders the cmux
# workspace board: Pinned / Needs Attention / To Review / Active / P0 To-dos /
# Waiting / Paused / Idle / Background. cmux loads sidebars from
# ~/.config/cmux/sidebars/, so this script symlinks the tracked source there —
# same durable install pattern as install-amp-plugins.sh. Idempotent; called
# by start-day.sh Phase 1 and safe to run manually any time.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_DIR="$REPO_ROOT/system/cmux-sidebars"
TARGET_DIR="$HOME/.config/cmux/sidebars"
HELPER_SOURCE="$REPO_ROOT/system/scripts/cmux_new_contextual_amp_tab.py"
HELPER_TARGET="$HOME/.config/cmux-sidebar/scripts/cmux_new_contextual_amp_tab.py"
CMUX="${CMUX_BIN:-${CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}}"

# workspaces.swift is also installed by the standalone cmux-sidebar installer.
# Keep its helper entrypoints compatible with the switchboard recorder. Reapply on
# every switchboard install/start-day, including after a standalone sidebar reinstall.
# Use wrappers, not symlinks: the standalone install uses cp, which would follow
# symlinks and overwrite our tracked scripts.
install_action_helpers() {
    local name target
    mkdir -p "$(dirname "$HELPER_TARGET")"
    for name in cmux-reopen-last-closed.sh cmux-relaunch-workspace.sh cmux-rename-workspace.sh cmux-close-workspace.sh; do
        target="$(dirname "$HELPER_TARGET")/$name"
        rm -f "$target"
        printf '#!/bin/bash\nexec /bin/bash %q "$@"\n' "$REPO_ROOT/system/scripts/$name" > "$target"
        chmod +x "$target"
    done
}

# Narrow repair path: no sidebar selection, keybinding, or terminal reload.
if [[ "${1:-}" == "--actions-only" ]]; then
    install_action_helpers
    exit 0
fi

install_sidebar() {
    local name="$1"
    local source="$SOURCE_DIR/$name"
    local target="$TARGET_DIR/$name"

    if [[ ! -f "$source" ]]; then
        echo "Missing tracked sidebar source: $source" >&2
        return 1
    fi

    if [[ -L "$target" ]]; then
        ln -sfn "$source" "$target"
    elif [[ -e "$target" ]]; then
        if ! cmp -s "$source" "$target"; then
            echo "Refusing to replace modified sidebar: $target" >&2
            return 1
        fi
        rm "$target"
        ln -s "$source" "$target"
    else
        ln -s "$source" "$target"
    fi

    echo "Installed $name -> $source"
}

mkdir -p "$TARGET_DIR"
install_sidebar switchboard.swift
install_action_helpers

mkdir -p "$(dirname "$HELPER_TARGET")"
cp "$HELPER_SOURCE" "$HELPER_TARGET"
/usr/bin/python3 "$HELPER_TARGET" \
    --install-config \
    --helper-path "$HELPER_TARGET"
echo "Installed contextual Amp launcher -> $HELPER_TARGET"

if [[ -x "$CMUX" ]]; then
    "$CMUX" reload-config
fi
