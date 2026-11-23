#!/usr/bin/env bash
# Install switchboard-owned Amp plugins from tracked sources in system/amp-plugins/.
#
# - Installs one host for the modules in switchboard-bundle.json. Their original
#   source files remain in place for imports, tests, and independent edits.
# - --unbundle restores separate hosts for rollback.
# - Ensures ~/.config/amp/plugins/<name> is a symlink to the tracked copy.
# - Never touches cmux-session.ts (owned by `cmux hooks amp install`).
# - If a plain runtime file differs from the tracked copy, refuses and warns
#   unless --force, which backs the runtime copy up first.
# - Idempotent: converged entries produce no output.
# - Where symlinks are unavailable (Windows Git Bash, where `ln -s` makes a
#   plain copy that goes stale), each runtime .ts entry is a two-line shim
#   that re-exports the tracked source by absolute path, so Amp always loads
#   the current file and relative imports (switchboard/, switchboard-bundle.json) resolve
#   next to the source. A task worktree installs shims that point at the main
#   checkout, because the worktree is removed when the task lands.
#   SWITCHBOARD_PLUGIN_LINK_MODE=symlink|shim overrides the probe.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET_DIR="${SWITCHBOARD_PLUGIN_TARGET_DIR:-$HOME/.config/amp/plugins}"
BACKUP_DIR="$TARGET_DIR/_pre-symlink-backup"
FORCE=0
UNBUNDLE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --unbundle) UNBUNDLE=1 ;;
        *) echo "usage: install-amp-plugins.sh [--force] [--unbundle]" >&2; exit 2 ;;
    esac
done
mkdir -p "$TARGET_DIR"

# Link mode: a real symlink where the filesystem allows one, else a shim.
LINK_MODE="${SWITCHBOARD_PLUGIN_LINK_MODE:-}"
if [[ -z "$LINK_MODE" ]]; then
    probe_src="$TARGET_DIR/.switchboard-link-probe-src.$$"
    probe_dst="$TARGET_DIR/.switchboard-link-probe.$$"
    : > "$probe_src"
    if ln -s "$probe_src" "$probe_dst" 2>/dev/null && [[ -L "$probe_dst" ]]; then
        LINK_MODE=symlink
    else
        LINK_MODE=shim
    fi
    rm -f "$probe_src" "$probe_dst"
fi
case "$LINK_MODE" in symlink|shim) ;; *) echo "SWITCHBOARD_PLUGIN_LINK_MODE must be symlink or shim" >&2; exit 2 ;; esac

SOURCE_ROOT="$REPO_ROOT"
if [[ "$LINK_MODE" == "shim" && -z "${SWITCHBOARD_PLUGIN_SOURCE_DIR:-}" ]]; then
    # Shims are absolute paths, so never aim them at a task worktree.
    main_root="$(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"
    if [[ -n "$main_root" && -d "$main_root/system/amp-plugins" ]]; then
        SOURCE_ROOT="$(cd "$main_root" && pwd)"
    fi
fi
SOURCE_DIR="${SWITCHBOARD_PLUGIN_SOURCE_DIR:-$SOURCE_ROOT/system/amp-plugins}"

# Path form Bun accepts in an import specifier (C:/... on Windows).
if command -v cygpath >/dev/null 2>&1; then SOURCE_SPEC="$(cygpath -m "$SOURCE_DIR")"; else SOURCE_SPEC="$SOURCE_DIR"; fi

shim_text() {
    local spec="$SOURCE_SPEC/$1"
    printf '%s\n' \
        "// switchboard plugin shim written by system/scripts/install-amp-plugins.sh: loads the tracked source." \
        "export * from '$spec'" \
        "export { default } from '$spec'"
}

# True when the runtime entry already points at the tracked source.
is_canonical() {
    local name="$1" target="$TARGET_DIR/$1"
    if [[ -L "$target" && "$(readlink "$target")" == "$SOURCE_DIR/$name" ]]; then
        return 0
    fi
    [[ "$LINK_MODE" == "shim" && -f "$target" && ! -L "$target" ]] || return 1
    [[ "$(<"$target")" == "$(shim_text "$name")" ]]
}

