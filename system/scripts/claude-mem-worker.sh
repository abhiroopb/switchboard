#!/usr/bin/env bash
#
# claude-mem-worker.sh: run the claude-mem worker in the foreground for launchd.
#
# claude-mem (https://github.com/thedotmack/claude-mem) is the Claude Code
# memory plugin that replaces amp-mem in the Amp to Claude migration
# (workstreams/switchboard-claude-migration). Its own `npx claude-mem start` forks a
# detached daemon and exits, which fights launchd KeepAlive, so this wrapper
# resolves the installed plugin root and execs the worker script directly with
# `--daemon` (the foreground entry point in worker-service.cjs). The worker is
# managed here only: ~/.claude-mem/settings.json sets
# CLAUDE_MEM_WORKER_AUTOSTART=false so hooks and the installer never spawn,
# stop, or recycle it.
#
# Plugin root resolution: the version Claude Code has installed (installPath in
# ~/.claude/plugins/installed_plugins.json, so hooks and worker run the same
# build), then the newest versioned cache dir that holds
# scripts/worker-service.cjs, then the marketplace checkout. Override with
# CLAUDE_MEM_WORKER_SCRIPT_PATH.
#
# The marketplace auto-updater extracts a new version without running
# `bun install`, so hooks can print "plugin dependencies are missing" while the
# worker keeps serving the old version. Before
# resolving, this wrapper runs the plugin's own scripts/version-check.js on the
# installed root, which installs the missing dependencies idempotently.
#
# Usage:
#   bash system/scripts/claude-mem-worker.sh            # foreground (launchd)
#   bash system/scripts/claude-mem-worker.sh --resolve  # print the script path
#
# launchd: system/launchd/com.switchboard.claude-mem.plist (installed by
# install-switchboard-bin.sh). Kill switch: `launchctl bootout gui/$(id -u)/com.switchboard.claude-mem`
# plus `claude plugin disable claude-mem@thedotmack`.
set -euo pipefail

CACHE_ROOT="$HOME/.claude/plugins/cache/thedotmack/claude-mem"
MARKETPLACE_PLUGIN="$HOME/.claude/plugins/marketplaces/thedotmack/plugin"
INSTALLED_PLUGINS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/installed_plugins.json"

installed_plugin_root() {
  [[ -f "$INSTALLED_PLUGINS" ]] || return 1
  python3 - "$INSTALLED_PLUGINS" <<'EOF'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
entries = (data.get("plugins", data) or {}).get("claude-mem@thedotmack") or []
for entry in entries:
    path = entry.get("installPath")
    if path:
        print(path)
        sys.exit(0)
sys.exit(1)
EOF
}

# Run the plugin's Setup-time dependency installer when the root has no
# node_modules. version-check.js only runs `bun install --production` inside
# the root and writes .install-version; it never touches the worker.
ensure_plugin_dependencies() {
  local root="$1"
  [[ -f "$root/scripts/version-check.js" && -f "$root/package.json" ]] || return 0
  [[ -d "$root/node_modules" ]] && return 0
  command -v node >/dev/null 2>&1 || {
    echo "claude-mem-worker: node not found; cannot install dependencies for $root" >&2
    return 0
  }
  echo "claude-mem-worker: installing plugin dependencies for $root" >&2
  CLAUDE_PLUGIN_ROOT="$root" node "$root/scripts/version-check.js" || true
}

resolve_worker_script() {
  if [[ -n "${CLAUDE_MEM_WORKER_SCRIPT_PATH:-}" && -f "$CLAUDE_MEM_WORKER_SCRIPT_PATH" ]]; then
    printf '%s\n' "$CLAUDE_MEM_WORKER_SCRIPT_PATH"
    return 0
  fi
  local installed
  if installed="$(installed_plugin_root)" && [[ -f "$installed/scripts/worker-service.cjs" ]]; then
    ensure_plugin_dependencies "$installed"
    if [[ -d "$installed/node_modules" ]]; then
      printf '%s\n' "$installed/scripts/worker-service.cjs"
      return 0
    fi
    echo "claude-mem-worker: installed plugin $installed has no node_modules; falling back to the newest cache dir with dependencies" >&2
  fi
  if [[ -d "$CACHE_ROOT" ]]; then
    # Highest semantic version first; `sort -V` orders 15.10.0 after 15.9.0.
    local candidate
    while IFS= read -r candidate; do
      if [[ -f "$CACHE_ROOT/$candidate/scripts/worker-service.cjs" && -d "$CACHE_ROOT/$candidate/node_modules" ]]; then
        printf '%s\n' "$CACHE_ROOT/$candidate/scripts/worker-service.cjs"
        return 0
      fi
    done < <(ls -1 "$CACHE_ROOT" | sort -rV)
  fi
  if [[ -f "$MARKETPLACE_PLUGIN/scripts/worker-service.cjs" ]]; then
    printf '%s\n' "$MARKETPLACE_PLUGIN/scripts/worker-service.cjs"
    return 0
  fi
  return 1
}

script="$(resolve_worker_script)" || {
  echo "claude-mem-worker: no installed worker script found (run: npx claude-mem install)" >&2
  exit 1
}

if [[ "${1:-}" == "--resolve" ]]; then
  printf '%s\n' "$script"
  exit 0
fi

BUN="${BUN_PATH:-$(command -v bun || true)}"
if [[ -z "$BUN" ]]; then
  for candidate in /opt/homebrew/bin/bun "$HOME/.bun/bin/bun" /usr/local/bin/bun; do
    [[ -x "$candidate" ]] && BUN="$candidate" && break
  done
fi
if [[ -z "$BUN" ]]; then
  echo "claude-mem-worker: bun not found" >&2
  exit 1
fi

# The worker reads ~/.claude-mem/.env and settings.json itself; telemetry is
# off there (telemetry.json) and DO_NOT_TRACK is a second lock.
export DO_NOT_TRACK=1
cd "$(dirname "$(dirname "$script")")"
exec "$BUN" "$script" --daemon
