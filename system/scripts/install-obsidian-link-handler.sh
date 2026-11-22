#!/usr/bin/env bash
# Build and register "PM OS Obsidian Link.app", the pmos-obsidian:// handler.
#
# Why: in Claude Code
# inside cmux, report links to vault notes are pmos-obsidian://open?vault=…&file=…;
# this background-only app turns them into `open -g obsidian://open?...` so the
# note loads in Obsidian behind the app the owner is using. Source:
# system/native/obsidian-link/. Idempotent: rebuilds only when the source or
# Info.plist changed, then re-registers with LaunchServices.
#
# Usage: install-obsidian-link-handler.sh [--force] [--check]
#   --check  exit 0 when the app is installed and owns pmos-obsidian://, else 1
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
src="$repo/system/native/obsidian-link"
app="${PM_OS_OBSIDIAN_LINK_APP:-$HOME/Applications/PM OS Obsidian Link.app}"
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
bundle_id=com.pm-os.obsidian-link

[[ "$(uname -s)" == "Darwin" ]] || { echo "install-obsidian-link-handler: macOS only" >&2; exit 0; }

handler_owner() {
    swift -e 'import AppKit; print(NSWorkspace.shared.urlForApplication(toOpen: URL(string: "pmos-obsidian://open")!)?.path ?? "")' 2>/dev/null || true
}

if [[ "${1:-}" == "--check" ]]; then
    owner="$(handler_owner)"
    if [[ -x "$app/Contents/MacOS/pm-os-obsidian-link" && "$owner" == "$app" ]]; then
        echo "ok: $app owns pmos-obsidian://"
        exit 0
    fi
    echo "missing: pmos-obsidian:// handler is '${owner:-none}', expected $app" >&2
    exit 1
fi

stamp="$(cat "$src/main.swift" "$src/Info.plist" | shasum | cut -c1-12)"
current="$(cat "$app/Contents/Resources/source-hash" 2>/dev/null || true)"
if [[ "${1:-}" != "--force" && "$stamp" == "$current" && -x "$app/Contents/MacOS/pm-os-obsidian-link" ]]; then
    echo "up to date: $app ($stamp)"
else
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/app/Contents/MacOS" "$tmp/app/Contents/Resources"
    cp "$src/Info.plist" "$tmp/app/Contents/Info.plist"
    swiftc -O -o "$tmp/app/Contents/MacOS/pm-os-obsidian-link" "$src/main.swift"
    echo "$stamp" > "$tmp/app/Contents/Resources/source-hash"
    codesign --force --sign - "$tmp/app" >/dev/null 2>&1 || true
    mkdir -p "$(dirname "$app")"
    rm -rf "$app"
    mv "$tmp/app" "$app"
    echo "built: $app ($stamp)"
fi

"$lsregister" -f "$app"
owner="$(handler_owner)"
if [[ "$owner" != "$app" ]]; then
    echo "warning: LaunchServices maps pmos-obsidian:// to '${owner:-none}', not $app" >&2
    exit 1
fi
echo "registered: pmos-obsidian:// -> $app ($bundle_id)"