# Only .ts entries get a shim; directories and data files are imported
# relative to the tracked source and need no runtime copy.
shim_skips() {
    [[ "$LINK_MODE" == "shim" && "$1" != *.ts ]]
}

BUNDLED=$(python3 -c 'import json,sys; print("\n".join(n+".ts" for n in json.load(open(sys.argv[1]))))' "$SOURCE_DIR/switchboard-bundle.json" | tr -d '\r')

changed=0
failed=0

install_entry() {
    local name="$1"
    local source="$SOURCE_DIR/$name"
    local target="$TARGET_DIR/$name"

    # Already the correct symlink or shim → converged, stay quiet.
    if is_canonical "$name"; then
        return 0
    fi

    if [[ -L "$target" ]]; then
        if [[ "$LINK_MODE" == "shim" ]]; then
            rm "$target"
            shim_text "$name" > "$target"
            echo "Replaced link $name with a shim -> $source"
        else
            ln -sfn "$source" "$target"
            echo "Relinked $name -> $source"
        fi
        changed=$((changed + 1))
        return 0
    fi

    if [[ -e "$target" ]]; then
        if [[ -f "$source" && -f "$target" ]] && cmp -s "$source" "$target"; then
            rm "$target"
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

    if [[ "$LINK_MODE" == "shim" ]]; then
        shim_text "$name" > "$target"
        echo "Installed shim $name -> $source"
    else
        ln -s "$source" "$target"
        echo "Installed $name -> $source"
    fi
    changed=$((changed + 1))
}

# Preflight before any change. Never remove someone else's runtime override,
# or load both the old modules and bundle (which would duplicate side effects).
if [[ "$UNBUNDLE" -eq 0 ]]; then
    retiring="$BUNDLED"
else
    retiring="switchboard-runtime.ts"
fi
while IFS= read -r name; do
    target="$TARGET_DIR/$name"
    if [[ -e "$target" || -L "$target" ]]; then
        if ! is_canonical "$name"; then
            # --force backs up an old plain copy (what Windows Git Bash leaves);
            # a foreign symlink still blocks.
            if [[ "$FORCE" -eq 1 && -f "$target" && ! -L "$target" ]]; then
                mkdir -p "$BACKUP_DIR"
                mv "$target" "$BACKUP_DIR/$name.$(date +%Y%m%d-%H%M%S)"
                echo "Backed up plain copy of retired module $name to $BACKUP_DIR"
                continue
            fi
            echo "Refusing to retire noncanonical plugin $target; preserve and reconcile it first" >&2
            exit 1
        fi
    fi
done <<< "$retiring"

for source in "$SOURCE_DIR"/*; do
    name="$(basename "$source")"
    case "$name" in _retired|cmux-session.ts|switchboard-bundle.json) continue ;; esac
    target="$TARGET_DIR/$name"
    shim_skips "$name" && continue
    if [[ -e "$target" && ! -L "$target" && "$FORCE" -eq 0 ]] && ! is_canonical "$name"; then
        if ! { [[ -f "$source" && -f "$target" ]] && cmp -s "$source" "$target"; }; then
            echo "Refusing partial migration: runtime $target differs (use --force to back up)" >&2
            exit 1
        fi
    fi
done

for source in "$SOURCE_DIR"/*; do
    name="$(basename "$source")"
    case "$name" in
        _retired|cmux-session.ts|switchboard-bundle.json) continue ;;
    esac
    if [[ "$UNBUNDLE" -eq 0 ]] && grep -qxF "$name" <<< "$BUNDLED"; then continue; fi
    if [[ "$UNBUNDLE" -eq 1 && "$name" == "switchboard-runtime.ts" ]]; then continue; fi
    shim_skips "$name" && continue
    install_entry "$name"
done

if [[ "$failed" -gt 0 ]]; then
    echo "install-amp-plugins: $failed entr(y/ies) skipped due to differing runtime copies" >&2
    exit 1
fi

while IFS= read -r name; do
    target="$TARGET_DIR/$name"
    if is_canonical "$name"; then
        rm "$target"
        echo "Retired runtime link $name (source preserved)"
        changed=$((changed + 1))
    fi
done <<< "$retiring"

if [[ "$changed" -eq 0 ]]; then
    exit 0
fi

echo "install-amp-plugins: $changed change(s) applied"
