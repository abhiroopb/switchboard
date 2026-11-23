#!/usr/bin/env bash
# test-install-switchboard-bin.sh — focused tests for install-switchboard-bin.sh:
#
#   * a runtime file the installer never wrote is skipped with a warning and a
#     nonzero exit, never clobbered (same on macOS and Windows).
#   * a plain copy the installer wrote (Windows Git Bash `ln -s` makes copies)
#     is refreshed when the tracked source changes, and a converged copy is
#     quiet on the next run.
#   * launchd plists are copied on macOS only; elsewhere the block is skipped
#     (~/Library/LaunchAgents does not exist off macOS).
#
# Hermetic: temp fixture repo and temp HOME, `uname` shimmed through PATH.
#
# Run: bash system/scripts/test-install-switchboard-bin.sh

set -uo pipefail

REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/install-bin-test.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT

REPO="$FIXTURE/repo"
mkdir -p "$REPO/system/scripts" "$REPO/system/bin" "$REPO/system/launchd" "$FIXTURE/shim"
cp "$REAL_REPO/system/scripts/install-switchboard-bin.sh" "$REPO/system/scripts/"
printf '#!/bin/sh\necho tool-v1\n' > "$REPO/system/bin/tool"
printf '#!/bin/sh\necho mine\n' > "$REPO/system/bin/owned"
printf '<plist/>\n' > "$REPO/system/launchd/com.example.test.plist"

pass=0
fail=0
check() {
    if eval "$2"; then pass=$((pass + 1)); else echo "FAIL: $1"; fail=$((fail + 1)); fi
}

run_install() {  # $1 = HOME, $2 = uname output
    printf '#!/bin/sh\necho %s\n' "$2" > "$FIXTURE/shim/uname"
    chmod +x "$FIXTURE/shim/uname"
    HOME="$1" PATH="$FIXTURE/shim:$PATH" bash "$REPO/system/scripts/install-switchboard-bin.sh" 2>&1
}

# --- non-macOS: foreign file skipped, launchd skipped -------------------------
H1="$FIXTURE/home1"; mkdir -p "$H1/bin"
echo "someone else's tool" > "$H1/bin/owned"
out=$(run_install "$H1" MINGW64_NT-10.0); rc=$?
check "foreign runtime file makes the run exit nonzero" '[[ $rc -ne 0 ]]'
check "foreign runtime file is left untouched" 'grep -q "someone else" "$H1/bin/owned"'
check "foreign runtime file is warned about" 'grep -q "runtime owned differs" <<<"$out"'
check "tracked tool installed" 'cmp -s "$REPO/system/bin/tool" "$H1/bin/tool"'
check "no launchd copy off macOS" '[[ ! -e "$H1/Library/LaunchAgents/com.example.test.plist" ]]'
check "no cp error off macOS" '! grep -q "cannot create" <<<"$out"'

# --- installed copy: quiet when converged, refreshed when stale ---------------
H2="$FIXTURE/home2"; mkdir -p "$H2/bin"
run_install "$H2" Linux >/dev/null
# Force the plain-copy shape Windows Git Bash produces, whatever this OS does.
rm -f "$H2/bin/tool"; cp "$REPO/system/bin/tool" "$H2/bin/tool"
h=$(cksum < "$H2/bin/tool" | awk '{print $1 "-" $2}')
{ grep -vF "$H2/bin/tool " "$H2/bin/.switchboard-installed" 2>/dev/null || true; echo "$H2/bin/tool $h"; } > "$H2/m" && mv "$H2/m" "$H2/bin/.switchboard-installed"
out=$(run_install "$H2" Linux); rc=$?
check "converged copy run exits 0" '[[ $rc -eq 0 ]]'
check "converged copy run is quiet" '[[ -z "$out" ]]'
printf '#!/bin/sh\necho tool-v2\n' > "$REPO/system/bin/tool"
out=$(run_install "$H2" Linux); rc=$?
check "stale installed copy run exits 0" '[[ $rc -eq 0 ]]'
check "stale installed copy refreshed" 'grep -q tool-v2 "$H2/bin/tool"'
# A hand edit to an installed copy no longer matches the manifest: skipped.
if [[ ! -L "$H2/bin/tool" ]]; then
    echo "# local edit" >> "$H2/bin/tool"
    printf '#!/bin/sh\necho tool-v3\n' > "$REPO/system/bin/tool"
    out=$(run_install "$H2" Linux); rc=$?
    check "hand-edited copy is skipped" '[[ $rc -ne 0 ]] && grep -q "local edit" "$H2/bin/tool"'
fi

# --- macOS: launchd plist copied ---------------------------------------------
H3="$FIXTURE/home3"; mkdir -p "$H3/bin" "$H3/Library/LaunchAgents"
run_install "$H3" Darwin >/dev/null
check "launchd plist copied on macOS" 'cmp -s "$REPO/system/launchd/com.example.test.plist" "$H3/Library/LaunchAgents/com.example.test.plist"'

echo "install-switchboard-bin: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
