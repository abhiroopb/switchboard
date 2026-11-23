#!/usr/bin/env bash
# One command to finish a switchboard install on any machine, with no questions:
# macOS + cmux, Windows + wmux (run from Git Bash), or Linux (policy and skills).
#
#   bash system/scripts/install-all.sh                 detect everything and install
#   bash system/scripts/install-all.sh --dry-run       print the plan only
#   bash system/scripts/install-all.sh --profile company|personal   force the organisation profile
#   bash system/scripts/install-all.sh --agent claude|amp         default agent for switchboard launches
#   bash system/scripts/install-all.sh --skip-services            no launchd / Task Scheduler
#
# Run it after install.py has written the checkout (README steps 1 to 4). It is
# idempotent: run it again after an update. Every step that needs a missing tool
# (amp, claude, cmux, wmux) is skipped with one line saying how to add it later.
#
# Steps, in order:
#   1. organisation profile   system/scripts/apply-profile.py (company when the real switchboard is on PATH)
#   2. skills                 system/bin/install-amp-skills.sh (~/.agents/skills for Amp and Codex,
#                             ~/.claude/skills for Claude Code)
#   3. adapters               adapters/install-adapters.sh --apply (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md)
#   4. Amp                    system/scripts/install-amp-plugins.sh, when `amp` is installed
#   5. Claude Code            system/scripts/install-claude-code.sh, when `claude` is installed
#   5b. reader and graph      Obsidian (winget / brew cask), the switchboard vault
#                             (register-obsidian-vault.py), on Windows the .md opener
#                             (system/windows/register-obsidian-notes.ps1), graphify
#                             (uv tool) and the first knowledge graph build
#   6. default agent          `agent:` in system/config/router.yaml (--agent; unchanged otherwise)
#   7. background services    macOS: install-launchd.sh + install-cmux-sidebar.sh;
#                             Windows: system/windows/install.ps1 -SkipSeed
#   8. checks                 verify-kit-install.sh --quick, check-profile-leaks.py, connector status
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1
DRY=0; PROFILE="auto"; AGENT=""; SERVICES=1
while (($#)); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --profile) PROFILE="${2:?--profile needs company or personal}"; shift ;;
    --agent) AGENT="${2:?--agent needs claude or amp}"; shift ;;
    --skip-services) SERVICES=0 ;;
    -h|--help) sed -n 2,29p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '[switchboard] %s\n' "$*"; }
run() {  # run a step; never stop the whole install on one failure
  local label="$1"; shift
  if ((DRY)); then say "would run ($label): $*"; return 0; fi
  say "==> $label"
  if "$@"; then say "    ok: $label"; else say "    FAILED: $label (exit $?); continuing"; failures+=("$label"); fi
}
failures=()

case "$(uname -s)" in
  Darwin) OS=macos ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;
  Linux) if grep -qi microsoft /proc/version 2>/dev/null; then OS=wsl; else OS=linux; fi ;;
  *) OS=unknown ;;
esac
if [[ "$OS" == windows && -f system/windows/switchboard-env.sh ]]; then
  # shellcheck source=../windows/switchboard-env.sh
  source system/windows/switchboard-env.sh >/dev/null 2>&1 || true
fi
PY="${SWITCHBOARD_PYTHON:-$(command -v python3 || command -v python)}"
MUX=none
if [[ "$OS" == macos ]] && { command -v cmux >/dev/null 2>&1 || [[ -x /Applications/cmux.app/Contents/Resources/bin/cmux ]]; }; then MUX=cmux; fi
if [[ "$OS" == windows ]] && { command -v wmux >/dev/null 2>&1 || [[ -f "${LOCALAPPDATA:-}/Programs/wmux/resources/cli/wmux.js" ]]; }; then MUX=wmux; fi
HAVE_AMP=0; command -v amp >/dev/null 2>&1 && HAVE_AMP=1
HAVE_CLAUDE=0; command -v claude >/dev/null 2>&1 && HAVE_CLAUDE=1
say "os=$OS mux=$MUX amp=$HAVE_AMP claude=$HAVE_CLAUDE python=$PY profile=$PROFILE dry_run=$DRY"

# 1. organisation profile
if ((DRY)); then run "profile" "$PY" system/scripts/apply-profile.py --profile "$PROFILE" --dry-run
else run "profile" "$PY" system/scripts/apply-profile.py --profile "$PROFILE"; fi
[[ -f "${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}/env" ]] && export "$(grep -m1 '^SWITCHBOARD_PROFILE=' "${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}/env" || echo SWITCHBOARD_PROFILE=personal)"

# 2-3. skills and adapters (both harnesses read these)
run "skills (Amp, Codex, Claude Code)" bash system/bin/install-amp-skills.sh
run "adapters (CLAUDE.md, Codex AGENTS.md)" bash adapters/install-adapters.sh --apply

