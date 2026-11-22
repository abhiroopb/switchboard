#!/usr/bin/env bash
# Set up Claude Code for PM OS on macOS, Linux, or Windows (Git for Windows bash).
#
# One command does every Claude-specific step that the Amp path does not cover:
#
#   1. skills      copy the manifest skills into ~/.claude/skills
#                  (system/config/claude-skills-manifest.yaml, copied, never symlinked).
#                  Pinned third-party skills ship in system/vendor/skills, so this
#                  step needs no network. --refresh-vendor re-fetches them with git.
#   2. memory      --memory builtin (default) keeps Claude Code's own memory and
#                  changes nothing. --memory claude-mem installs claude-mem
#                  (pinned, telemetry off, worker autostart off) and registers
#                  the worker as a service the OS owns:
#                    macOS    launchd  com.pm-os.claude-mem  (system/launchd/)
#                    Linux    systemd  ~/.config/systemd/user/pm-os-claude-mem.service
#                    Windows  Task Scheduler  \PM OS\com.pm-os.claude-mem
#                                             (system/windows/register-scheduled-tasks.ps1)
#   3. plugins     the official plugins PM OS relies on, plus the PM OS plugin
#                  itself (pm-os@pm-os-local) from the marketplace folder
#                  system/claude-marketplace. Its hooks run the Amp plugin
#                  modules through Bun, so guardrails, liveness, and reports
#                  behave the same in Claude Code and in Amp.
#   4. mcp         user-scope MCP servers: chrome-devtools (sanctioned headless
#                  profile; a different entry is replaced; on a machine opted in
#                  with owner_browser.py, the agent profile plus chrome-devtools-owner
#                  and chrome-devtools-headless). An organization adds its own
#                  servers through its profile.
#   5. settings    with --memory claude-mem only: DISABLE_AUTOUPDATER,
#                  CLAUDE_CODE_DISABLE_AUTO_MEMORY and skipDangerousModePermissionPrompt
#                  in ~/.claude/settings.json. With --memory builtin (the
#                  default) those three keys are left alone: auto-updater on,
#                  built-in memory on, permission prompt on. (Step 3 still
#                  records the plugin under enabledPlugins in the same file.)
#   6. trust       records Claude Code's folder trust in ~/.claude.json for the
#                  PM OS folder and the task worktrees folder
#                  (PM_OS_WORKTREES_DIR), so a new worker pane does not stop at
#                  "Do you trust the files in this folder?". --no-folder-trust
#                  skips it.
#
# Every step is idempotent. Rerun after `git pull` or a kit `--update`.
#
# Usage:
#   bash system/scripts/install-claude-code.sh                   # do everything
#   bash system/scripts/install-claude-code.sh --dry-run         # print the plan only
#   bash system/scripts/install-claude-code.sh --refresh-vendor  # re-fetch vendor skills (network)
#   bash system/scripts/install-claude-code.sh --memory claude-mem   # replace built-in memory
#   bash system/scripts/install-claude-code.sh --skip-plugins --skip-mcp
#   bash system/scripts/install-claude-code.sh --home /tmp/fake-home   # tests
#   bash system/scripts/install-claude-code.sh --allow-nested     # run steps 3-4 inside Claude Code
#
# Run it from a plain terminal (Terminal, Git Bash, PowerShell), not from a
# Claude Code session. Steps 3 and 4 drive the `claude` CLI; inside a session
# they are skipped unless you pass --allow-nested, and every `claude` call has a
# timeout (CLAUDE_CLI_TIMEOUT seconds, default 120) with stdin closed, so a
# blocked call prints the manual command and the script moves on.
#
# Environment:
#   PM_OS_PYTHON          python interpreter (default python3, then py -3 on Windows)
#   CLAUDE_MEM_VERSION    claude-mem version to install (default below)
#   CLAUDE_CLI_TIMEOUT    seconds before one `claude plugin|mcp` call is abandoned (default 120)
#
# Requirements: claude on PATH. bun for the PM OS plugin hooks and the memory
# worker. npx (Node 18+) for memory and chrome-devtools.
# Missing optional tools skip their step with a one-line note and the install
# command for the OS, never a prompt. Kill switch for the plugin hooks:
# ~/.claude/pm-os-plugin-config.json {"pm-os-bridge.disabled": true}.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOME_DIR="${HOME}"
DRY=0
REFRESH_VENDOR=0
MEMORY="builtin"   # builtin | claude-mem
SKIP_PLUGINS=0
SKIP_MCP=0
ALLOW_NESTED=0
FOLDER_TRUST=1    # --no-folder-trust: leave ~/.claude.json alone
CLAUDE_CLI_TIMEOUT="${CLAUDE_CLI_TIMEOUT:-120}"
CLAUDE_MEM_VERSION="${CLAUDE_MEM_VERSION:-15.8.1}"
PLUGINS=(claude-md-management frontend-design hookify playground receipts security-guidance)
PM_OS_MARKETPLACE_NAME="pm-os-local"
PM_OS_PLUGIN="pm-os@${PM_OS_MARKETPLACE_NAME}"
CHROME_DEVTOOLS_MCP_VERSION="1.9.0"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --home) HOME_DIR="$2"; shift ;;
    --refresh-vendor) REFRESH_VENDOR=1 ;;
    --skip-vendor) REFRESH_VENDOR=0 ;;   # kept for old docs; vendor fetch is already off by default
    --memory) MEMORY="$2"; shift ;;
    --skip-memory) MEMORY="builtin" ;;   # old spelling of --memory builtin
    --skip-plugins) SKIP_PLUGINS=1 ;;
    --skip-mcp) SKIP_MCP=1 ;;
    --allow-nested) ALLOW_NESTED=1 ;;
    --no-folder-trust) FOLDER_TRUST=0 ;;
    --claude-mem-version) CLAUDE_MEM_VERSION="$2"; shift ;;
    -h|--help) awk 'NR > 1 && /^set -euo/ { exit } NR > 1' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;   # the whole header, however long
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$MEMORY" in
  builtin|claude-mem) ;;
  *) echo "--memory must be builtin or claude-mem (got: $MEMORY)" >&2; exit 2 ;;
