#!/usr/bin/env bash
# Install switchboard-owned CLI scripts and support files from tracked sources.
#
# - system/bin/*            -> ~/bin/<name>
# - system/amp-mem/schema.sql -> ~/.amp/memory/schema.sql (amp-mem init reads it)
#
# Mirrors install-amp-plugins.sh:
# - Ensures each target is a symlink to the tracked copy.
# - If a plain runtime file is identical to the tracked copy, replaces it with
#   a symlink. If it differs, refuses and warns unless --force (backs up first).
# - Idempotent: converged entries produce no output.
# - Windows Git Bash without real symlinks: `ln -s` makes a copy. Each copy's
#   hash is recorded in $TARGET_DIR/.switchboard-installed, so a later run can tell a
#   stale copy it installed (refreshed) from a runtime file it never wrote
#   (skipped with a warning, as on macOS).
# - launchd plists are installed on macOS only.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_DIR="$REPO_ROOT/system/bin"
TARGET_DIR="$HOME/bin"
BACKUP_DIR="$TARGET_DIR/_pre-symlink-backup"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

MANIFEST="$TARGET_DIR/.switchboard-installed"
changed=0
failed=0

file_hash() { cksum < "$1" | awk '{print $1 "-" $2}'; }

# True when $target is a plain copy this script wrote earlier (manifest hash match).
installed_copy() {
    local target="$1"
    [[ -f "$MANIFEST" && -f "$target" ]] || return 1
    grep -qxF "$target $(file_hash "$target")" "$MANIFEST"
}

record_copy() {
    local target="$1"
    local tmp="$MANIFEST.tmp.$$"
    { [[ -f "$MANIFEST" ]] && grep -vF "$target " "$MANIFEST" || true; } > "$tmp"
    echo "$target $(file_hash "$target")" >> "$tmp"
    mv "$tmp" "$MANIFEST"
}

install_entry() {
    local source="$1"
    local target="$2"
    local name; name="$(basename "$target")"

    if [[ -L "$target" && "$(readlink "$target")" == "$source" ]]; then
        return 0
    fi

    if [[ -L "$target" ]]; then
        ln -sfn "$source" "$target"
        echo "Relinked $name -> $source"
        changed=$((changed + 1))
        return 0
    fi

    if [[ -e "$target" ]]; then
        if [[ -f "$source" && -f "$target" ]] && cmp -s "$source" "$target"; then
            # Identical plain copy: converged where symlinks are copies.
            if [[ ! -L "$target" ]] && installed_copy "$target"; then
                return 0
            fi
            rm "$target"
        elif installed_copy "$target"; then
            rm "$target"
            echo "Refreshing stale installed copy of $name"
        elif [[ "$FORCE" -eq 1 ]]; then
            mkdir -p "$BACKUP_DIR"
            mv "$target" "$BACKUP_DIR/$name.$(date +%Y%m%d-%H%M%S)"
            echo "Backed up differing runtime copy of $name to $BACKUP_DIR"
        else
            echo "WARNING: runtime $name differs from tracked copy; skipping (use --force to back up and replace)" >&2
            failed=$((failed + 1))
            return 0
        fi
    fi

    ln -s "$source" "$target"
    [[ -L "$target" ]] || record_copy "$target"
    echo "Installed $name -> $source"
    changed=$((changed + 1))
}

mkdir -p "$TARGET_DIR"

if [[ -d "$SOURCE_DIR" ]]; then
    for source in "$SOURCE_DIR"/*; do
        [[ -e "$source" ]] || continue
        install_entry "$source" "$TARGET_DIR/$(basename "$source")"
    done
fi

# amp-mem schema: the CLI's init reads this path directly.
if [[ -f "$REPO_ROOT/system/amp-mem/schema.sql" ]]; then
    mkdir -p "$HOME/.amp/memory"
    install_entry "$REPO_ROOT/system/amp-mem/schema.sql" "$HOME/.amp/memory/schema.sql"
fi

# Canonical todo state: the to-do list lives at system/state/todo.json,
# gitignored local state. The old runtime path ~/.config/amp/todo.json is retired.
# Clean up leftovers: drop a legacy symlink, and if a plain file reappears
# there (stale code still writing the old path), recover it newest-wins
# with a backup so no data is silently lost.
TODO_STATE="$REPO_ROOT/system/state/todo.json"
TODO_LEGACY="$HOME/.config/amp/todo.json"
if [[ -L "$TODO_LEGACY" ]]; then
    rm "$TODO_LEGACY"
    echo "Removed legacy todo.json symlink at $TODO_LEGACY"
    changed=$((changed + 1))
elif [[ -e "$TODO_LEGACY" ]]; then
    echo "WARNING: stray todo.json found at $TODO_LEGACY — some code still writes the retired path" >&2
    if [[ ! -e "$TODO_STATE" ]]; then
        mv "$TODO_LEGACY" "$TODO_STATE"
        echo "Migrated stray $TODO_LEGACY -> $TODO_STATE"
    elif cmp -s "$TODO_LEGACY" "$TODO_STATE"; then
        rm "$TODO_LEGACY"
        echo "Removed duplicate stray $TODO_LEGACY"
    elif [[ "$TODO_LEGACY" -nt "$TODO_STATE" ]]; then
        cp -p "$TODO_STATE" "$TODO_STATE.bak-$(date +%Y%m%dT%H%M%S)"
        mv "$TODO_LEGACY" "$TODO_STATE"
        echo "Recovered newer stray todo.json into $TODO_STATE (previous state copy backed up alongside)"
    else
        cp -p "$TODO_LEGACY" "$TODO_STATE.legacy-bak-$(date +%Y%m%dT%H%M%S)"
        rm "$TODO_LEGACY"
        echo "Removed older stray $TODO_LEGACY (backed up alongside $TODO_STATE)"
    fi
    changed=$((changed + 1))
fi

# switchboard-launcher: build if missing (never rebuild here — a rebuild changes
# the code hash and silently revokes the Full Disk Access grant; use
# build-switchboard-launcher.sh --force deliberately for that).
if [[ ! -x "$REPO_ROOT/system/bin/switchboard-launcher" && -f "$REPO_ROOT/system/native/switchboard-launcher.c" ]]; then
    bash "$REPO_ROOT/system/scripts/build-switchboard-launcher.sh"
    changed=$((changed + 1))
fi

# launchd plists: copied (not symlinked — launchd is unreliable with symlinked
# plists). If the runtime copy differs, refresh it and remind about reload.
if [[ "$(uname -s)" == Darwin && -d "$REPO_ROOT/system/launchd" ]]; then
    for source in "$REPO_ROOT/system/launchd"/*.plist; do
        [[ -e "$source" ]] || continue
        name="$(basename "$source")"
        target="$HOME/Library/LaunchAgents/$name"
        if [[ ! -e "$target" ]] || ! cmp -s "$source" "$target"; then
            cp -p "$source" "$target"
            echo "Refreshed launchd plist $name (reload: launchctl bootout gui/\$(id -u)/${name%.plist} 2>/dev/null; launchctl bootstrap gui/\$(id -u) $target)"
            changed=$((changed + 1))
        fi
    done
fi

if [[ "$failed" -gt 0 ]]; then
    echo "install-switchboard-bin: $failed entr(y/ies) skipped due to differing runtime copies" >&2
    exit 1
fi

if [[ "$changed" -eq 0 ]]; then
    exit 0
fi

echo "install-switchboard-bin: $changed change(s) applied"