# 4. Amp
if ((HAVE_AMP)); then run "Amp plugins" bash system/scripts/install-amp-plugins.sh
else say "    skip: Amp not installed (install it, then rerun this script)"; fi

# 5. Claude Code (inside a Claude Code session the plugin and MCP steps need --allow-nested)
if ((HAVE_CLAUDE)); then
  nested=(); [[ -n "${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}" ]] && nested=(--allow-nested)
  run "Claude Code (skills, plugins, MCP servers, bypass permissions)" bash system/scripts/install-claude-code.sh "${nested[@]}"
else say "    skip: Claude Code not installed (install it, then rerun this script)"; fi

# 5b. Obsidian (how the owner reads switchboard notes), and the knowledge graph.
# Obsidian is only the reader: review links are obsidian://open?vault=switchboard&file=…,
# so the checkout must be a registered vault named switchboard. graphify and memory are
# separate tools that never need Obsidian. Memory is chosen in step 5
# (install-claude-code.sh --memory builtin|claude-mem; amp-mem ships with Amp).
case "$OS" in
  windows)
    obsidian_exe=""
    for c in "${LOCALAPPDATA:-}/Programs/Obsidian/Obsidian.exe" "${PROGRAMFILES:-C:/Program Files}/Obsidian/Obsidian.exe"; do [[ -f "$c" ]] && { obsidian_exe="$c"; break; }; done
    if [[ -z "$obsidian_exe" ]]; then
      if command -v winget >/dev/null 2>&1; then
        run "Obsidian (winget)" winget install --id Obsidian.Obsidian -e --silent --accept-package-agreements --accept-source-agreements
      else say "    skip: winget not found; install Obsidian from https://obsidian.md, then rerun"; fi
    else say "    ok: Obsidian already installed ($obsidian_exe)"; fi ;;
  macos)
    if [[ -d /Applications/Obsidian.app ]]; then say "    ok: Obsidian already installed"
    elif command -v brew >/dev/null 2>&1; then run "Obsidian (Homebrew)" brew install --cask obsidian
    else say "    skip: Homebrew not found; install Obsidian from https://obsidian.md, then rerun"; fi ;;
  *) say "    skip: Obsidian install is manual on $OS (https://obsidian.md); notes stay plain Markdown" ;;
esac
if [[ "$OS" == windows || "$OS" == macos ]]; then
  # Closes and reopens Obsidian once when it is running (it rewrites its vault list while open).
  if ((DRY)); then run "Obsidian vault switchboard" "$PY" system/scripts/register-obsidian-vault.py --dry-run
  else run "Obsidian vault switchboard" "$PY" system/scripts/register-obsidian-vault.py; fi
fi
if [[ "$OS" == windows ]]; then
  # Obsidian.exe ignores a bare .md argument, so a clicked note link opened nothing.
  notes_ps1="$(cygpath -w "$ROOT/system/windows/register-obsidian-notes.ps1" 2>/dev/null || echo "$ROOT/system/windows/register-obsidian-notes.ps1")"
  dry_flag=(); ((DRY)) && dry_flag=(-DryRun)
  run "Markdown notes open in their Obsidian vault" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$notes_ps1" -SwitchboardRoot "$(cygpath -w "$ROOT" 2>/dev/null || echo "$ROOT")" "${dry_flag[@]}"
fi
GRAPHIFY_VERSION="$(sed -n 's/^TESTED_GRAPHIFY_VERSION = "\(.*\)"/\1/p' system/scripts/build-knowledge-graph.py)"
if command -v uv >/dev/null 2>&1; then
  if uv tool list 2>/dev/null | grep -q "^graphifyy v${GRAPHIFY_VERSION}\b"; then say "    ok: graphify $GRAPHIFY_VERSION already installed"
  else run "graphify $GRAPHIFY_VERSION (uv tool)" uv tool install --force "graphifyy==${GRAPHIFY_VERSION}" --python 3.12; fi
  run "knowledge graph (graphify-out/graph.json)" "$PY" system/scripts/build-knowledge-graph.py $( ((DRY)) && echo --dry-run ) --if-installed
else
  say "    skip: uv not found, so no knowledge graph (Windows: winget install astral-sh.uv; macOS: brew install uv), then rerun"
fi