esac

say()  { printf '%s\n' "$*"; }
note() { printf '  · %s\n' "$*"; }
step() { printf '\n## %s\n' "$*"; }
run()  {
  if [ "$DRY" = 1 ]; then printf '  would  %s\n' "${*/#claude_cli/claude}"; else printf '  run    %s\n' "${*/#claude_cli/claude}"; "$@"; fi
}

# with_timeout SECS CMD...: run CMD with stdin closed; exit 124 when it outlives SECS.
with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@" </dev/null; return; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@" </dev/null; return; fi
  "$@" </dev/null &
  local pid=$! watchdog rc=0
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) &
  watchdog=$!
  wait "$pid" || rc=$?
  if kill "$watchdog" 2>/dev/null; then wait "$watchdog" 2>/dev/null || true
  else rc=124; fi
  return "$rc"
}

# claude_cli ARGS...: one `claude` call with progress, a timeout and no stdin.
# After the first timeout every later call is skipped, so a stuck CLI costs one
# timeout, not one per plugin.
# Progress and timeout notes go to fd 3 (the terminal), so callers can silence
# claude's own output without hiding them. Call it in the current shell, never
# inside $(...): the stuck flag must survive the call (claude_capture for output).
exec 3>&1
CLAUDE_CLI_STUCK=0
claude_cli() {
  if [ "$CLAUDE_CLI_STUCK" = 1 ]; then
    printf '  · skipped: claude %s (an earlier claude call timed out)\n' "$*" >&3
    return 124
  fi
  printf '  ...    claude %s  (timeout %ss)\n' "$*" "$CLAUDE_CLI_TIMEOUT" >&3
  local rc=0
  with_timeout "$CLAUDE_CLI_TIMEOUT" claude "$@" || rc=$?
  if [ "$rc" = 124 ]; then
    CLAUDE_CLI_STUCK=1
    printf '  · claude %s did not finish in %ss; stopped it. Run it by hand from a plain terminal.\n' "$*" "$CLAUDE_CLI_TIMEOUT" >&3
  fi
  return "$rc"
}
# claude_capture ARGS...: claude_cli with its stdout in $CLAUDE_OUT.
CLAUDE_OUT=""
claude_capture() {
  local out
  out="$(mktemp)"
  claude_cli "$@" >"$out" 2>/dev/null || true
  CLAUDE_OUT="$(cat "$out")"
  rm -f "$out"
}

case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;
  *) OS=other ;;
esac

# Python: PM_OS_PYTHON wins. Git for Windows has no python3, so fall back to the
# py launcher there.
if [ -n "${PM_OS_PYTHON:-}" ]; then
  PY="$PM_OS_PYTHON"
elif python3 --version >/dev/null 2>&1; then
  PY=python3
elif py -3 --version >/dev/null 2>&1; then
  PY="py -3"
elif python --version >/dev/null 2>&1; then
  PY=python
