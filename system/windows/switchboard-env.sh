#!/usr/bin/env bash
# switchboard-env.sh: shell environment for switchboard on Windows when wmux is the terminal.
#
# Works under Git for Windows bash (MSYS, the native default) and under WSL2
# (the explicit option). Source it from ~/.bashrc; install.ps1 appends the line:
#   source "/c/Dev/switchboard/system/windows/switchboard-env.sh"   (your SWITCHBOARD_ROOT in MSYS form)
#
# What it does, in order:
#   1. Reads ~/.switchboard/env (KEY=VALUE lines written by install.py) and exports
#      each key that the shell has not already set.
#   2. Picks the runtime (native from MSYSTEM, wsl from WSL_DISTRO_NAME) and the
#      wmux backend, converting Windows paths to POSIX form on MSYS.
#   3. Exports WMUX_BIN, SWITCHBOARD_PYTHON (python3, python, or py -3), AMP_EDITOR,
#      SWITCHBOARD_VAULT_ROOT, SWITCHBOARD_VAULT_NAME.
#   4. Sources system/scripts/lib/mux.sh and runs mux_init so CMUX_BIN points at
#      the shim.
#   5. Starts the optional switchboard board only when SWITCHBOARD_BOARD_AUTOSTART=1.
# Nothing here runs on macOS; mux.sh keeps the cmux path unchanged.

