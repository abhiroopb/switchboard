#!/usr/bin/env bash
# mux.sh: pick the terminal multiplexer backend for PM OS scripts.
#
# Source this file; it exports PM_OS_MUX (cmux|wmux|none) and, when the
# backend is wmux, points CMUX_BIN / CMUX / CMUX_AMP_CMUX_BIN at the
# cmux-compatible shim so every script that speaks the cmux CLI keeps working.
# On macOS with cmux installed nothing changes: the variables keep their
# existing values or defaults, so the macOS path stays byte-for-byte.
#
#   PM_OS_MUX=cmux|wmux|none   force a backend (default: auto)
#   WMUX=1                     set by wmux inside its panes; selects wmux
#   CMUX_BIN                   honored as before when already set
#
# Portable helpers for the handful of macOS-only commands PM OS uses live here
# too (mux_copy, mux_open, mux_notify, mux_mtime, mux_size, mux_mode, mux_date_shift, mux_keep_awake) so
# callers can replace pbcopy/open/osascript/stat -f/date -v/caffeinate one line at a time.

_mux_repo_root() {
  git -C "${1:-$PWD}" rev-parse --show-toplevel 2>/dev/null || true
}

_mux_shim_path() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  echo "$here/../mux/cmux"
}

# Windows only: no forks, because every process started there leaks until reboot.
_mux_shim_path_nofork() {
  local here="${BASH_SOURCE[0]//\\//}"
  if [[ "$here" == */* ]]; then here="${here%/*}"; else here="."; fi
  [[ "$here" == /* || "$here" =~ ^[A-Za-z]: ]] || here="$PWD/$here"
  MUX_SHIM_PATH="$here/../mux/cmux"
}

mux_detect() {
  if [[ -n "${PM_OS_MUX:-}" && "${PM_OS_MUX}" != "auto" ]]; then
    echo "${PM_OS_MUX}"
    return
  fi
  if [[ "${WMUX:-}" == "1" || -n "${WMUX_PIPE:-}" || -n "${WMUX_CLI:-}" ]]; then
    echo wmux
    return
  fi
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      if [[ -n "${CMUX_BIN:-}" || -x /Applications/cmux.app/Contents/Resources/bin/cmux ]] || command -v cmux >/dev/null 2>&1; then
        echo cmux
      else
        echo none
      fi
      ;;
    Linux)
      # WSL2 with wmux on the Windows side, or a plain Linux box.
      if grep -qi microsoft /proc/version 2>/dev/null && { command -v wmux >/dev/null 2>&1 || command -v wmux.cmd >/dev/null 2>&1 || [[ -n "${WMUX_BIN:-}" ]]; }; then
        echo wmux
      else
        echo none
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*)
      echo wmux
      ;;
    *)
      echo none
      ;;
  esac
}

mux_init() {
  # A set backend needs no detection and no fork; pm-os-env.sh always sets it on Windows.
  if [[ "${PM_OS_MUX:-}" != "wmux" || -z "${MSYSTEM:-}" ]]; then
    PM_OS_MUX="$(mux_detect)"
  fi
  export PM_OS_MUX
  if [[ "${PM_OS_TEST_SANDBOX:-0}" == "1" ]]; then
    # Every backend, macOS included: cmux-helpers.sh would otherwise fall back to
    # /Applications/cmux.app and the guard to `cmux` on PATH. Only an explicit stub CMUX_BIN runs under the sandbox.
    export CMUX_BIN="${CMUX_BIN:-/nonexistent/pm-os-test-sandbox/cmux}"
    export CMUX="${CMUX:-$CMUX_BIN}"
  fi
  if [[ "$PM_OS_MUX" == "wmux" ]]; then
    local shim MUX_SHIM_PATH
    if [[ -n "${MSYSTEM:-}" ]]; then
      _mux_shim_path_nofork
      shim="$MUX_SHIM_PATH"
    else
      shim="$(_mux_shim_path)"
    fi
    if [[ "${PM_OS_TEST_SANDBOX:-0}" == "1" ]]; then
      # Test sandbox: a test can inherit WMUX=1 from the worker pane, and a stub
      # cmux that fails Git Bash's -x check must never be swapped for the real
      # shim, which would reach the live CoS. Under the sandbox the caller's CMUX_BIN stays, executable or not, and a
      # missing one names a binary that does not exist: fail closed.
      export CMUX_BIN="${CMUX_BIN:-/nonexistent/pm-os-test-sandbox/cmux}"
    elif [[ -z "${CMUX_BIN:-}" || ! -x "${CMUX_BIN}" ]]; then
      export CMUX_BIN="$shim"
    fi
    export CMUX="${CMUX:-$CMUX_BIN}"
    export CMUX_AMP_CMUX_BIN="${CMUX_AMP_CMUX_BIN:-$CMUX_BIN}"
    # wmux panes expose WMUX_SURFACE_ID only; derive the cmux-style ids once.
    if [[ -z "${CMUX_SURFACE_ID:-}" && -n "${WMUX_SURFACE_ID:-}" ]]; then
      export CMUX_SURFACE_ID="$WMUX_SURFACE_ID"
    fi
    # Only a pane has a surface to ask about. Without this guard every scheduled
    # task and hook that sourced this file paid a full shim call to get nothing.
    if [[ -z "${CMUX_WORKSPACE_ID:-}" && ( -z "${MSYSTEM:-}" || -n "${CMUX_SURFACE_ID:-}" ) ]]; then
      local wid
      wid="$("$CMUX_BIN" workspace-id-of-surface 2>/dev/null || true)"
      [[ -n "$wid" ]] && export CMUX_WORKSPACE_ID="$wid"
    fi
    export CC_CMUX_SESSION_JSON="${CC_CMUX_SESSION_JSON:-${PM_OS_CMUX_SESSION_JSON:-$HOME/.pm-os/wmux-session-store.json}}"
  fi
}

# -- portable command helpers ------------------------------------------------

mux_is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

# mux_copy: stdin -> clipboard (pbcopy | clip.exe | xclip | wl-copy)
mux_copy() {
  if command -v pbcopy >/dev/null 2>&1; then pbcopy
  elif command -v clip.exe >/dev/null 2>&1; then clip.exe
  elif command -v wl-copy >/dev/null 2>&1; then wl-copy
  elif command -v xclip >/dev/null 2>&1; then xclip -selection clipboard
  else cat >/dev/null; return 1; fi
}

# mux_open <path-or-url>: open -g | wslview | powershell Start-Process | xdg-open
# macOS opens in the background, never stealing focus; the others have no portable no-focus flag.
mux_open() {
  local target="$1"
  if command -v open >/dev/null 2>&1 && [[ "$(uname -s)" == "Darwin" ]]; then open -g "$target"
  elif command -v wslview >/dev/null 2>&1; then wslview "$target"
  elif command -v powershell.exe >/dev/null 2>&1; then powershell.exe -NoProfile -Command "Start-Process '$target'" >/dev/null
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$target"
  else return 1; fi
}

# mux_notify <title> <body>: osascript display notification | wmux notify | powershell toast
mux_notify() {
  local title="$1" body="${2:-}"
  if [[ "$(uname -s)" == "Darwin" ]] && command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${body//\"/\\\"}\" with title \"${title//\"/\\\"}\"" >/dev/null 2>&1 || true
  elif [[ "${PM_OS_MUX:-}" == "wmux" && -n "${CMUX_BIN:-}" ]]; then
    "$CMUX_BIN" notify --title "$title" --body "$body" >/dev/null 2>&1 || true
  elif command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms'); \$n = New-Object System.Windows.Forms.NotifyIcon; \$n.Icon = [System.Drawing.SystemIcons]::Information; \$n.Visible = \$true; \$n.ShowBalloonTip(5000, '${title//\'/\'\'}', '${body//\'/\'\'}', 'Info')" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "$title" "$body" || true
  fi
}

# mux_mtime, mux_size, mux_mode <path>: epoch seconds, bytes, octal permissions.
# The GNU form goes first on purpose. GNU `stat -f` means "report the filesystem":
# it prints a block of text to stdout before failing, so a BSD-first chain or a
# bare `$(stat -f %m f || echo 0)` captures that text and breaks the arithmetic
# that reads it. BSD stat rejects -c with nothing on stdout, so this order is
# clean on both. test_portable_stat.py keeps every other script off bare `stat -f`.
mux_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}
mux_size() {
  stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null
}
mux_mode() {
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null
}

# mux_date_shift <+N|-N><unit> [+format]: now shifted by N units (d, H, M, S), printed
# with `date` formatting. `date -v` is BSD-only and exits 1 on GNU date (Windows Git
# Bash, Linux), so its output used to come back empty there. macOS takes the -v branch.
mux_date_shift() {
  local spec="$1" word
  shift
  case "${spec: -1}" in
    d) word=day ;;
    H) word=hour ;;
    M) word=minute ;;
    S) word=second ;;
    *) echo "mux_date_shift: unit must be d, H, M, or S: $spec" >&2; return 2 ;;
  esac
  date -v"$spec" "$@" 2>/dev/null || date -d "${spec%?} $word" "$@"
}

# mux_keep_awake <cmd...>: caffeinate -i on macOS, plain exec elsewhere
mux_keep_awake() {
  if command -v caffeinate >/dev/null 2>&1; then caffeinate -i "$@"; else "$@"; fi
}