else
  echo "python 3 not found; install it first (see README prerequisites)" >&2
  exit 1
fi
# shellcheck disable=SC2206
PYCMD=($PY)

if ! command -v claude >/dev/null 2>&1; then
  cat >&2 <<EOF
claude is not on PATH. Install Claude Code first:
  macOS/Linux:  curl -fsSL https://claude.ai/install.sh | bash
  Windows:      irm https://claude.ai/install.ps1 | iex   (PowerShell)
  any OS:       npm install -g @anthropic-ai/claude-code
then rerun this script.
EOF
  exit 2
fi

bun_hint() {
  case "$OS" in
    macos)   echo "brew install oven-sh/bun/bun" ;;
    windows) echo "winget install Oven-sh.Bun   (or PowerShell: irm bun.sh/install.ps1 | iex)" ;;
    *)       echo "curl -fsSL https://bun.sh/install | bash" ;;
  esac
}
HAVE_BUN=0
if command -v bun >/dev/null 2>&1; then
  HAVE_BUN=1
fi

say "PM OS root:  $ROOT"
say "home:        $HOME_DIR"
say "os:          $OS"
say "python:      $PY"
say "claude:      $(claude --version 2>/dev/null | head -1)"
if [ "$HAVE_BUN" = 1 ]; then
  say "bun:         $(bun --version 2>/dev/null | head -1)"
else
  say "bun:         missing. The PM OS plugin hooks and the memory worker need it: $(bun_hint)"
fi
[ "$DRY" = 1 ] && say "mode:        dry run (nothing is written)"

# Inside a Claude Code session (CLAUDECODE=1) the plugin and MCP steps drive a
# second claude process from the first. On Windows that can block for minutes and
# crash the session, so steps 3 and 4 wait for a plain terminal.
NESTED=0
if [ -n "${CLAUDECODE:-}" ] && [ "$ALLOW_NESTED" = 0 ]; then
  NESTED=1
  say "session:     inside Claude Code; steps 3 (plugins) and 4 (MCP servers) are skipped."
  say "             Finish them from a plain terminal: bash \"$ROOT/system/scripts/install-claude-code.sh\""
  say "             (or pass --allow-nested to run them here anyway)"
fi

# ---------------------------------------------------------------- 1. skills
step "skills -> $HOME_DIR/.claude/skills"
INSTALL_SKILLS="$ROOT/system/scripts/install-claude-skills.py"
if [ ! -f "$INSTALL_SKILLS" ]; then
  note "missing $INSTALL_SKILLS, skipping"
else
  # Pinned third-party skills ship in system/vendor/skills, so the default
  # path needs no network. --refresh-vendor re-fetches them at the pinned commits.
  if [ "$REFRESH_VENDOR" = 1 ]; then
    if command -v git >/dev/null 2>&1; then
      run "${PYCMD[@]}" "$INSTALL_SKILLS" vendor --all || note "vendor refresh failed (offline?); continuing with the shipped copies"
    else
      note "vendor skills: git not on PATH, refresh skipped; using the shipped copies"
    fi
  else
    note "vendor skills: using the shipped copies in system/vendor/skills (pass --refresh-vendor to re-fetch)"
  fi
  skill_args=(--skip-missing --home "$HOME_DIR")
  [ "$DRY" = 1 ] && skill_args+=(--dry-run)
  "${PYCMD[@]}" "$INSTALL_SKILLS" "${skill_args[@]}"
fi

# ---------------------------------------------------------------- 2. memory
if [ "$MEMORY" = "claude-mem" ]; then
  step "memory (claude-mem $CLAUDE_MEM_VERSION, worker owned by the OS)"
else
  step "memory (Claude Code built-in, unchanged)"
fi
CLAUDE_MEM_DIR="$HOME_DIR/.claude-mem"
write_json_merge() {
  # write_json_merge FILE KEY=VALUE...  merge string keys into a JSON object
  # (created when absent). Existing keys with other values are overwritten;
  # unrelated keys are kept.
  local file="$1"; shift
  if [ "$DRY" = 1 ]; then printf '  would  merge %s into %s\n' "$*" "$file"; return; fi
  mkdir -p "$(dirname "$file")"
  "${PYCMD[@]}" - "$file" "$@" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = {}
if path.exists() and path.read_text().strip():
    data = json.loads(path.read_text())
changed = False
for pair in sys.argv[2:]:
    key, _, raw = pair.partition("=")
    value = {"true": True, "false": False}.get(raw, raw)
    if raw.isdigit():
        value = int(raw)
    if data.get(key) != value:
        data[key] = value
        changed = True
if changed:
    path.write_text(json.dumps(data, indent=2) + "\n")
print(f"  {'write' if changed else 'keep '}  {path}")
PY
}