# No command substitution on the normal path of this file.
# A kernel leak on some Windows machines keeps every exited process until reboot, and on
# Git Bash each $(...) is a new process. Sourcing this file cost 52 processes, and every
# scheduled task, hook shell and pane sources it. test_windows_process_rate.py pins the count.
_switchboard_windows_dir="${BASH_SOURCE[0]//\\//}"
if [[ "$_switchboard_windows_dir" == */* ]]; then _switchboard_windows_dir="${_switchboard_windows_dir%/*}"; else _switchboard_windows_dir="."; fi
[[ "$_switchboard_windows_dir" == /* || "$_switchboard_windows_dir" =~ ^[A-Za-z]: ]] || _switchboard_windows_dir="$PWD/$_switchboard_windows_dir"

# -- 1. ~/.switchboard/env ---------------------------------------------------------
# install.py writes KEY=VALUE lines. A key already set in the shell wins, so a
# per-session override (SWITCHBOARD_RUNTIME=wsl bash) still works.
_switchboard_env_file="${SWITCHBOARD_ENV_FILE:-$HOME/.switchboard/env}"
if [[ -f "$_switchboard_env_file" ]]; then
  while IFS= read -r _switchboard_line || [[ -n "$_switchboard_line" ]]; do
    _switchboard_line="${_switchboard_line%$'\r'}"
    case "$_switchboard_line" in
      ''|'#'*) continue ;;
    esac
    [[ "$_switchboard_line" == *=* ]] || continue
    _switchboard_key="${_switchboard_line%%=*}"
    _switchboard_val="${_switchboard_line#*=}"
    # Strip one layer of surrounding quotes.
    if [[ "$_switchboard_val" == \"*\" && "$_switchboard_val" == *\" ]]; then _switchboard_val="${_switchboard_val:1:${#_switchboard_val}-2}"; fi
    if [[ "$_switchboard_val" == \'*\' && "$_switchboard_val" == *\' ]]; then _switchboard_val="${_switchboard_val:1:${#_switchboard_val}-2}"; fi
    [[ "$_switchboard_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # These switches live in local/machines/<MACHINE>.env (comms_triage_setting.py,
    # machine_role.py). A copy left here, say from an old backup, would be exported and beat
    # that file, so it is never read.
    case "$_switchboard_key" in
      SWITCHBOARD_COMMS_TRIAGE|SWITCHBOARD_MACHINE_ROLE|SWITCHBOARD_KEEP_SURFACES|SWITCHBOARD_LOADED_SURFACES) continue ;;
    esac
    if [[ -z "${!_switchboard_key:-}" ]]; then
      export "$_switchboard_key=$_switchboard_val"
    fi
  done < "$_switchboard_env_file"
  unset _switchboard_line _switchboard_key _switchboard_val
fi
unset _switchboard_env_file

# -- 2. runtime and path conversion -------------------------------------------
if [[ -z "${SWITCHBOARD_RUNTIME:-}" ]]; then
  if [[ -n "${MSYSTEM:-}" ]]; then
    SWITCHBOARD_RUNTIME=native
  elif [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    SWITCHBOARD_RUNTIME=wsl
  else
    SWITCHBOARD_RUNTIME=native
  fi
fi
export SWITCHBOARD_RUNTIME

# On MSYS a value such as C:\Dev\switchboard or C:/Dev/switchboard must become
# /c/Dev/switchboard before bash can cd into it. Anything else passes through.
# The result lands in _switchboard_posix, so MSYS needs no subshell and no cygpath. WSL mounts
# drives where its config says, so it still asks wslpath.
_switchboard_to_posix() {
  local value="$1"
  _switchboard_posix="$value"
  [[ "$value" =~ ^[A-Za-z]:[\\/] ]] || return 0
  if [[ "$SWITCHBOARD_RUNTIME" == "wsl" ]] && command -v wslpath >/dev/null 2>&1; then
    _switchboard_posix="$(wslpath -u "$value" 2>/dev/null)" && return 0
  fi
  local drive="${value:0:1}" rest="${value:2}"
  rest="${rest//\\//}"
  _switchboard_posix="/${drive,,}$rest"
}
for _switchboard_key in SWITCHBOARD_ROOT SWITCHBOARD_WORKTREES_DIR SWITCHBOARD_STATE_HOME SWITCHBOARD_VAULT_ROOT SWITCHBOARD_WMUX_SIDECAR SWITCHBOARD_CMUX_SESSION_JSON SWITCHBOARD_PYTHON SWITCHBOARD_RUBY WMUX_BIN SWITCHBOARD_PROFILE SWITCHBOARD_AGENT SWITCHBOARD_AGENTS_AMP_FEEDBACK_OTLP_ENDPOINT; do
  if [[ -n "${!_switchboard_key:-}" ]]; then
    _switchboard_to_posix "${!_switchboard_key}"
    export "$_switchboard_key=$_switchboard_posix"
  fi
done
unset _switchboard_key

# Task Scheduler gives a task the PATH from sign-in, so tools installed since
# then (Python, Ruby, Git, ripgrep, Bun) are missing. install.ps1 records their
# folders in SWITCHBOARD_TOOL_PATH (";" separated) and this puts them first.
if [[ -n "${SWITCHBOARD_TOOL_PATH:-}" ]]; then
  _switchboard_tool_dirs=""
  while IFS= read -r -d ';' _switchboard_dir || [[ -n "$_switchboard_dir" ]]; do
    [[ -n "$_switchboard_dir" ]] || continue
    _switchboard_to_posix "$_switchboard_dir"
    _switchboard_dir="$_switchboard_posix"
    [[ -d "$_switchboard_dir" ]] || continue
    # Git\cmd\git.exe is a launcher: each git call costs it, a conhost and the real
    # git, three leaked processes in place of one. Use the real one when it is there.
    if [[ "$_switchboard_dir" == */[Cc]md ]]; then
      for _switchboard_sub in ucrt64 mingw64; do
        if [[ -x "${_switchboard_dir%/*}/$_switchboard_sub/bin/git.exe" ]]; then
          _switchboard_dir="${_switchboard_dir%/*}/$_switchboard_sub/bin"
          break
        fi
      done
      unset _switchboard_sub
    fi
    case ":$PATH:$_switchboard_tool_dirs:" in
      *":$_switchboard_dir:"*) ;;
      *) _switchboard_tool_dirs="${_switchboard_tool_dirs:+$_switchboard_tool_dirs:}$_switchboard_dir" ;;
    esac
  done <<< "${SWITCHBOARD_TOOL_PATH};"
  if [[ -n "$_switchboard_tool_dirs" ]]; then export PATH="$_switchboard_tool_dirs:$PATH"; fi
  hash -r
  unset _switchboard_tool_dirs _switchboard_dir
fi

SWITCHBOARD_ROOT="${SWITCHBOARD_ROOT:-$(cd "$_switchboard_windows_dir/../.." && pwd)}"
export SWITCHBOARD_ROOT
export SWITCHBOARD_STATE_HOME="${SWITCHBOARD_STATE_HOME:-$HOME/.switchboard}"
export SWITCHBOARD_MUX="${SWITCHBOARD_MUX:-wmux}"
export SWITCHBOARD_BOARD_AUTOSTART="${SWITCHBOARD_BOARD_AUTOSTART:-0}"