# 5c. Recall inputs: the daily knowledge layer that
# query-knowledge.py reads and the recall index generate-recall-pack.py reads.
# Both rebuild on their own afterwards (background tasks build-daily-knowledge
# and import-memory-corpus).
run "knowledge layer (system/state/knowledge)" "$PY" system/scripts/build-daily-knowledge.py
run "recall index (system/recall)" "$PY" system/scripts/_archive/import-memory-corpus.py
if [[ "$OS" == windows ]]; then
  # GitHub CLI: named_refs, reply_check and the GitHub review gate call `gh`.
  # The winget package is a machine-wide MSI (an admin prompt), so install the
  # official zip into ~/.local/bin instead, checksum verified. Sign-in stays the
  # owner's step: gh auth login.
  GH_VERSION=2.103.3
  if command -v gh >/dev/null 2>&1; then say "    ok: gh already installed ($(gh --version | head -1))"
  elif ((DRY)); then say "would install gh $GH_VERSION into ~/.local/bin (official zip, sha256 checked)"
  else
    gh_tmp="$(mktemp -d)"
    gh_base="https://github.com/cli/cli/releases/download/v${GH_VERSION}"
    gh_zip="gh_${GH_VERSION}_windows_amd64.zip"
    if curl -fsSL -o "$gh_tmp/$gh_zip" "$gh_base/$gh_zip" && curl -fsSL -o "$gh_tmp/sums" "$gh_base/gh_${GH_VERSION}_checksums.txt" \
       && [[ "$(grep " $gh_zip\$" "$gh_tmp/sums" | cut -d' ' -f1)" == "$(sha256sum "$gh_tmp/$gh_zip" | cut -d' ' -f1)" ]] \
       && unzip -q -o "$gh_tmp/$gh_zip" -d "$gh_tmp/x"; then
      mkdir -p "$HOME/.local/share/gh-cli" "$HOME/.local/bin"
      cp -r "$gh_tmp/x/." "$HOME/.local/share/gh-cli/" && cp "$gh_tmp/x/bin/gh.exe" "$HOME/.local/bin/gh.exe" \
        && say "    ok: gh $GH_VERSION installed in ~/.local/bin (sign in: gh auth login)"
    else say "    FAILED: gh download or checksum; install by hand from https://cli.github.com"; failures+=("gh"); fi
    rm -rf "$gh_tmp"
  fi
  # Note links: wmux opens only http(s) links, so vault-note links in reports go
  # through this 127.0.0.1 server (the com.switchboard.note-links task keeps it up).
  ((DRY)) || run "note links server (wmux)" "$PY" system/scripts/mux/note-links.py ensure
fi

# 6. default agent for switchboard launches
if [[ -n "$AGENT" ]]; then
  if ((DRY)); then say "would set agent: $AGENT in system/config/router.yaml"
  else run "default agent: $AGENT" bash system/scripts/set-agent.sh "$AGENT"; fi
fi

# 7. background services
if ((SERVICES)); then
  case "$OS" in
    macos)
      run "launchd jobs" bash system/scripts/install-launchd.sh
      if [[ "$MUX" == cmux ]]; then run "cmux sidebar" bash system/scripts/install-cmux-sidebar.sh
      else say "    skip: cmux not installed (install cmux, then rerun)"; fi ;;
    windows)
      ps1="$(cygpath -w "$ROOT/system/windows/install.ps1" 2>/dev/null || echo "$ROOT/system/windows/install.ps1")"
      win_root="$(cygpath -w "$ROOT" 2>/dev/null || echo "$ROOT")"
      run "Windows env, PATH and scheduled tasks" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1" -SwitchboardRoot "$win_root" -SkipSeed
      [[ "$MUX" == wmux ]] || say "    note: wmux not found; install it from https://wmux.org, then rerun" ;;
    *) say "    skip: no background services on $OS (policy and skills tier)" ;;
  esac
fi

# 8. checks
if ((! DRY)); then
  run "verify (quick)" bash system/scripts/verify-kit-install.sh --quick
  run "profile leak check" "$PY" system/scripts/check-profile-leaks.py
  if [[ "${SWITCHBOARD_PROFILE:-personal}" == personal ]]; then
    say "connectors: $(switchboard status 2>/dev/null | head -1 || echo 'run: switchboard login google')"
    say "connector setup: profiles/personal/CONNECTORS.md"
  else
    say "connector setup: profiles/company/CONNECTORS.md"
  fi
  if [[ "$OS" == windows ]]; then
    # Optional: without Developer Mode worker worktrees get hard links that self-heal.
    devmode_hint="$("$PY" system/scripts/lib/winlink.py developer-mode-hint 2>/dev/null || true)"
    [[ -n "$devmode_hint" ]] && say "$devmode_hint"
  fi
  case "$MUX" in
    cmux) say "next: open cmux, then: bash system/scripts/ensure-routine-workspaces.sh switchboard launchpad todo comms-triage" ;;
    wmux) say "next: in any wmux pane (PowerShell or bash): powershell -File system/windows/open-routines.ps1" ;;
  esac
fi
if ((${#failures[@]})); then say "done with ${#failures[@]} failed step(s): ${failures[*]}"; exit 1; fi
say "done"