if [ "$MEMORY" = "builtin" ]; then
  note "keeping Claude Code's built-in memory; claude-mem not installed (--memory claude-mem to switch)"
elif ! command -v npx >/dev/null 2>&1; then
  note "npx not on PATH, skipped. Install Node.js (https://nodejs.org or your package manager) and rerun."
else
  # Telemetry lock before the installer runs, so the first run never phones home.
  if [ "$DRY" = 1 ]; then
    printf '  would  write %s/telemetry.json {"enabled": false}\n' "$CLAUDE_MEM_DIR"
  else
    mkdir -p "$CLAUDE_MEM_DIR"
    printf '{"enabled": false}\n' > "$CLAUDE_MEM_DIR/telemetry.json"
  fi
  export DO_NOT_TRACK=1 CI=1
  run npx -y "claude-mem@$CLAUDE_MEM_VERSION" install --ide claude-code --provider claude --runtime worker --no-auto-start --disable-auto-memory
  run npx -y "claude-mem@$CLAUDE_MEM_VERSION" telemetry disable
  write_json_merge "$CLAUDE_MEM_DIR/settings.json" \
    CLAUDE_MEM_RUNTIME=worker \
    CLAUDE_MEM_PROVIDER=claude \
    CLAUDE_MEM_CLAUDE_AUTH_METHOD=subscription \
    CLAUDE_MEM_WORKER_AUTOSTART=false \
    CLAUDE_MEM_PROJECT_NAME_SOURCE=git-remote \
    CLAUDE_MEM_CONTEXT_OBSERVATIONS=20 \
    CLAUDE_MEM_CONTEXT_SESSION_COUNT=5 \
    CLAUDE_MEM_CONTEXT_FULL_COUNT=3 \
    CLAUDE_MEM_WORK_VIEW_AUTO_REFRESH=false \
    CLAUDE_MEM_WELCOME_HINT_ENABLED=false

  if [ "$HAVE_BUN" = 0 ]; then
    note "bun not on PATH: the worker service is not registered. Install bun ($(bun_hint)), reopen the shell, rerun this script."
  else
    mkdir -p "$ROOT/system/state/logs"
    case "$OS" in
      macos)
        run bash "$ROOT/system/scripts/install-launchd.sh" $([ "$DRY" = 1 ] && echo --dry-run) com.pm-os.claude-mem
        ;;
      linux)
        unit_dir="$HOME_DIR/.config/systemd/user"
        unit="$unit_dir/pm-os-claude-mem.service"
        bun_dir="$(dirname "$(command -v bun)")"
        unit_text="[Unit]
Description=PM OS claude-mem worker (Claude Code memory)
After=default.target

[Service]
Type=simple
WorkingDirectory=$CLAUDE_MEM_DIR
Environment=DO_NOT_TRACK=1
Environment=PATH=$bun_dir:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash "$ROOT/system/scripts/claude-mem-worker.sh"
Restart=on-failure
RestartSec=30
StandardOutput=append:$ROOT/system/state/logs/claude-mem.out.log
StandardError=append:$ROOT/system/state/logs/claude-mem.err.log

[Install]
WantedBy=default.target
"
        if [ "$DRY" = 1 ]; then
          printf '  would  write %s and systemctl --user enable --now pm-os-claude-mem.service\n' "$unit"
        else
          mkdir -p "$unit_dir"
          printf '%s' "$unit_text" > "$unit"
          say "  write  $unit"
          if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
            systemctl --user daemon-reload
            systemctl --user enable --now pm-os-claude-mem.service
            systemctl --user --no-pager --lines=0 status pm-os-claude-mem.service || true
          else
            note "systemd user session not available; start the worker with: bash $ROOT/system/scripts/claude-mem-worker.sh &"
          fi
        fi
        ;;
      windows)
        ps1="$ROOT/system/windows/register-scheduled-tasks.ps1"
        win_ps1="$(cygpath -w "$ps1" 2>/dev/null || printf '%s' "$ps1")"
        win_root="$(cygpath -w "$ROOT" 2>/dev/null || printf '%s' "$ROOT")"
        ps_args=(-NoProfile -ExecutionPolicy Bypass -File "$win_ps1" -PmOsRoot "$win_root" -Labels com.pm-os.claude-mem)
        [ "$DRY" = 1 ] && ps_args+=(-DryRun)
        if command -v powershell.exe >/dev/null 2>&1; then
          run powershell.exe "${ps_args[@]}"
          [ "$DRY" = 0 ] && run powershell.exe -NoProfile -Command "Start-ScheduledTask -TaskPath '\PM OS\' -TaskName 'com.pm-os.claude-mem'"
        else
          note "powershell.exe not found; from PowerShell run: powershell -ExecutionPolicy Bypass -File \"$win_ps1\" -PmOsRoot \"$win_root\" -Labels com.pm-os.claude-mem"
        fi
        ;;
      *)
        note "unknown OS; start the worker by hand: bash $ROOT/system/scripts/claude-mem-worker.sh"
        ;;
    esac
  fi