# -- 3. wmux binary, editor shim, vault ---------------------------------------
# On MSYS `wmux` usually resolves to wmux.cmd through PATHEXT; bash needs the
# full name. wmux puts its CLI on PATH only in terminals it started, so a shell
# from Task Scheduler or another terminal also looks in the known install folders
# (the same order as system/scripts/lib/mux.py wmux_install_dirs): WMUX_HOME,
# %LOCALAPPDATA%\Programs\wmux, scoop, %ProgramFiles%\wmux. Under WSL the
# Windows PATH is appended through interop.
if [[ -z "${WMUX_BIN:-}" ]] && ! command -v wmux >/dev/null 2>&1; then
  if command -v wmux.cmd >/dev/null 2>&1; then
    export WMUX_BIN="wmux.cmd"
  elif command -v wmux.exe >/dev/null 2>&1; then
    export WMUX_BIN="wmux.exe"
  else
    _switchboard_win_home="${USERPROFILE:-$HOME}"
    _switchboard_scoop="${SCOOP:-$_switchboard_win_home/scoop}"
    for _switchboard_dir in \
      "${WMUX_HOME:-}" \
      "${LOCALAPPDATA:+$LOCALAPPDATA/Programs/wmux}" \
      "$_switchboard_scoop/apps/wmux/current" \
      "${ProgramFiles:+$ProgramFiles/wmux}" \
      "${ProgramW6432:+$ProgramW6432/wmux}"; do
      [[ -n "$_switchboard_dir" ]] || continue
      _switchboard_to_posix "$_switchboard_dir"
      _switchboard_dir="$_switchboard_posix"
      for _switchboard_name in wmux.cmd wmux; do
        if [[ -f "$_switchboard_dir/resources/cli-bin/$_switchboard_name" ]]; then
          export WMUX_BIN="$_switchboard_dir/resources/cli-bin/$_switchboard_name"
          break 2
        fi
      done
    done
    if [[ -z "${WMUX_BIN:-}" ]]; then
      for _switchboard_name in wmux.cmd wmux.exe wmux; do
        _switchboard_to_posix "$_switchboard_scoop"
        _switchboard_shim="$_switchboard_posix/shims/$_switchboard_name"
        if [[ -f "$_switchboard_shim" ]]; then export WMUX_BIN="$_switchboard_shim"; break; fi
      done
    fi
    unset _switchboard_win_home _switchboard_scoop _switchboard_dir _switchboard_name _switchboard_shim
  fi
fi

# Windows Python installs python.exe and py.exe, not python3. The repo's bash
# wrappers exec "${SWITCHBOARD_PYTHON:-python3}", so resolve SWITCHBOARD_PYTHON once here
# and give scripts that hard-code python3 an exported function.
# A recorded SWITCHBOARD_PYTHON (from ~/.switchboard/env) wins over a PATH lookup, but only
# while that file still exists and is not the Store stub.
if [[ -n "${SWITCHBOARD_PYTHON:-}" && ( ! -x "$SWITCHBOARD_PYTHON" || "$SWITCHBOARD_PYTHON" == *[Ww]indows[Aa]pps* ) ]]; then
  unset SWITCHBOARD_PYTHON
fi
if [[ -z "${SWITCHBOARD_PYTHON:-}" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 --version >/dev/null 2>&1; then
    SWITCHBOARD_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1 && python --version >/dev/null 2>&1; then
    SWITCHBOARD_PYTHON="$(command -v python)"
  elif command -v py >/dev/null 2>&1; then
    SWITCHBOARD_PYTHON="$(py -3 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
    if [[ -n "$SWITCHBOARD_PYTHON" ]] && command -v cygpath >/dev/null 2>&1; then
      SWITCHBOARD_PYTHON="$(cygpath -u "$SWITCHBOARD_PYTHON")"
    fi
  fi
  [[ -n "${SWITCHBOARD_PYTHON:-}" ]] && export SWITCHBOARD_PYTHON
fi
# The Windows Store alias stub python3.exe exists on PATH but only opens the Store, so a
# python3 under WindowsApps does not count. hash finds it with no process. A missing or
# stub python3 gets a function; SWITCHBOARD_PYTHON3_CHECK=run restores the `--version` probe.
_switchboard_python3_works() {
  if [[ "${SWITCHBOARD_PYTHON3_CHECK:-}" == "run" ]]; then
    python3 --version >/dev/null 2>&1
    return
  fi
  hash python3 2>/dev/null || return 1
  [[ "${BASH_CMDS[python3]:-}" != *[Ww]indows[Aa]pps* ]]
}
if [[ -n "${SWITCHBOARD_PYTHON:-}" && "$SWITCHBOARD_RUNTIME" == "native" ]] && ! _switchboard_python3_works; then
  python3() { "$SWITCHBOARD_PYTHON" "$@"; }
  export -f python3
fi
unset -f _switchboard_python3_works

# -- 3b. PATH: the cmux shim and the connectors shim --------------------------
# Git Bash does not resolve cmux.cmd or switchboard.cmd by bare name, so put the bash
# launchers first. Repo scripts call `cmux` and `switchboard` by name.
if [[ "$SWITCHBOARD_MUX" == "wmux" && -d "$SWITCHBOARD_ROOT/system/scripts/mux" ]]; then
  case ":$PATH:" in
    *":$SWITCHBOARD_ROOT/system/scripts/mux:"*) ;;
    *) export PATH="$SWITCHBOARD_ROOT/system/scripts/mux:$PATH" ;;
  esac
