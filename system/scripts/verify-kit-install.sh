#!/usr/bin/env bash
# Verify an installed switchboard kit: syntax, config parse, placeholders, plugin
# install into a scratch dir, and the curated test suite.
#
# Usage: bash system/scripts/verify-kit-install.sh [--quick | --fast | --full]
#   --quick  syntax, config and placeholder checks only (no tests)
#   --fast   the tests minus the slow suites (SLOW_TESTS below); a good first run on Windows
#   --full   also run the shell tests (needs bash 4+, ruby, a few minutes)
#
# Each test runs under a time limit (SWITCHBOARD_VERIFY_TIMEOUT seconds, default 600; the
# slow suites get SWITCHBOARD_VERIFY_SLOW_TIMEOUT, default 2400), prints a start line,
# and keeps its full output in a log folder named at the end.
# Exit 0 when everything passes. Prints one line per check and a summary.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
mode=tests
for arg in "$@"; do
  case "$arg" in
    --quick) mode=quick ;;
    --fast) mode=fast ;;
    --full) mode=full ;;
    -h|--help) sed -n 2,13p "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

pass=0; fail=0; failures=()
ok()   { pass=$((pass+1)); printf '  ok    %s\n' "$1"; }
bad()  { fail=$((fail+1)); failures+=("$1"); printf '  FAIL  %s\n' "$1"; }
# Full output of every check, one file each (install report v3: a 20-line tail
# hid 43 of the 45 test_dispatch.py errors). They live in the gitignored local/, not in
# ~/.switchboard.
LOG_DIR="$ROOT/local/verify-logs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="$(mktemp -d)"
TEST_TIMEOUT="${SWITCHBOARD_VERIFY_TIMEOUT:-600}"
# Suites that take minutes on Windows (a bash or git process per case). --fast skips them,
# and the default run gives them a longer limit: on a Windows laptop test_report_delivery.py
# passes in about 21 minutes, so the plain 600 s limit killed it and it read as a hang.
SLOW_TESTS=" test_dispatch.py test_report_delivery.py test_publish_switchboard_kit.py test_running_task_ledger.py test_sod_workspace_plan.py "
SLOW_TEST_TIMEOUT="${SWITCHBOARD_VERIFY_SLOW_TIMEOUT:-2400}"
# The time limit for one test, by its file name.
test_limit() { # name
  if [[ "$SLOW_TESTS" == *" $1 "* ]]; then printf '%s' "$SLOW_TEST_TIMEOUT"; else printf '%s' "$TEST_TIMEOUT"; fi
}
log_name() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }
check() { # name, command...
  local name="$1"; shift
  local log; log="$LOG_DIR/$(log_name "$name").log"
  if "$@" >"$log" 2>&1; then ok "$name"; else bad "$name"; sed 's/^/        /' "$log" | tail -20; printf '        full log: %s\n' "$log"; fi
}
# One test: a start line, a time limit, and the elapsed seconds.
run_test() { # name, command...
  local name="$1"; shift
  local log start status limit; log="$LOG_DIR/$(log_name "$name").log"; start=$SECONDS
  limit="$(test_limit "$name")"
  printf '  ...   %s (started %s)\n' "$name" "$(date +%H:%M:%S)"
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 10 "$limit" "$@" >"$log" 2>&1; status=$?
  else
    "$@" >"$log" 2>&1; status=$?
  fi
  if (( status == 0 )); then ok "$name ($((SECONDS - start))s)"
  elif (( status == 124 || status == 137 )); then bad "$name (timed out after ${limit}s)"; printf '        full log: %s\n' "$log"
  else bad "$name ($((SECONDS - start))s)"; sed 's/^/        /' "$log" | tail -20; printf '        full log: %s\n' "$log"; fi
}

echo "switchboard kit verify ($mode) in $ROOT"

# Python: SWITCHBOARD_PYTHON, else a python3 that runs, else python, else py -3. On
# Windows `python3` is often the Microsoft Store alias, which only prints
# "Python was not found", and xargs or a subprocess never sees a bash function.
# PY is one absolute interpreter path, exported as SWITCHBOARD_PYTHON for the checks.
PY=""
for candidate in "${SWITCHBOARD_PYTHON:-}" python3 python "py -3"; do
  [[ -n "$candidate" ]] || continue
  if [[ "$candidate" == "py -3" ]]; then
    exe="$(py -3 -c 'import sys; print(sys.executable)' 2>/dev/null | tr -d '\r')" || continue
  else  # quoted: SWITCHBOARD_PYTHON may hold a path with spaces (C:/Program Files/...)
    exe="$("$candidate" -c 'import sys; print(sys.executable)' 2>/dev/null | tr -d '\r')" || continue
  fi
  [[ -n "$exe" ]] || continue
  if command -v cygpath >/dev/null 2>&1; then exe="$(cygpath -u "$exe")"; fi
  PY="$exe"; break