fi

# ---------------------------------------------------------------- 3. plugins
step "plugins"
if [ "$SKIP_PLUGINS" = 1 ]; then
  note "skipped (--skip-plugins)"
elif [ "$NESTED" = 1 ]; then
  note "skipped inside Claude Code; from a plain terminal run: bash \"$ROOT/system/scripts/install-claude-code.sh\""
else
  claude_capture plugin list
  installed="$CLAUDE_OUT"
  for p in "${PLUGINS[@]}"; do
    if [ "$CLAUDE_CLI_STUCK" = 1 ]; then note "$p skipped (claude CLI timed out)"; continue; fi
    if printf '%s' "$installed" | grep -q "$p@claude-plugins-official"; then
      note "$p already installed"
    else
      run claude_cli plugin install "$p@claude-plugins-official" || note "$p install failed (offline?); rerun later"
    fi
  done
  if [ "$MEMORY" = "claude-mem" ]; then
    if printf '%s' "$installed" | grep -q "claude-mem@thedotmack"; then
      note "claude-mem@thedotmack already installed"
    else
      note "claude-mem@thedotmack is installed by the claude-mem installer above; check with: claude plugin list"
    fi
  fi

  # The PM OS plugin: the marketplace folder lives in this repo, so the
  # install reads from disk and needs no network. Hooks run through Bun.
  MARKETPLACE_DIR="$ROOT/system/claude-marketplace"
  if [ ! -f "$MARKETPLACE_DIR/.claude-plugin/marketplace.json" ]; then
    note "PM OS plugin: $MARKETPLACE_DIR/.claude-plugin/marketplace.json missing, skipped"
  else
    claude_capture plugin marketplace list
    marketplaces="$CLAUDE_OUT"
    if printf '%s' "$marketplaces" | grep -q "$PM_OS_MARKETPLACE_NAME"; then
      note "marketplace $PM_OS_MARKETPLACE_NAME already added"
    else
      run claude_cli plugin marketplace add "$MARKETPLACE_DIR" --scope user || note "marketplace add failed; rerun: claude plugin marketplace add $MARKETPLACE_DIR"
    fi
    if printf '%s' "$installed" | grep -q "$PM_OS_PLUGIN"; then
      note "$PM_OS_PLUGIN already installed"
    else
      run claude_cli plugin install "$PM_OS_PLUGIN" --scope user || note "$PM_OS_PLUGIN install failed; rerun: claude plugin install $PM_OS_PLUGIN"
    fi
    if [ "$HAVE_BUN" = 0 ]; then
      note "$PM_OS_PLUGIN hooks need bun; until it is installed ($(bun_hint)) the hooks exit without acting"
    fi
    note "kill switch: $HOME_DIR/.claude/pm-os-plugin-config.json  {\"pm-os-bridge.disabled\": true}"
    # A reinstall restores the installed plugin's hooks.json, so apply the Windows hook profile again.
    if [ "$OS" = windows ] && [ -f "$ROOT/system/windows/apply-hook-profile.py" ]; then
      run "${PM_OS_PYTHON:-python3}" "$ROOT/system/windows/apply-hook-profile.py" || note "hook profile not applied; rerun: python3 system/windows/apply-hook-profile.py"
    fi
  fi
fi

# ---------------------------------------------------------------- 4. MCP servers
step "MCP servers (user scope)"
mcp_add() {
  # mcp_add NAME [-e K=V ...] -- CMD ARGS...   skip when the server already exists.
  local name="$1"; shift
  if [ "$CLAUDE_CLI_STUCK" = 1 ]; then note "$name skipped (claude CLI timed out)"; return; fi
  if claude_cli mcp get "$name" >/dev/null 2>&1; then note "$name already configured"; return; fi
  if [ "$CLAUDE_CLI_STUCK" = 1 ]; then note "$name skipped (claude CLI timed out)"; return; fi
  run claude_cli mcp add -s user "$name" "$@" || note "$name add failed; rerun later"
}
if [ "$SKIP_MCP" = 1 ]; then
  note "skipped (--skip-mcp)"