fi
# The connectors dir holds the switchboard shim. It joins PATH only when no switchboard exists at all,
# so another CLI with the same name is never shadowed. This only edits PATH; which
# one is in use is decided by system/connectors/company_cli.py.
if [[ -d "$SWITCHBOARD_ROOT/system/connectors" ]] && ! command -v switchboard >/dev/null 2>&1; then
  case ":$PATH:" in
    *":$SWITCHBOARD_ROOT/system/connectors:"*) ;;
    *) export PATH="$SWITCHBOARD_ROOT/system/connectors:$PATH" ;;
  esac
fi

# Amp opens clicked file:// links through $AMP_EDITOR; the shim turns a vault
# note into obsidian://open?vault=switchboard&file=... and opens it on Windows.
if [[ -z "${AMP_EDITOR:-}" ]]; then
  if [[ "$SWITCHBOARD_RUNTIME" == "native" ]]; then
    _switchboard_editor="$_switchboard_windows_dir/amp-obsidian-editor.ps1"
    if [[ "$_switchboard_editor" =~ ^/([A-Za-z])/(.*)$ ]]; then
      _switchboard_editor="${BASH_REMATCH[1]^^}:/${BASH_REMATCH[2]}"
      _switchboard_editor="${_switchboard_editor//\//\\}"
    fi
    export AMP_EDITOR="powershell -NoProfile -ExecutionPolicy Bypass -File $_switchboard_editor"
    unset _switchboard_editor
  else
    export AMP_EDITOR="$SWITCHBOARD_ROOT/system/scripts/amp-obsidian-editor.sh"
  fi
fi
export SWITCHBOARD_VAULT_ROOT="${SWITCHBOARD_VAULT_ROOT:-$SWITCHBOARD_ROOT}"
export SWITCHBOARD_VAULT_NAME="${SWITCHBOARD_VAULT_NAME:-switchboard}"

# -- 4. backend selection -----------------------------------------------------
# shellcheck source=../scripts/lib/mux.sh
if [[ -f "$SWITCHBOARD_ROOT/system/scripts/lib/mux.sh" ]]; then
  source "$SWITCHBOARD_ROOT/system/scripts/lib/mux.sh"
  mux_init
fi

# -- 5. switchboard board (opt-in) --------------------------------------------------
# The board is a local web page that mirrors the cmux sidebar lanes. It is off
# by default because the sidebar must not load in a browser; set
# SWITCHBOARD_BOARD_AUTOSTART=1 in ~/.switchboard/env to start it once per login and open
# it with `switchboard-board.py open`. Worker shells never start it.
if [[ "${SWITCHBOARD_BOARD_AUTOSTART}" == "1" && -z "${SWITCHBOARD_BOARD_STARTED:-}" && -z "${SWITCHBOARD_WORKER_TASK_ID:-}" ]]; then
  _switchboard_py=""
  if command -v python3 >/dev/null 2>&1; then _switchboard_py=python3
  elif command -v python >/dev/null 2>&1; then _switchboard_py=python
  elif command -v py >/dev/null 2>&1; then _switchboard_py="py -3"
  fi
  if [[ -n "$_switchboard_py" ]]; then
    export SWITCHBOARD_BOARD_STARTED=1
    # shellcheck disable=SC2086
    ($_switchboard_py "$SWITCHBOARD_ROOT/system/scripts/mux/switchboard-board.py" ensure >/dev/null 2>&1 &)
  fi
  unset _switchboard_py
fi

unset -f _switchboard_to_posix
unset _switchboard_windows_dir _switchboard_posix