done
if [[ -z "$PY" ]]; then
  echo "  FAIL  no working Python 3 (python3, python and py -3 all failed; on Windows turn off the python.exe and python3.exe App Execution Aliases or rerun install.ps1)"
  exit 1
fi
export SWITCHBOARD_PYTHON="$PY"
echo "python: $PY"

# 0. Applied decisions (docs/install-decisions.md): the same six lines install.py printed.
mux_backend=none
if [[ -f system/scripts/lib/mux.sh ]]; then
  mux_backend="$(bash -c 'source system/scripts/lib/mux.sh && mux_detect' 2>/dev/null || echo none)"
fi
# wmux CLI and liveness through lib/mux.py. wmux puts its CLI on PATH only inside
# its own panes, and `wmux ping` is the one verb that answers without the pipe
# token (install report v3, bug 6: "not on PATH" and "not running" inside a pane).
wmux_cli=""; wmux_state="not checked"
if [[ $mux_backend == wmux && -f system/scripts/lib/mux.py ]]; then
  wmux_probe="$("$PY" -c 'import sys
sys.path.insert(0, "system/scripts")
from lib.mux import wmux_cli, wmux_running
argv = wmux_cli()[0]
print("running" if argv and wmux_running() else ("not running" if argv else "not installed"))
print(" ".join(argv))' 2>/dev/null | tr -d '\r')"
  wmux_state="$(printf '%s\n' "$wmux_probe" | sed -n 1p)"
  wmux_cli="$(printf '%s\n' "$wmux_probe" | sed -n 2p)"
  [[ -n "$wmux_state" ]] || wmux_state="not checked"
fi
if [[ -n "${SWITCHBOARD_RUNTIME:-}" ]]; then
  case "${SWITCHBOARD_RUNTIME}" in wsl) runtime=wsl ;; native) runtime=windows-native ;; *) runtime="$SWITCHBOARD_RUNTIME" ;; esac
else
  case "$(uname -s 2>/dev/null)" in
    Darwin) runtime=macos ;;
    MINGW*|MSYS*|CYGWIN*) runtime=windows-native ;;
    Linux) if [[ -n "${WSL_DISTRO_NAME:-}${WSL_INTEROP:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null; then runtime=wsl; else runtime=linux; fi ;;
    *) runtime=unknown ;;
  esac
fi
harnesses=""
for h in amp claude codex cursor; do command -v "$h" >/dev/null 2>&1 && harnesses="${harnesses:+$harnesses,}$h"; done
has_amp=no; command -v amp >/dev/null 2>&1 && has_amp=yes
scope=policy-and-skills
if [[ $has_amp == yes && ( $mux_backend == cmux || $mux_backend == wmux ) ]]; then scope=full-loop; fi
synced=""
# Split on / only, so a segment with a space ("My Files", "Google Drive") stays whole.
IFS=/ read -r -a root_parts <<< "$ROOT"
for part in "${root_parts[@]}"; do
  low="$(printf '%s' "$part" | tr '[:upper:]' '[:lower:]')"
  case "$low" in
    *dropbox*|*onedrive*|*icloud*|*com~apple~clouddocs*|*"mobile documents"*|*"google drive"*|*googledrive*|*cloudstorage*|*"box sync"*|*box-box*) synced="$part"; break ;;
  esac
done
echo "applied decisions (docs/install-decisions.md):"
if [[ -n "$synced" ]]; then
  worktrees_dir="${SWITCHBOARD_WORKTREES_DIR:-}"
  if [[ -z "$worktrees_dir" ]]; then
    if [[ $runtime == windows-native ]]; then worktrees_dir="${LOCALAPPDATA:-$HOME/AppData/Local}/switchboard/worktrees"; else worktrees_dir="$HOME/.local/state/switchboard/worktrees"; fi
  fi
  ok "decision: location   $ROOT (synced folder '$synced'; worktrees and state relocated)"
  printf '  note  worktrees: %s  state: %s  (one device at a time on this checkout)\n' "$worktrees_dir" "${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}"
  if [[ -f "${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}/env" ]]; then ok "env file ${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}/env present"; else bad "env file ${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}/env missing (re-run install.py --update)"; fi
