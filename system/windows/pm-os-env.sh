#!/usr/bin/env bash
# pm-os-env.sh: shell environment for PM OS on Windows when wmux is the terminal.
#
# Works under Git for Windows bash (MSYS, the native default) and under WSL2
# (the explicit option). Source it from ~/.bashrc; install.ps1 appends the line:
#   source "/c/Dev/pm-os/system/windows/pm-os-env.sh"   (your PM_OS_ROOT in MSYS form)
#
# What it does, in order:
#   1. Reads ~/.pm-os/env (KEY=VALUE lines written by install.py) and exports
#      each key that the shell has not already set.
#   2. Picks the runtime (native from MSYSTEM, wsl from WSL_DISTRO_NAME) and the
#      wmux backend, converting Windows paths to POSIX form on MSYS.
#   3. Exports WMUX_BIN, PM_OS_PYTHON (python3, python, or py -3), AMP_EDITOR,
#      PM_OS_VAULT_ROOT, PM_OS_VAULT_NAME.
#   4. Sources system/scripts/lib/mux.sh and runs mux_init so CMUX_BIN points at
#      the shim.
#   5. Starts the optional PM OS board only when PM_OS_BOARD_AUTOSTART=1.
# Nothing here runs on macOS; mux.sh keeps the cmux path unchanged.

