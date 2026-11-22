#!/usr/bin/env bash
# Shell-native lock for Git Bash on Windows, where `flock(9)` from Python cannot work:
# native Python does not inherit bash's fd 9, and an msvcrt lock ends with the process
# that took it. mkdir is atomic. A lock whose recorded owner is gone is reclaimed.
pm_os_is_windows_shell() {
  case "${OSTYPE:-}" in msys*|cygwin*) return 0 ;; *) return 1 ;; esac
}

pm_os_dir_lock_acquire() {  # <lock dir> [wait seconds]
  local dir="$1" wait="${2:-0}" start="$SECONDS" owner
  mkdir -p "$(dirname "$dir")" 2>/dev/null || true
  while :; do
    if mkdir "$dir" 2>/dev/null; then
      # BASHPID names this subshell, not the parent: a killed `( ... )` caller leaves
      # a dead owner that the next caller can reclaim. bash 3.2 (macOS) lacks it.
      printf '%s' "${BASHPID:-$$}" > "$dir/pid"
      PM_OS_DIR_LOCK_HELD="$dir"
      return 0
    fi
    owner="$(cat "$dir/pid" 2>/dev/null || true)"
    if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
      # One reclaimer at a time, and it rechecks the owner under the guard. Two waiters
      # may both see the dead owner; the second must not delete a lock just retaken, and
      # moving the lock aside is no fix, because a live lock moved in that gap gives two holders.
      # A live reclaimer returns 1, and then the wait limit below still applies.
      pm_os_dir_lock_reclaim "$dir" "$owner" && continue
    fi
    (( SECONDS - start >= wait )) && return 1
    sleep 0.25
  done
}

pm_os_dir_lock_reclaim() {  # <lock dir> <dead owner pid>
  local dir="$1" owner="$2" guard="$1.reclaim" holder
  if ! mkdir "$guard" 2>/dev/null; then
    holder="$(cat "$guard/pid" 2>/dev/null || true)"
    # A reclaimer killed inside the guard must not block every later waiter.
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
      rm -rf "$guard"
      return 0
    fi
    return 1
  fi
  printf '%s' "${BASHPID:-$$}" > "$guard/pid"
  if [[ "$(cat "$dir/pid" 2>/dev/null || true)" == "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
    rm -rf "$dir"
  fi
  rm -rf "$guard"
}

pm_os_dir_lock_release() {
  if [[ -n "${PM_OS_DIR_LOCK_HELD:-}" ]]; then
    rm -rf "$PM_OS_DIR_LOCK_HELD"
    unset PM_OS_DIR_LOCK_HELD
  fi
}