elif [ "$NESTED" = 1 ]; then
  note "skipped inside Claude Code; from a plain terminal run: bash \"$ROOT/system/scripts/install-claude-code.sh\""
else
  # A machine with PM_OS_OWNER_BROWSER in local/machines/<MACHINE>.env drives a real Brave
  # through owner_browser.py (rule: local/agents-local.md): chrome-devtools is the agent
  # profile, a separate Brave signed in to nothing that needs no click, and
  # chrome-devtools-owner is the owner's own signed-in Brave, through its Allow dialog,
  # for tasks that need the owner's sign-in. chrome-devtools-headless keeps the sanctioned
  # profile for scheduled jobs. Every other machine keeps chrome-devtools on the sanctioned
  # headless profile.
  OWNER_BROWSER_PY="$ROOT/system/scripts/owner_browser.py"
  command -v cygpath >/dev/null 2>&1 && OWNER_BROWSER_PY="$(cygpath -m "$OWNER_BROWSER_PY")"
  owner_browser="$(python3 "$OWNER_BROWSER_PY" mode 2>/dev/null || echo off)"
  # A chrome-devtools entry that does not match this machine's rule (for example
  # --browserUrl=http://127.0.0.1:9222, which attaches to whatever browser holds
  # that port) breaks the browser policy, so replace it.
  if [ "$owner_browser" = off ]; then cd_expected='chrome-devtools-mcp/chrome-profile'; else cd_expected='proxy --profile agent'; fi
  if [ "$CLAUDE_CLI_STUCK" != 1 ] && cd_spec="$(claude_cli mcp get chrome-devtools 2>/dev/null)" \
     && ! printf '%s' "$cd_spec" | grep -q -- "$cd_expected"; then
    note "chrome-devtools does not match this machine's browser rule ($owner_browser); replacing it"
    run claude_cli mcp remove -s user chrome-devtools || note "chrome-devtools remove failed; fix by hand: claude mcp remove -s user chrome-devtools"
  fi
  if [ "$owner_browser" != off ] && [ "$CLAUDE_CLI_STUCK" != 1 ] && cd_spec="$(claude_cli mcp get chrome-devtools-owner 2>/dev/null)" \
     && ! printf '%s' "$cd_spec" | grep -q -- 'proxy --profile owner'; then
    note "chrome-devtools-owner does not match this machine's browser rule; replacing it"
    run claude_cli mcp remove -s user chrome-devtools-owner || note "chrome-devtools-owner remove failed; fix by hand: claude mcp remove -s user chrome-devtools-owner"
  fi
  if [ "$owner_browser" = off ] && [ "$CLAUDE_CLI_STUCK" != 1 ]; then
    for leftover in chrome-devtools-headless chrome-devtools-owner; do
      if claude_cli mcp get "$leftover" >/dev/null 2>&1; then
        run claude_cli mcp remove -s user "$leftover" || note "$leftover remove failed; fix by hand: claude mcp remove -s user $leftover"
      fi
    done
  fi
  if command -v npx >/dev/null 2>&1; then
    if [ "$owner_browser" = off ]; then
      mcp_add chrome-devtools -e CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1 -- \
        npx --yes "chrome-devtools-mcp@$CHROME_DEVTOOLS_MCP_VERSION" --headless=true \
        "--userDataDir=$HOME_DIR/.cache/chrome-devtools-mcp/chrome-profile" --no-usage-statistics --no-performance-crux
    else
      mcp_add chrome-devtools -e CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1 -- \
        python3 "$OWNER_BROWSER_PY" proxy --profile agent --mcp-version "$CHROME_DEVTOOLS_MCP_VERSION"
      mcp_add chrome-devtools-owner -e CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1 -- \
        python3 "$OWNER_BROWSER_PY" proxy --profile owner --mcp-version "$CHROME_DEVTOOLS_MCP_VERSION"
      mcp_add chrome-devtools-headless -e CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1 -- \
        npx --yes "chrome-devtools-mcp@$CHROME_DEVTOOLS_MCP_VERSION" --headless=true \
        "--userDataDir=$HOME_DIR/.cache/chrome-devtools-mcp/chrome-profile" --no-usage-statistics --no-performance-crux
    fi
  else
    note "chrome-devtools: npx not on PATH, skipped (install Node.js)"
  fi
