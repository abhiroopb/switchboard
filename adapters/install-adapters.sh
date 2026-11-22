#!/usr/bin/env bash
# Wire the PM OS policy and skills into Claude Code, Codex CLI and Cursor.
#
# The repo already carries the project-level adapters (CLAUDE.md, .claude/,
# .codex/, .cursor/). This script adds the *home-level* pieces so the policy
# also loads when you open another project, and so skills resolve:
#
#   ~/.claude/CLAUDE.md        managed block importing <root>/AGENTS.md
#   ~/.claude/skills/<name>    copies of the manifest skills
#                              (system/config/claude-skills-manifest.yaml, via
#                              system/scripts/install-claude-skills.py; copied,
#                              never symlinked, so Claude Code sees a plain dir)
#   ~/.codex/AGENTS.md         managed block pointing at <root>/AGENTS.md
#   ~/.codex/config.toml       project_doc_max_bytes = 65536 (Codex truncates
#                              AGENTS.md at 32 KiB otherwise)
#
# Claude Code memory (claude-mem), plugins, and MCP servers are a separate,
# one-command step: bash system/scripts/install-claude-code.sh
#
# Cursor reads <root>/AGENTS.md and .cursor/rules/pm-os.mdc from the repo; there
# is nothing to install at home. ChatGPT and other chat tools: paste
# adapters/SYSTEM.md (printed at the end).
#
# Managed blocks are delimited by <!-- pm-os-kit:start --> / <!-- pm-os-kit:end -->
# and are replaced in place on re-run; text outside them is never touched.
#
# Usage:
#   bash adapters/install-adapters.sh            # dry run: print what would change
#   bash adapters/install-adapters.sh --apply    # write
#   bash adapters/install-adapters.sh --apply --home /tmp/fake-home   # for tests
#   bash adapters/install-adapters.sh --apply --no-skills             # adapters only, no skill copies
#
# Owner context: when <root>/local/agents-local.md exists, the home adapters load it too.
# Employer and product details live there because AGENTS.md ships in the kit. local/ is
# gitignored and left out of the kit, so no kit, commit, or push can carry it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HOME}"
APPLY=0
SKILLS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --no-skills) SKILLS=0 ;;
    --home) HOME_DIR="$2"; shift ;;
    --root) ROOT="$(cd "$2" && pwd)"; shift ;;
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f "$ROOT/AGENTS.md" ] || { echo "no AGENTS.md under $ROOT; pass --root" >&2; exit 1; }
PY="${PM_OS_PYTHON:-python3}"
if ! $PY --version >/dev/null 2>&1; then
  if py -3 --version >/dev/null 2>&1; then PY="py -3"; else echo "python3 is required (set PM_OS_PYTHON)" >&2; exit 1; fi
fi

say() { printf '%s\n' "$*"; }
plan() { if [ "$APPLY" = 1 ]; then say "write  $*"; else say "would  $*"; fi; }

# upsert_block FILE BLOCK_TEXT  -- replace or append the managed block.
upsert_block() {
  local file="$1" block="$2"
  if [ "$APPLY" = 0 ]; then
    if [ -f "$file" ] && grep -q '<!-- pm-os-kit:start -->' "$file"; then plan "$file (replace managed block)"; else plan "$file (append managed block)"; fi
    return
  fi
  mkdir -p "$(dirname "$file")"
  $PY - "$file" "$block" <<'PY'
import sys, pathlib
path, block = pathlib.Path(sys.argv[1]), sys.argv[2].rstrip("\n") + "\n"
start, end = "<!-- pm-os-kit:start -->", "<!-- pm-os-kit:end -->"
text = path.read_text() if path.exists() else ""
if start in text and end in text:
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    text = head + block.rstrip("\n") + tail
else:
    text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
path.write_text(text)
print(f"write  {path}")
PY
}

LOCAL_POLICY="$ROOT/local/agents-local.md"
LOCAL_IMPORT=""
LOCAL_POINTER=""
LOCAL_READ=""
if [ -f "$LOCAL_POLICY" ]; then
  LOCAL_IMPORT=$'\n'"@$LOCAL_POLICY"
  LOCAL_POINTER=$'\n'"- Owner context: read $LOCAL_POLICY."
  LOCAL_READ=" Then read $LOCAL_POLICY for the owner context."
