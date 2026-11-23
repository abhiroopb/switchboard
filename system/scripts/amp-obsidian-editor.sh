#!/usr/bin/env bash
# AMP_EDITOR shim: a clicked file:// link to a switchboard vault note opens in Obsidian.
#
# Why: some Amp CLI versions open only http(s)
# links on click ("Only http and https links can be opened."), so the
# obsidian:// review pointers CoS prints stopped opening. A clicked file:// link
# is handed to $AMP_EDITOR (then $VISUAL, $EDITOR) with the path as its only
# argument. This shim turns a vault note path into `open -g obsidian://open?...`
# and hands anything else (prompt editing, `amp settings edit`, a non-vault
# file) to the real editor unchanged.
#
# Install: `export AMP_EDITOR={{SWITCHBOARD_ROOT}}/system/scripts/amp-obsidian-editor.sh`
# in ~/.zshrc (every Amp launch path inherits the shell env). The real editor
# is AMP_REAL_EDITOR, else VISUAL, else EDITOR, else vim.
#
# Vault: $SWITCHBOARD_VAULT_ROOT / $SWITCHBOARD_VAULT_NAME when set, else the switchboard vault
# registered in Obsidian's obsidian.json, else {{SWITCHBOARD_ROOT}}.
# A note inside a task worktree (<vault>/.amp/worktrees/<id>/<rel>) maps to
# <rel> on main when main holds that file (the branch has landed), else it
# falls through to the editor.
set -u

real_editor() {
    exec "${AMP_REAL_EDITOR:-${VISUAL:-${EDITOR:-vim}}}" "$@"
}

vault_root="${SWITCHBOARD_VAULT_ROOT:-}"
vault_name="${SWITCHBOARD_VAULT_NAME:-switchboard}"
if [[ -z "$vault_root" ]]; then
    vault_root="$(python3 - "$vault_name" <<'PY' 2>/dev/null
import json, os, sys
name = sys.argv[1]
cfg = os.path.expanduser("~/Library/Application Support/obsidian/obsidian.json")
try:
    vaults = json.load(open(cfg)).get("vaults", {})
except Exception:
    vaults = {}
for v in vaults.values():
    path = v.get("path") or ""
    if os.path.basename(path.rstrip("/")) == name:
        print(path)
        break
PY
)"
fi
vault_root="${vault_root:-{{SWITCHBOARD_ROOT}}}"
vault_root="${vault_root%/}"

[[ $# -eq 1 ]] || real_editor "$@"
target="$1"

# Images are links the owner views themselves (external-replies
# Rule 14): a clicked file:// link to an image opens in the system viewer, and
# a link to an HTML page (the image_compare.py comparison page, Rule 14 item 2)
# opens in the default browser, never the editor. This path runs only when the owner
# clicks a link, so it opens in the foreground; automated opens use `open -g`
#. A task-worktree path maps to
# main once the branch has landed; a main path that has not landed yet opens the
# live worktree copy, newest first.
open_image() {
    local img="$1"
    case "$img" in
        "$vault_root"/.amp/worktrees/*/*)
            local rel="${img#"$vault_root"/.amp/worktrees/}"
            rel="${rel#*/}"
            [[ -f "$vault_root/$rel" ]] && img="$vault_root/$rel"
            ;;
        "$vault_root"/*)
            if [[ ! -f "$img" ]]; then
                local rel="${img#"$vault_root"/}" tree
                while IFS= read -r tree; do
                    [[ -n "$tree" && -f "$tree/$rel" ]] && { img="$tree/$rel"; break; }
                done < <(ls -1dt "$vault_root"/.amp/worktrees/*/ 2>/dev/null | sed 's:/$::')
            fi
            ;;
    esac
    [[ -f "$img" ]] || real_editor "$1"
    if [[ -x /usr/bin/open && "$(uname -s)" == "Darwin" ]]; then
        exec /usr/bin/open "$img"  # focus-ok: the owner clicked this link, so the viewer comes to the front
    fi
    if command -v wslview >/dev/null 2>&1; then
        exec wslview "$img"
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        exec xdg-open "$img"
    fi
    real_editor "$1"
}
shopt -s nocasematch
case "$target" in
    *.png|*.jpg|*.jpeg|*.gif|*.webp|*.svg|*.heic|*.tif|*.tiff|*.bmp|*.avif|*.html|*.htm)
        case "$target" in /*) open_image "$target" ;; *) open_image "$PWD/$target" ;; esac ;;
esac
shopt -u nocasematch

case "$target" in
    *.md|*.markdown) ;;
    *) real_editor "$@" ;;
esac

# Resolve the target against the vault without following the vault's own
# symlinks (people/ in a worktree is a link to main's directory).
case "$target" in
    /*) abs="$target" ;;
    *) abs="$PWD/$target" ;;
esac
case "$abs" in
    "$vault_root"/.amp/worktrees/*)
        rel="${abs#"$vault_root"/.amp/worktrees/}"
        rel="${rel#*/}"
        [[ -f "$vault_root/$rel" ]] || real_editor "$@"
        ;;
    "$vault_root"/*)
        rel="${abs#"$vault_root"/}"
        [[ -f "$abs" ]] || real_editor "$@"
        ;;
    *) real_editor "$@" ;;
esac

uri="$(python3 - "$vault_name" "$rel" <<'PY'
import sys
from urllib.parse import quote
name, rel = sys.argv[1], sys.argv[2]
if rel.endswith(".md"):
    rel = rel[:-3]
print(f"obsidian://open?vault={quote(name, safe='')}&file={quote(rel, safe='/')}")
PY
)"
# macOS: `open -g`, so the note loads in Obsidian behind the app the owner is using
#. Claude Code in cmux
# gets the same result through the switchboard-obsidian:// handler app.
# Elsewhere (WSL2 under wmux, Linux): wslview hands the URI to Windows, then
# PowerShell, then xdg-open.
if [[ -x /usr/bin/open && "$(uname -s)" == "Darwin" ]]; then
    exec /usr/bin/open -g "$uri"
fi
if command -v wslview >/dev/null 2>&1; then
    exec wslview "$uri"
fi
if command -v powershell.exe >/dev/null 2>&1; then
    exec powershell.exe -NoProfile -Command "Start-Process '$uri'"
fi
if command -v xdg-open >/dev/null 2>&1; then
    exec xdg-open "$uri"
fi
echo "amp-obsidian-editor: no opener for $uri" >&2
exit 1