fi

# ---------------------------------------------------------------- 5. settings
step "settings -> $HOME_DIR/.claude/settings.json"
# PM OS runs its sessions with permission prompts bypassed; the plugin hooks are the
# guardrail. Undo: remove permissions.defaultMode and skipDangerousModePermissionPrompt.
# Deliberate default, not an oversight: bypassPermissions here and
# --dangerously-skip-permissions on worker tabs (dispatch.py launch_command_for) stay on,
# as an accepted risk.
if [ "$DRY" = 1 ]; then
  printf '  would  merge permissions.defaultMode=bypassPermissions, skipDangerousModePermissionPrompt=true\n'
else
  mkdir -p "$HOME_DIR/.claude"
  "${PYCMD[@]}" - "$HOME_DIR/.claude/settings.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8") if path.exists() else ""
try:
    data = json.loads(text) if text.strip() else {}
except json.JSONDecodeError:
    data = None
# A settings file that is not a JSON object is left alone with a note, never a crash.
if not isinstance(data, dict):
    print(f"  skip   {path} is not a JSON object; set permissions.defaultMode by hand")
    sys.exit(0)
perms = data.setdefault("permissions", {})
if not isinstance(perms, dict):   # someone's own shape: leave it, never overwrite it
    print(f"  skip   {path} has a permissions value that is not an object; set permissions.defaultMode by hand")
    sys.exit(0)
changed = perms.get("defaultMode") != "bypassPermissions" or data.get("skipDangerousModePermissionPrompt") is not True
perms["defaultMode"] = "bypassPermissions"
data["skipDangerousModePermissionPrompt"] = True
if changed:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"  {'write' if changed else 'keep '}  {path} (bypass permissions on)")
PY
fi
# Windows tabs that restart together after a crash or wake can spend over 30 s cold
# starting npx, which times out MCP connects.
# This lives in the user settings, not claude-tab-settings.json, because a resumed
# tab starts without --settings. check-windows-install.py reads the same value.
if [ "$OS" = windows ]; then
  if [ "$DRY" = 1 ]; then
    printf '  would  merge env.MCP_TIMEOUT=60000 unless already set\n'
  else
    mkdir -p "$HOME_DIR/.claude"
    "${PYCMD[@]}" - "$HOME_DIR/.claude/settings.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.read_text(encoding="utf-8").strip() else {}
env = data.setdefault("env", {})
changed = "MCP_TIMEOUT" not in env
env.setdefault("MCP_TIMEOUT", "60000")
if changed:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"  {'write' if changed else 'keep '}  {path} (MCP_TIMEOUT {env['MCP_TIMEOUT']})")
PY
  fi
fi
if [ "$MEMORY" = "builtin" ]; then
  note "left unchanged: auto-updater on, built-in memory on (--memory claude-mem applies the worker settings)"
elif [ "$DRY" = 1 ]; then
  printf '  would  merge env.DISABLE_AUTOUPDATER=1, env.CLAUDE_CODE_DISABLE_AUTO_MEMORY=1, skipDangerousModePermissionPrompt=true\n'
else
  mkdir -p "$HOME_DIR/.claude"
  "${PYCMD[@]}" - "$HOME_DIR/.claude/settings.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8") if path.exists() else ""
data = json.loads(text) if text.strip() else {}
env = data.setdefault("env", {})
want_env = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# Workers run with the permission prompt skipped; the PM OS plugin hooks are
# the guardrail (git add -A, force push, rm -rf outside /tmp are still blocked).
want_top = {"skipDangerousModePermissionPrompt": True}
changed = any(env.get(k) != v for k, v in want_env.items()) or any(data.get(k) != v for k, v in want_top.items())
env.update(want_env)
data.update(want_top)
if changed:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"  {'write' if changed else 'keep '}  {path}")
PY
fi

# ---------------------------------------------------------------- 6. folder trust
# A worker pane opens in a folder Claude Code has not seen; its trust prompt
# blocks the pane until someone answers it (on Windows the worktrees live under
# %LOCALAPPDATA%, outside the PM OS folder). Both
# folders are recorded; each task worktree sits under the second one.
step "folder trust -> $HOME_DIR/.claude.json"
WT_DIR="${PM_OS_WORKTREES_DIR:-$ROOT/.amp/worktrees}"
trust_paths=("$ROOT" "$WT_DIR")
if [ "$OS" = windows ] && command -v cygpath >/dev/null 2>&1; then
  # Drive forms. Which one Claude Code keys by on Windows is on the v7 verify
  # list, so both are written: C:/... and C:\...
  trust_paths=("$(cygpath -m "$ROOT")" "$(cygpath -w "$ROOT")" "$(cygpath -m "$WT_DIR")" "$(cygpath -w "$WT_DIR")")