# No command substitution on the normal path of this file.
# A kernel leak on some Windows machines keeps every exited process until reboot, and on
# Git Bash each $(...) is a new process. Sourcing this file cost 52 processes, and every
# scheduled task, hook shell and pane sources it. test_windows_process_rate.py pins the count.
_pm_os_windows_dir="${BASH_SOURCE[0]//\\//}"
if [[ "$_pm_os_windows_dir" == */* ]]; then _pm_os_windows_dir="${_pm_os_windows_dir%/*}"; else _pm_os_windows_dir="."; fi
[[ "$_pm_os_windows_dir" == /* || "$_pm_os_windows_dir" =~ ^[A-Za-z]: ]] || _pm_os_windows_dir="$PWD/$_pm_os_windows_dir"

# -- 1. ~/.pm-os/env ---------------------------------------------------------
# install.py writes KEY=VALUE lines. A key already set in the shell wins, so a
# per-session override (PM_OS_RUNTIME=wsl bash) still works.
_pm_os_env_file="${PM_OS_ENV_FILE:-$HOME/.pm-os/env}"
if [[ -f "$_pm_os_env_file" ]]; then
  while IFS= read -r _pm_os_line || [[ -n "$_pm_os_line" ]]; do
    _pm_os_line="${_pm_os_line%$'\r'}"
    case "$_pm_os_line" in
      ''|'#'*) continue ;;
    esac
    [[ "$_pm_os_line" == *=* ]] || continue
    _pm_os_key="${_pm_os_line%%=*}"
    _pm_os_val="${_pm_os_line#*=}"
    # Strip one layer of surrounding quotes.
    if [[ "$_pm_os_val" == \"*\" && "$_pm_os_val" == *\" ]]; then _pm_os_val="${_pm_os_val:1:${#_pm_os_val}-2}"; fi
    if [[ "$_pm_os_val" == \'*\' && "$_pm_os_val" == *\' ]]; then _pm_os_val="${_pm_os_val:1:${#_pm_os_val}-2}"; fi
    [[ "$_pm_os_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # These switches live in local/machines/<MACHINE>.env (comms_triage_setting.py,
    # machine_role.py). A copy left here, say from an old backup, would be exported and beat
    # that file, so it is never read.
    case "$_pm_os_key" in
      PM_OS_COMMS_TRIAGE|PM_OS_MACHINE_ROLE|PM_OS_KEEP_SURFACES|PM_OS_LOADED_SURFACES) continue ;;
    esac
    if [[ -z "${!_pm_os_key:-}" ]]; then
      export "$_pm_os_key=$_pm_os_val"
    fi
  done < "$_pm_os_env_file"
  unset _pm_os_line _pm_os_key _pm_os_val
fi
unset _pm_os_env_file

# -- 2. runtime and path conversion -------------------------------------------
if [[ -z "${PM_OS_RUNTIME:-}" ]]; then
  if [[ -n "${MSYSTEM:-}" ]]; then
    PM_OS_RUNTIME=native
  elif [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    PM_OS_RUNTIME=wsl
  else
    PM_OS_RUNTIME=native
  fi
fi
export PM_OS_RUNTIME

# On MSYS a value such as C:\Dev\pm-os or C:/Dev/pm-os must become
# /c/Dev/pm-os before bash can cd into it. Anything else passes through.
# The result lands in _pm_os_posix, so MSYS needs no subshell and no cygpath. WSL mounts
# drives where its config says, so it still asks wslpath.
_pm_os_to_posix() {
  local value="$1"
  _pm_os_posix="$value"
  [[ "$value" =~ ^[A-Za-z]:[\\/] ]] || return 0
  if [[ "$PM_OS_RUNTIME" == "wsl" ]] && command -v wslpath >/dev/null 2>&1; then
    _pm_os_posix="$(wslpath -u "$value" 2>/dev/null)" && return 0
  fi
  local drive="${value:0:1}" rest="${value:2}"
  rest="${rest//\\//}"
  _pm_os_posix="/${drive,,}$rest"
}
for _pm_os_key in PM_OS_ROOT PM_OS_WORKTREES_DIR PM_OS_STATE_HOME PM_OS_VAULT_ROOT PM_OS_WMUX_SIDECAR PM_OS_CMUX_SESSION_JSON PM_OS_PYTHON PM_OS_RUBY WMUX_BIN PM_OS_PROFILE PM_OS_AGENT PMOS_AGENTS_AMP_FEEDBACK_OTLP_ENDPOINT; do
  if [[ -n "${!_pm_os_key:-}" ]]; then
    _pm_os_to_posix "${!_pm_os_key}"
    export "$_pm_os_key=$_pm_os_posix"
  fi
done
unset _pm_os_key

# Task Scheduler gives a task the PATH from sign-in, so tools installed since
# then (Python, Ruby, Git, ripgrep, Bun) are missing. install.ps1 records their
# folders in PM_OS_TOOL_PATH (";" separated) and this puts them first.
if [[ -n "${PM_OS_TOOL_PATH:-}" ]]; then
  _pm_os_tool_dirs=""
  while IFS= read -r -d ';' _pm_os_dir || [[ -n "$_pm_os_dir" ]]; do
    [[ -n "$_pm_os_dir" ]] || continue
    _pm_os_to_posix "$_pm_os_dir"
    _pm_os_dir="$_pm_os_posix"
    [[ -d "$_pm_os_dir" ]] || continue
    # Git\cmd\git.exe is a launcher: each git call costs it, a conhost and the real
    # git, three leaked processes in place of one. Use the real one when it is there.
    if [[ "$_pm_os_dir" == */[Cc]md ]]; then
      for _pm_os_sub in ucrt64 mingw64; do
        if [[ -x "${_pm_os_dir%/*}/$_pm_os_sub/bin/git.exe" ]]; then
          _pm_os_dir="${_pm_os_dir%/*}/$_pm_os_sub/bin"
          break
        fi
      done
      unset _pm_os_sub
    fi
    case ":$PATH:$_pm_os_tool_dirs:" in
      *":$_pm_os_dir:"*) ;;
      *) _pm_os_tool_dirs="${_pm_os_tool_dirs:+$_pm_os_tool_dirs:}$_pm_os_dir" ;;
    esac
  done <<< "${PM_OS_TOOL_PATH};"
  if [[ -n "$_pm_os_tool_dirs" ]]; then export PATH="$_pm_os_tool_dirs:$PATH"; fi
  hash -r
  unset _pm_os_tool_dirs _pm_os_dir
fi