else
  ok "decision: location   $ROOT (not synced)"
fi
# The install folder is the owner's choice: the env file record and the shell's
# SWITCHBOARD_ROOT must name this checkout, or skills and tasks read another folder.
same_root() { # value -> 0 when it names $ROOT (Windows C:\ form or POSIX form)
  local v="$1" p
  if [[ "$v" == *:\\* || "$v" == *:/* ]] && command -v cygpath >/dev/null 2>&1; then v="$(cygpath -u "$v" 2>/dev/null || printf '%s' "$v")"; fi
  p="$(cd "$v" 2>/dev/null && pwd -P)" || return 1
  [[ "$p" == "$(pwd -P)" ]]
}
env_file="${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}/env"
if [[ -f "$env_file" ]]; then
  recorded="$(sed -n 's/^SWITCHBOARD_ROOT=//p' "$env_file" | tr -d '\r' | tail -1)"
  if [[ -z "$recorded" ]]; then
    bad "env file $env_file has no SWITCHBOARD_ROOT (re-run install.py --update)"
  elif same_root "$recorded"; then
    ok "env file records this folder (SWITCHBOARD_ROOT=$recorded)"
  else
    bad "env file $env_file records SWITCHBOARD_ROOT=$recorded, not this folder $ROOT (re-run install.py --destination \"$ROOT\" --update; on Windows also install.ps1 -SwitchboardRoot)"
  fi
fi
if [[ -n "${SWITCHBOARD_ROOT:-}" ]]; then
  if same_root "$SWITCHBOARD_ROOT"; then ok "shell SWITCHBOARD_ROOT names this folder"
  else bad "shell SWITCHBOARD_ROOT=$SWITCHBOARD_ROOT, not this folder $ROOT (open a new shell after install.ps1, or fix the user environment)"
  fi
fi
ok "decision: scope      $scope (amp: $has_amp, mux: $mux_backend, harnesses: ${harnesses:-none})"
ok "decision: runtime    $runtime"
# One rule for the full switchboard CLI (system/connectors/company_cli.py): the kit's connector shim is not it.
switchboard_kind="$("$PY" system/connectors/company_cli.py 2>/dev/null | tr -d '\r' | cut -d' ' -f1)"
[[ -n "$switchboard_kind" ]] || switchboard_kind=absent
if [[ $switchboard_kind == real ]]; then ok "decision: internal-tooling  active (another switchboard CLI on PATH)"
elif [[ $switchboard_kind == shim ]]; then printf '  note  decision: internal-tooling  dormant (only the connector shim on PATH; skills that need switchboard report that at use time, nothing to remove)\n'
else printf '  note  decision: internal-tooling  dormant (switchboard not on PATH; skills that need it report that at use time, nothing to remove)\n'; fi
if [[ $scope != full-loop ]]; then ok "decision: sidebar    none (no terminal backend)"
elif [[ $mux_backend == wmux ]]; then
  ok "decision: sidebar    wmux-native (set-status lanes, report-agent blocked, targeted notify; seed-session.ps1 -Restart for pins, colors, order; panes open in Git Bash; board opt-in)"
  if ! command -v cmux >/dev/null 2>&1; then bad "cmux shim not on PATH (open a new Git Bash window; install.ps1 appends the switchboard-env.sh source line to ~/.bashrc)"
  elif [[ $wmux_state != running ]]; then printf '  note  wmux is %s; rerun verify from a wmux pane to check cmux sidebar lanes --json\n' "$wmux_state"
  elif cmux sidebar lanes --json >/dev/null 2>&1; then ok "cmux sidebar lanes --json renders the lane view through the shim"; else bad "cmux sidebar lanes --json failed (run CMUX_SHIM_TRACE=1 cmux list-workspaces)"; fi
else ok "decision: sidebar    cmux-sidebar (native)"; fi
if [[ $switchboard_kind == real ]]; then ok "decision: connectors switchboard (internal CLI)"
elif [[ -x system/connectors/switchboard || -f system/connectors/switchboard.cmd ]]; then
  ok "decision: connectors switchboard shim (system/connectors; run switchboard login)"
  if command -v switchboard >/dev/null 2>&1; then ok "switchboard shim on PATH ($(switchboard --version 2>/dev/null))"; else printf '  note  system/connectors is not on PATH yet; install.ps1 or system/windows/switchboard-env.sh adds it\n'; fi
else bad "decision: connectors none (switchboard absent and system/connectors/switchboard missing; re-run install.py --update)"; fi

# 1. Toolchain
check "python3 >= 3.11" "$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
check "PyYAML importable" "$PY" -c 'import yaml'
if command -v ruby >/dev/null 2>&1; then ok "ruby present (background-task config test)"
elif [[ $scope == full-loop ]]; then
  case "$runtime" in
    windows-native) ruby_hint="winget install RubyInstallerTeam.Ruby.3.3, then open a new terminal" ;;
    macos) ruby_hint="brew install ruby or macOS system ruby" ;;
    *) ruby_hint="install ruby with your package manager" ;;
  esac
  bad "ruby missing (full loop needs it for run-background-tasks.sh; $ruby_hint)"
else printf '  note  ruby not on PATH (only the full loop needs it; not required for %s)\n' "$scope"; fi
for tool in git rg; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool present"
  elif [[ $runtime == windows-native ]]; then bad "$tool missing (installed with winget? open a new terminal so PATH reloads; install.ps1 adds the folders winget skips)"
  else bad "$tool missing"; fi
done
for tool in amp cmux; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool present"; else printf '  note  %s not on PATH (full loop only; scope is %s)\n' "$tool" "$scope"; fi
done
# Claude Code: policy loads through ~/.claude/CLAUDE.md, skills are copies in
# ~/.claude/skills, memory is Claude Code's built-in memory unless the owner
# chose claude-mem (install-claude-code.sh --memory claude-mem).
if command -v claude >/dev/null 2>&1; then
  ok "claude present ($(claude --version 2>/dev/null | head -1))"
  # The managed block from install-adapters.sh, or a hand-written @import of this root's AGENTS.md, both count.
  # README step 4 runs verify before the Claude steps, so "not installed yet" is a note with the command,
  # and only an installed adapter whose skill copies drift from the manifest is a failure.
  claude_adapter=0
  if [[ -f "$HOME/.claude/CLAUDE.md" ]] && { grep -q 'switchboard-kit:start' "$HOME/.claude/CLAUDE.md" || grep -qF "$PWD/AGENTS.md" "$HOME/.claude/CLAUDE.md"; }; then
    claude_adapter=1; ok "~/.claude/CLAUDE.md loads the switchboard policy"
  else
    printf '  note  ~/.claude/CLAUDE.md has no switchboard block yet (bash adapters/install-adapters.sh --apply)\n'
  fi
  if [[ -f system/scripts/install-claude-skills.py ]]; then
    if "$PY" system/scripts/install-claude-skills.py --check --skip-missing >/dev/null 2>&1; then
      ok "~/.claude/skills matches the manifest"
    elif [[ $claude_adapter == 1 ]]; then
      bad "~/.claude/skills drifts from the manifest (bash system/scripts/install-claude-code.sh)"
    else
      printf '  note  ~/.claude/skills not installed yet (bash adapters/install-adapters.sh --apply, then bash system/scripts/install-claude-code.sh)\n'
    fi
  fi
  # Memory: only check the claude-mem worker when the owner chose it (its
  # settings file exists). Built-in memory has nothing to verify.
  if [[ -f "$HOME/.claude-mem/settings.json" && -f system/scripts/check-claude-mem.py ]]; then
    if "$PY" system/scripts/check-claude-mem.py --quiet >/dev/null 2>&1; then ok "claude-mem worker healthy (telemetry off, autostart off)"; else printf '  note  claude-mem worker not healthy (bash system/scripts/install-claude-code.sh --memory claude-mem; python3 system/scripts/check-claude-mem.py for detail)\n'; fi
  else
    ok "memory: Claude Code built-in (claude-mem not installed; --memory claude-mem to switch)"
  fi
  # switchboard plugin: hooks run the Amp plugin modules through Bun, so Claude Code
  # gets the same guardrails. Installed by install-claude-code.sh step 3.
  bridge=system/claude-marketplace/plugins/switchboard/hooks/amp-bridge.ts
  if [[ -f "$bridge" ]]; then
    if claude plugin list 2>/dev/null | grep -q 'switchboard@switchboard-local'; then
      ok "switchboard@switchboard-local plugin installed"
    elif [[ $claude_adapter == 1 ]]; then
      printf '  warn  switchboard@switchboard-local plugin not installed (bash system/scripts/install-claude-code.sh --skip-mcp)\n'
    else
      printf '  note  switchboard@switchboard-local plugin not installed yet (bash system/scripts/install-claude-code.sh)\n'
    fi
    if command -v bun >/dev/null 2>&1; then
      deny_probe='{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"git add -A"},"session_id":"verify","cwd":"/tmp"}'
      if printf '%s' "$deny_probe" | bun "$bridge" pre-tool-use 2>/dev/null | grep -q '"permissionDecision": *"deny"'; then
        ok "plugin bridge denies git add -A"
      else
        printf '  warn  plugin bridge did not deny git add -A (echo <PreToolUse json> | bun %s pre-tool-use)\n' "$bridge"
      fi
    else
      printf '  note  bun not on PATH; the switchboard plugin hooks are inert until it is installed (macOS: brew install oven-sh/bun/bun, Windows: winget install Oven-sh.Bun, Linux: curl -fsSL https://bun.sh/install | bash)\n'
    fi
  fi
else
  printf '  note  claude not on PATH (optional; install Claude Code, then bash system/scripts/install-claude-code.sh)\n'
fi
# Obsidian reads the notes switchboard writes (review links are obsidian://open?vault=switchboard).
# graphify (knowledge graph) and memory are separate tools that never need Obsidian.
obsidian_app=""
case "$runtime" in
  windows-native) for c in "${LOCALAPPDATA:-}/Programs/Obsidian/Obsidian.exe" "${PROGRAMFILES:-C:/Program Files}/Obsidian/Obsidian.exe"; do [[ -f "$c" ]] && { obsidian_app="$c"; break; }; done ;;
  macos) [[ -d /Applications/Obsidian.app ]] && obsidian_app=/Applications/Obsidian.app ;;
  *) command -v obsidian >/dev/null 2>&1 && obsidian_app="$(command -v obsidian)" ;;
esac
if [[ -n "$obsidian_app" ]]; then
  ok "Obsidian installed ($obsidian_app)"
  if [[ ! -f system/scripts/register-obsidian-vault.py ]]; then :
  elif vault_json="$("$PY" system/scripts/register-obsidian-vault.py --check 2>/dev/null)"; then ok "Obsidian vault switchboard points at this checkout"
  else printf '  warn  Obsidian has no switchboard vault for this checkout, so review links open nothing (python3 system/scripts/register-obsidian-vault.py)\n'; fi
  if [[ $runtime == windows-native ]] && command -v powershell.exe >/dev/null 2>&1; then
    if powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$ROOT/system/windows/register-obsidian-notes.ps1" 2>/dev/null || echo system/windows/register-obsidian-notes.ps1)" -Check >/dev/null 2>&1; then
      ok "a clicked .md note opens in its Obsidian vault"
    else printf '  warn  .md files open with Obsidian.exe directly, which ignores notes already in a vault (powershell -File system\\windows\\register-obsidian-notes.ps1)\n'; fi
  fi
  [[ -f .obsidian/app.json ]] && ok ".obsidian shared vault config present" || printf '  warn  .obsidian/app.json missing (tracked in git; git checkout -- .obsidian)\n'
else
  case "$runtime" in
    windows-native) printf '  warn  Obsidian not installed; it is how you read switchboard notes (winget install --id Obsidian.Obsidian -e, then python3 system/scripts/register-obsidian-vault.py)\n' ;;
    macos) printf '  warn  Obsidian not installed; it is how you read switchboard notes (brew install --cask obsidian, then python3 system/scripts/register-obsidian-vault.py)\n' ;;
    *) printf '  note  Obsidian not found (optional reader for switchboard notes)\n' ;;
  esac
fi
# Knowledge graph: graphify CLI plus graphify-out/graph.json, rebuilt by the
# build-knowledge-graph background task.
if [[ ! -f system/scripts/build-knowledge-graph.py ]]; then :
elif command -v graphify >/dev/null 2>&1; then
  ok "graphify present ($(graphify --version 2>/dev/null | head -1))"
  # One graph per repo, on the main checkout (a task worktree has no copy).
  graph_root="$(cd "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd || echo .)"
  graph_file="$graph_root/graphify-out/graph.json"
  if [[ -f "$graph_file" ]]; then
    graph_age_h=$(( ( $(date +%s) - $(date -r "$graph_file" +%s 2>/dev/null || echo 0) ) / 3600 ))
    if (( graph_age_h <= 48 )); then ok "knowledge graph built (graphify-out/graph.json, ${graph_age_h}h old)"
    else printf '  warn  knowledge graph is %sh old (python3 system/scripts/build-knowledge-graph.py)\n' "$graph_age_h"; fi
  else printf '  warn  knowledge graph not built yet (python3 system/scripts/build-knowledge-graph.py)\n'; fi
else
  printf '  warn  graphify not installed, so workers skip the knowledge graph (uv tool install graphifyy==0.9.95 --python 3.12)\n'
fi
# Terminal backend: cmux on macOS, wmux (via system/scripts/mux/cmux) on Windows/WSL.
case "$mux_backend" in
  cmux) ok "mux backend: cmux" ;;
  wmux)
    if [[ -n "$wmux_cli" ]]; then ok "mux backend: wmux, $wmux_state (CLI $wmux_cli; cmux calls go through system/scripts/mux/cmux)"
    else printf '  warn  mux backend is wmux but no wmux CLI found (WMUX_BIN, PATH, LOCALAPPDATA/Programs/wmux, scoop); see system/windows/README.md\n'; fi
    check "cmux-shim and mux backend unit tests" bash -c 'cd system/scripts && "$SWITCHBOARD_PYTHON" -m unittest -q test_cmux_shim test_mux_backends' ;;
  *) printf '  note  no terminal backend (cmux on macOS, wmux on Windows); policy and skills run without sidebar integration\n' ;;
esac
if [[ $scope == full-loop && $runtime != macos ]]; then
  printf '  note  launchd is macOS-only; on Windows run system\\windows\\register-scheduled-tasks.ps1 (see system/windows/README.md)\n'
fi
# Windows install doctor: env keys the scheduled tasks
# need, the \switchboard\ tasks, background-task errors, CLIs (gh, node, uv, bun),
# the Claude MCP servers, connectors, recall inputs and the wmux note-link
# server. Its warn lines are printed here and never fail verify.
if [[ $runtime == windows-native && -f system/scripts/check-windows-install.py ]]; then
  "$PY" system/scripts/check-windows-install.py 2>/dev/null | sed -e 's/^  ok    /  ok    win: /' -e 's/^  warn  /  warn  win: /' -e 's/^  owner /  note  win (owner): /'
fi

# 2. Syntax
check "python: py_compile every .py" bash -c 'find system daemon .agents -name "*.py" -not -path "*/__pycache__/*" -print0 | xargs -0 "$SWITCHBOARD_PYTHON" -m py_compile'
check "bash -n every .sh" bash -c 'status=0; while IFS= read -r -d "" f; do bash -n "$f" || { echo "$f"; status=1; }; done < <(find system daemon .agents -name "*.sh" -print0); exit $status'
check "yaml parses" "$PY" - <<'PY'
import sys, yaml
from pathlib import Path
bad = []
for p in list(Path("system").rglob("*.yaml")) + list(Path("routines").rglob("*.yaml")) + list(Path("workstreams").rglob("*.yaml")):
    try:
        yaml.safe_load(p.read_text())
    except Exception as e:  # noqa: BLE001
        bad.append(f"{p}: {e}")
print("\n".join(bad)); sys.exit(1 if bad else 0)
PY
check "json parses" "$PY" - <<'PY'
import json, sys
from pathlib import Path
bad = []
for p in list(Path("system").rglob("*.json")) + list(Path(".switchboard-kit").glob("*.json")):
    if "node_modules" in p.parts: continue
    try: json.loads(p.read_text())
    except Exception as e: bad.append(f"{p}: {e}")  # noqa: BLE001
print("\n".join(bad)); sys.exit(1 if bad else 0)
PY
if command -v plutil >/dev/null 2>&1; then
  check "plutil -lint launchd plists" bash -c 'plutil -lint system/launchd/*.plist'
fi

# 3. Placeholders rendered (install.py should have replaced every {{TOKEN}})
check "no unrendered {{PLACEHOLDER}} tokens" bash -c '! rg -n "\{\{(OWNER_[A-Z_]+|SWITCHBOARD_ROOT|HOME|SWITCHBOARD_SLACK_BOT_CHANNEL)\}\}" --glob "!.switchboard-kit/**" --glob "!docs/kit-port/**" --glob "!docs/kit-changes-since-*.md" --glob "!install.py" --glob "!README.md" --glob "!CONTRIBUTING.md" --glob "!system/scripts/test_local_backup.py" --glob "!system/scripts/test_three_way_kit.py" . '
# A macOS home path from the publisher, left in a rendered file. Expected, so not hits:
#   * the redaction fixture "<home root>/person" used by the privacy test;
#   * this user's own home: install.py renders the HOME token to it, and on
#     Windows that is the C:/... form of the home folder (plus cygpath's other forms);
#   * this install's own folder: install.py renders the switchboard root token to it, and
#     it need not sit under HOME (another drive, or a temp HOME in a clean test);
#   * system/vendor/skills, third-party docs the publisher scans separately.
# The patterns open with [/] and MSYS_NO_PATHCONV=1 is set, because Git Bash
# rewrites an argument that starts with / into a Windows path before rg sees it.
home_root='[/]Use''rs/'
home_forms=("$HOME" "$ROOT")
if command -v cygpath >/dev/null 2>&1; then
  home_forms+=("$(cygpath -m "$HOME")" "$(cygpath -w "$HOME")" "$(cygpath -m "$ROOT")" "$(cygpath -w "$ROOT")")
fi
publisher_paths() {
  local hits form
  hits="$(MSYS_NO_PATHCONV=1 rg -n "${home_root}[a-z]" --glob '!.switchboard-kit/**' --glob '!README.md' --glob '!install.py' \
    --glob '!system/scripts/verify-kit-install.sh' --glob '!system/kit/test_build_tier.py' --glob '!system/vendor/skills/**' . \
    | MSYS_NO_PATHCONV=1 rg --passthru -r '' "${home_root}(person|someone|tester|invented|pat|x|realguy|jane_doe|a|example)\b" \
    | MSYS_NO_PATHCONV=1 rg "${home_root}[a-z]")"  # drop only the test homes, so a real path on the same line still shows
  for form in "${home_forms[@]}"; do
    [[ -n "$hits" ]] || break
    [[ -n "$form" ]] || continue  # an empty form (cygpath printed nothing) would drop every hit
    hits="$(printf '%s\n' "$hits" | rg -v -F -- "$form")"
  done
  [[ -z "$hits" ]] || { printf '%s\n' "$hits" | head -20; return 1; }
}
check "no publisher paths left behind" publisher_paths

# 4. Amp plugins install into a scratch target (never touches ~/.config)
scratch="$(mktemp -d)"
check "install-amp-plugins.sh into scratch dir" env SWITCHBOARD_PLUGIN_TARGET_DIR="$scratch" bash system/scripts/install-amp-plugins.sh
rm -rf "$scratch"

# 5. Skills installer dry run
check "install-amp-skills.sh --dry-run" bash system/bin/install-amp-skills.sh --dry-run

[[ $mode == quick ]] && { echo; echo "quick checks: $pass ok, $fail failed"; exit $(( fail > 0 )); }

# 6. Curated python tests (every test_*.py shipped in the kit)
echo "python tests (time limit ${TEST_TIMEOUT}s each, ${SLOW_TEST_TIMEOUT}s for the slow suites; full logs in $LOG_DIR):"
skipped=()
for t in system/scripts/test_*.py system/scripts/test-*.py; do
  [[ -f "$t" ]] || continue
  if [[ $mode == fast && "$SLOW_TESTS" == *" $(basename "$t") "* ]]; then skipped+=("$(basename "$t")"); continue; fi
  run_test "$(basename "$t")" "$PY" "$t"
done
if (( ${#skipped[@]} )); then printf '  note  --fast skipped the slow suites: %s (run without --fast to include them)\n' "${skipped[*]}"; fi

# 7. Shell tests (opt-in; they spin up fake cmux/amp fixtures)
if [[ $mode == full ]]; then
  echo "shell tests:"
  for t in system/scripts/test-*.sh; do
    run_test "$(basename "$t")" bash "$t"
  done
fi

echo
echo "verify: $pass ok, $fail failed (full logs: $LOG_DIR)"
if (( fail > 0 )); then printf '  - %s\n' "${failures[@]}"; exit 1; fi