fi
if [ "$FOLDER_TRUST" != 1 ]; then
  note "skipped (--no-folder-trust); answer the trust prompt once in each folder instead"
elif [ "$NESTED" = 1 ]; then
  # The running Claude Code rewrites ~/.claude.json all the time; a write from
  # inside it can drop its updates.
  note "skipped inside Claude Code; from a plain terminal run: bash \"$ROOT/system/scripts/install-claude-code.sh\""
elif [ "$DRY" = 1 ]; then
  printf '  would  set projects[<path>].hasTrustDialogAccepted=true for: %s\n' "${trust_paths[*]}"
else
  "${PYCMD[@]}" - "$HOME_DIR/.claude.json" "${trust_paths[@]}" <<'PY'
import json, os, pathlib, stat, sys, tempfile, time
path = pathlib.Path(sys.argv[1])
folders = []
for raw in sys.argv[2:]:
    for form in (raw.rstrip("/\\") or raw, os.path.realpath(raw) if os.name != "nt" and ":" not in raw else None):
        if form and form not in folders:
            folders.append(form)  # macOS: /tmp/x is looked up as /private/tmp/x


def load():
    """The current file as a dict, {} when absent, None when it is not a JSON object."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def trust(data):
    """Mark each folder trusted in data; return the folders that changed."""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = data["projects"] = {}
    added = []
    for folder in folders:
        entry = projects.get(folder)
        if not isinstance(entry, dict):
            entry = projects[folder] = {}
        if entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
            added.append(folder)
    return added


data = load()
if data is None:
    print(f"  skip   {path} is not a JSON object; answer the trust prompt by hand")
    sys.exit(0)
added = trust(data)
if added:
    # Claude Code keeps account state in this file: keep its mode (0600 when new),
    # use a unique temp file, and re-read just before the swap so a write by a
    # running Claude Code in between is kept.
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".pm-os-tmp", dir=str(path.parent))
    try:
        os.close(fd)
        os.chmod(tmp, mode)
        for attempt in range(50):  # Windows: a running Claude Code may hold the file open
            fresh = load()
            if fresh is None:
                print(f"  skip   {path} changed to something that is not a JSON object; answer the trust prompt by hand")
                sys.exit(0)
            trust(fresh)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(fresh, indent=2, ensure_ascii=False) + "\n")
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 49:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
print(f"  {'write' if added else 'keep '}  {path} (trusted: {', '.join(folders)})")
PY
fi

# ---------------------------------------------------------------- verify
step "verify"
if [ "$MEMORY" = "claude-mem" ]; then
  MEM_PLUGIN=" (+ claude-mem@thedotmack, ${PM_OS_PLUGIN})"
  MEM_CHECK="  ${PY} system/scripts/check-claude-mem.py --quiet    # worker health, telemetry off, autostart off"
else
  MEM_PLUGIN=" (+ ${PM_OS_PLUGIN}; no claude-mem, built-in memory kept)"
  MEM_CHECK="  # memory: Claude Code built-in, nothing to check"
fi
cat <<EOF
  claude plugin list                                  # ${PLUGINS[*]}${MEM_PLUGIN}
  claude mcp list                                     # chrome-devtools
  ${PY} system/scripts/install-claude-skills.py --check --skip-missing
${MEM_CHECK}
  claude -p "Which instruction files are loaded, and what bot attribution prefix do they require?"
  # PM OS plugin hooks (needs bun). Expect "permissionDecision": "deny":
  echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"git add -A"},"session_id":"t","cwd":"/tmp"}' \\
    | bun system/claude-marketplace/plugins/pm-os/hooks/amp-bridge.ts pre-tool-use
EOF
if [ "$MEMORY" = "claude-mem" ]; then
  case "$OS" in
    macos)   say "  launchctl print gui/\$(id -u)/com.pm-os.claude-mem | head -5" ;;
    linux)   say "  systemctl --user status pm-os-claude-mem.service" ;;
    windows) say "  Get-ScheduledTask -TaskPath '\\PM OS\\' -TaskName com.pm-os.claude-mem   (PowerShell)" ;;
  esac
fi
[ "$DRY" = 1 ] && say "" && say "Dry run only. Rerun without --dry-run to apply."
exit 0