PM_OS_ROOT="${PM_OS_ROOT:-$(cd "$_pm_os_windows_dir/../.." && pwd)}"
export PM_OS_ROOT
export PM_OS_STATE_HOME="${PM_OS_STATE_HOME:-$HOME/.pm-os}"
export PM_OS_MUX="${PM_OS_MUX:-wmux}"
export PM_OS_BOARD_AUTOSTART="${PM_OS_BOARD_AUTOSTART:-0}"

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
    _pm_os_win_home="${USERPROFILE:-$HOME}"
    _pm_os_scoop="${SCOOP:-$_pm_os_win_home/scoop}"
    for _pm_os_dir in \
      "${WMUX_HOME:-}" \
      "${LOCALAPPDATA:+$LOCALAPPDATA/Programs/wmux}" \
      "$_pm_os_scoop/apps/wmux/current" \
      "${ProgramFiles:+$ProgramFiles/wmux}" \
      "${ProgramW6432:+$ProgramW6432/wmux}"; do
      [[ -n "$_pm_os_dir" ]] || continue
      _pm_os_to_posix "$_pm_os_dir"
      _pm_os_dir="$_pm_os_posix"
      for _pm_os_name in wmux.cmd wmux; do
        if [[ -f "$_pm_os_dir/resources/cli-bin/$_pm_os_name" ]]; then
          export WMUX_BIN="$_pm_os_dir/resources/cli-bin/$_pm_os_name"
          break 2
        fi
      done
    done
    if [[ -z "${WMUX_BIN:-}" ]]; then
      for _pm_os_name in wmux.cmd wmux.exe wmux; do
        _pm_os_to_posix "$_pm_os_scoop"
        _pm_os_shim="$_pm_os_posix/shims/$_pm_os_name"
        if [[ -f "$_pm_os_shim" ]]; then export WMUX_BIN="$_pm_os_shim"; break; fi
      done
    fi
    unset _pm_os_win_home _pm_os_scoop _pm_os_dir _pm_os_name _pm_os_shim
  fi
fi

# Windows Python installs python.exe and py.exe, not python3. The repo's bash
# wrappers exec "${PM_OS_PYTHON:-python3}", so resolve PM_OS_PYTHON once here
# and give scripts that hard-code python3 an exported function.
# A recorded PM_OS_PYTHON (from ~/.pm-os/env) wins over a PATH lookup, but only
# while that file still exists and is not the Store stub.
if [[ -n "${PM_OS_PYTHON:-}" && ( ! -x "$PM_OS_PYTHON" || "$PM_OS_PYTHON" == *[Ww]indows[Aa]pps* ) ]]; then
  unset PM_OS_PYTHON
fi
if [[ -z "${PM_OS_PYTHON:-}" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 --version >/dev/null 2>&1; then
    PM_OS_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1 && python --version >/dev/null 2>&1; then
    PM_OS_PYTHON="$(command -v python)"
  elif command -v py >/dev/null 2>&1; then
    PM_OS_PYTHON="$(py -3 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
    if [[ -n "$PM_OS_PYTHON" ]] && command -v cygpath >/dev/null 2>&1; then
      PM_OS_PYTHON="$(cygpath -u "$PM_OS_PYTHON")"
    fi
  fi
  [[ -n "${PM_OS_PYTHON:-}" ]] && export PM_OS_PYTHON
fi
# The Windows Store alias stub python3.exe exists on PATH but only opens the Store, so a
# python3 under WindowsApps does not count. hash finds it with no process. A missing or
# stub python3 gets a function; PM_OS_PYTHON3_CHECK=run restores the `--version` probe.
_pm_os_python3_works() {
  if [[ "${PM_OS_PYTHON3_CHECK:-}" == "run" ]]; then
    python3 --version >/dev/null 2>&1
    return
  fi
  hash python3 2>/dev/null || return 1
  [[ "${BASH_CMDS[python3]:-}" != *[Ww]indows[Aa]pps* ]]
}
if [[ -n "${PM_OS_PYTHON:-}" && "$PM_OS_RUNTIME" == "native" ]] && ! _pm_os_python3_works; then
  python3() { "$PM_OS_PYTHON" "$@"; }
  export -f python3
fi
unset -f _pm_os_python3_works

# -- 3b. PATH: the cmux shim and the connectors shim --------------------------
# Git Bash does not resolve cmux.cmd or pmos.cmd by bare name, so put the bash
# launchers first. Repo scripts call `cmux` and `pmos` by name.
if [[ "$PM_OS_MUX" == "wmux" && -d "$PM_OS_ROOT/system/scripts/mux" ]]; then
  case ":$PATH:" in
    *":$PM_OS_ROOT/system/scripts/mux:"*) ;;
    *) export PATH="$PM_OS_ROOT/system/scripts/mux:$PATH" ;;
  esac