fi

say "PM OS root: $ROOT"
say "home:       $HOME_DIR  ($([ "$APPLY" = 1 ] && echo apply || echo 'dry run; pass --apply to write'))"
say ""

say "## Claude Code"
upsert_block "$HOME_DIR/.claude/CLAUDE.md" "<!-- pm-os-kit:start -->
# PM OS (managed by $ROOT/adapters/install-adapters.sh)
@$ROOT/AGENTS.md
@$ROOT/system/behavior-map.md$LOCAL_IMPORT

## Runtime Adapter
This file is the Claude Code home adapter. It only imports the repo policy above; shared PM OS behavior lives in the repo AGENTS.md, so change it there.
<!-- pm-os-kit:end -->"
if [ "$SKILLS" = 0 ]; then
  say "       skills: skipped (--no-skills)"
elif [ -f "$ROOT/system/scripts/install-claude-skills.py" ]; then
  skill_args=(--skip-missing --home "$HOME_DIR")
  [ "$APPLY" = 0 ] && skill_args+=(--dry-run)
  $PY "$ROOT/system/scripts/install-claude-skills.py" "${skill_args[@]}" | sed 's/^/       /'
else
  say "       skills: $ROOT/system/scripts/install-claude-skills.py missing; skipped"
fi
say "       project hook: $ROOT/.claude/settings.json -> .claude/hooks/pm-os-guardrails.py (already in the repo)"
say "       memory, plugins, MCP servers: bash $ROOT/system/scripts/install-claude-code.sh"
say ""

say "## Home AGENTS.md (Amp and other harnesses that read ~/AGENTS.md)"
# check-instruction-drift.py expects one pointer line per repo-owned section here,
# never a copy of the section.
upsert_block "$HOME_DIR/AGENTS.md" "<!-- pm-os-kit:start -->
# PM OS home pointers (managed by $ROOT/adapters/install-adapters.sh)
These sections live only in $ROOT/AGENTS.md; follow them from there.
- Control-Plane Ownership: see the repo AGENTS.md.
- Canonical Calendar Rule: see the repo AGENTS.md.
- Perspective Check: see the repo AGENTS.md.
- Already-Answered Pre-Check: see the repo AGENTS.md.$LOCAL_POINTER
<!-- pm-os-kit:end -->"
say ""

say "## Codex CLI"
upsert_block "$HOME_DIR/.codex/AGENTS.md" "<!-- pm-os-kit:start -->
# PM OS (managed by $ROOT/adapters/install-adapters.sh)
Before anything else, read $ROOT/AGENTS.md and follow it as the operating contract; $ROOT/system/behavior-map.md says which surface owns which behavior. Codex loads the project AGENTS.md automatically when the working directory is inside that repo; this block covers every other directory.$LOCAL_READ
<!-- pm-os-kit:end -->"
cfg="$HOME_DIR/.codex/config.toml"
if [ -f "$cfg" ] && grep -Eq '^[[:space:]]*project_doc_max_bytes[[:space:]]*=' "$cfg"; then
  say "keep   $cfg already sets project_doc_max_bytes"
else
  plan "$cfg (append project_doc_max_bytes = 65536)"
  if [ "$APPLY" = 1 ]; then
    mkdir -p "$(dirname "$cfg")"
    { [ -f "$cfg" ] && [ -s "$cfg" ] && printf '\n'; printf '# PM OS: AGENTS.md is larger than the 32 KiB default cap\nproject_doc_max_bytes = 65536\n'; } >> "$cfg"
  fi
fi
say "       skills: Codex reads $ROOT/.agents/skills/ in-repo and ~/.agents/skills/ (bash system/bin/install-amp-skills.sh)"
say ""

say "## Cursor"
say "       reads $ROOT/AGENTS.md and $ROOT/.cursor/rules/pm-os.mdc from the repo; nothing to install"
say ""

say "## ChatGPT and other chat tools"
say "       paste $ROOT/adapters/SYSTEM.md as the system prompt, then skills from $ROOT/adapters/SKILLS.md"
if [ "$APPLY" = 0 ]; then say ""; say "Dry run only. Re-run with --apply to write."; fi