fi
# The connectors dir holds the pmos shim. It joins PATH only when no pmos exists at all,
# so another CLI with the same name is never shadowed. This only edits PATH; which
# one is in use is decided by system/connectors/company_cli.py.
if [[ -d "$PM_OS_ROOT/system/connectors" ]] && ! command -v pmos >/dev/null 2>&1; then
  case ":$PATH:" in
    *":$PM_OS_ROOT/system/connectors:"*) ;;
    *) export PATH="$PM_OS_ROOT/system/connectors:$PATH" ;;
  esac
fi

# Amp opens clicked file:// links through $AMP_EDITOR; the shim turns a vault
# note into obsidian://open?vault=pm-os&file=... and opens it on Windows.
if [[ -z "${AMP_EDITOR:-}" ]]; then
  if [[ "$PM_OS_RUNTIME" == "native" ]]; then
    _pm_os_editor="$_pm_os_windows_dir/amp-obsidian-editor.ps1"
    if [[ "$_pm_os_editor" =~ ^/([A-Za-z])/(.*)$ ]]; then
      _pm_os_editor="${BASH_REMATCH[1]^^}:/${BASH_REMATCH[2]}"
      _pm_os_editor="${_pm_os_editor//\//\\}"
    fi
    export AMP_EDITOR="powershell -NoProfile -ExecutionPolicy Bypass -File $_pm_os_editor"
    unset _pm_os_editor
  else
    export AMP_EDITOR="$PM_OS_ROOT/system/scripts/amp-obsidian-editor.sh"
  fi
fi
export PM_OS_VAULT_ROOT="${PM_OS_VAULT_ROOT:-$PM_OS_ROOT}"
export PM_OS_VAULT_NAME="${PM_OS_VAULT_NAME:-pm-os}"

# -- 4. backend selection -----------------------------------------------------
# shellcheck source=../scripts/lib/mux.sh
if [[ -f "$PM_OS_ROOT/system/scripts/lib/mux.sh" ]]; then
  source "$PM_OS_ROOT/system/scripts/lib/mux.sh"
  mux_init
fi

# -- 5. PM OS board (opt-in) --------------------------------------------------
# The board is a local web page that mirrors the cmux sidebar lanes. It is off
# by default because the sidebar must not load in a browser; set
# PM_OS_BOARD_AUTOSTART=1 in ~/.pm-os/env to start it once per login and open
# it with `pm-os-board.py open`. Worker shells never start it.
if [[ "${PM_OS_BOARD_AUTOSTART}" == "1" && -z "${PM_OS_BOARD_STARTED:-}" && -z "${PM_OS_WORKER_TASK_ID:-}" ]]; then
  _pm_os_py=""
  if command -v python3 >/dev/null 2>&1; then _pm_os_py=python3
  elif command -v python >/dev/null 2>&1; then _pm_os_py=python
  elif command -v py >/dev/null 2>&1; then _pm_os_py="py -3"
  fi
  if [[ -n "$_pm_os_py" ]]; then
    export PM_OS_BOARD_STARTED=1
    # shellcheck disable=SC2086
    ($_pm_os_py "$PM_OS_ROOT/system/scripts/mux/pm-os-board.py" ensure >/dev/null 2>&1 &)
  fi
  unset _pm_os_py
fi

unset -f _pm_os_to_posix
unset _pm_os_windows_dir _pm_os_posix
