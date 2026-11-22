#!/usr/bin/env bash
# test-agent-mode-policy.sh — focused tests for the agent-mode policy:
#
#   * ultra is the PM OS default: missing/invalid modes
#     resolve to ultra.
#   * explicit ultra stays ultra; explicit high stays high (the owner's explicit
#     choice via PM_OS_AGENT_MODE or agent_mode: config always wins).
#   * medium and low NEVER reach Amp — legacy inputs coerce to ultra, in both
#     resolve_agent_mode and local_amp_launch_command (defense in depth).
#   * simple routines explicitly configured high launch high.
#
# Hermetic: temp fixture REPO_ROOT, no cmux calls, no Amp launches.
#
# Run: bash system/scripts/test-agent-mode-policy.sh

set -uo pipefail
# Amp-shaped fixtures (`-m <mode>` launch strings, Amp screens): pin the agent
# knob, which defaults to claude (system/scripts/pm_os_agent.py).
# Claude launch strings are covered by test_dispatch.py and test_pm_os_agent.py.
export PM_OS_AGENT=amp

REAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/agent-mode-test.XXXXXX")"
trap 'rm -rf "$FIXTURE"' EXIT

mkdir -p "$FIXTURE/system/state" "$FIXTURE/workstreams" "$FIXTURE/routines"

export REPO_ROOT="$FIXTURE"

# shellcheck source=../cmux-helpers.sh
source "$REAL_REPO/system/cmux-helpers.sh"
set +e  # helpers enable -e; tests must accumulate failures instead

# The floor assertions below describe the steady state; the dated CoS mode
# trial (router.yaml `cos_mode_trial:`) is tested on its own in
# the "CoS mode trial" section, so keep it off everywhere else.
export PM_OS_COS_MODE_TRIAL_DISABLED=1

PASS=0
FAIL=0
ok()   { echo "ok   - $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL - $1"; FAIL=$((FAIL + 1)); }

write_config() {  # <kind> <name> [agent_mode]
    local dir="$FIXTURE/$1/$2"
    mkdir -p "$dir"
    { echo "name: \"$2\""; [[ -n "${3:-}" ]] && echo "agent_mode: $3"; } \
        > "$dir/config.yaml"
}

# --- resolve_agent_mode ------------------------------------------------------

# No env var, no config -> ultra (policy default).
unset PM_OS_AGENT_MODE
mode=$(resolve_agent_mode "no-such-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "no mode anywhere resolves to ultra" \
    || fail "no mode anywhere resolves to ultra (got: $mode)"

# Config with no agent_mode field -> ultra.
write_config workstreams plain-workstream
mode=$(resolve_agent_mode "plain-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "config without agent_mode resolves to ultra" \
    || fail "config without agent_mode resolves to ultra (got: $mode)"

# Explicit ultra stays ultra (config path).
write_config workstreams ultra-workstream ultra
mode=$(resolve_agent_mode "ultra-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "explicit config ultra stays ultra" \
    || fail "explicit config ultra stays ultra (got: $mode)"

# Explicit high stays high (config path).
write_config workstreams high-workstream high
mode=$(resolve_agent_mode "high-workstream" 2>/dev/null)
[[ "$mode" == "high" ]] \
    && ok "explicit config high stays high" \
    || fail "explicit config high stays high (got: $mode)"

# Legacy medium coerces to ultra (config path).
write_config workstreams medium-workstream medium
mode=$(resolve_agent_mode "medium-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "legacy config medium coerces to ultra" \
    || fail "legacy config medium coerces to ultra (got: $mode)"

# Legacy low coerces to ultra (config path).
write_config workstreams low-workstream low
mode=$(resolve_agent_mode "low-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "legacy config low coerces to ultra" \
    || fail "legacy config low coerces to ultra (got: $mode)"

# Invalid value -> ultra.
write_config workstreams typo-workstream turbo
mode=$(resolve_agent_mode "typo-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "invalid config value defaults to ultra" \
    || fail "invalid config value defaults to ultra (got: $mode)"

# Env var precedence: explicit ultra/high pass, medium/low coerce.
mode=$(PM_OS_AGENT_MODE=ultra resolve_agent_mode "plain-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "env ultra stays ultra" || fail "env ultra stays ultra (got: $mode)"
mode=$(PM_OS_AGENT_MODE=high resolve_agent_mode "plain-workstream" 2>/dev/null)
[[ "$mode" == "high" ]] \
    && ok "env high stays high" || fail "env high stays high (got: $mode)"
mode=$(PM_OS_AGENT_MODE=medium resolve_agent_mode "plain-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "env medium coerces to ultra" || fail "env medium coerces to ultra (got: $mode)"
mode=$(PM_OS_AGENT_MODE=low resolve_agent_mode "plain-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "env low coerces to ultra" || fail "env low coerces to ultra (got: $mode)"

# Explicit user choice beats the ultra default: env high wins over a config
# that says ultra, and config high wins over the default.
write_config workstreams cfg-ultra-workstream ultra
mode=$(PM_OS_AGENT_MODE=high resolve_agent_mode "cfg-ultra-workstream" 2>/dev/null)
[[ "$mode" == "high" ]] \
    && ok "explicit env high overrides config ultra" \
    || fail "explicit env high overrides config ultra (got: $mode)"

# Simple routine explicitly configured high launches high (routine config path).
write_config routines simple-routine high
mode=$(resolve_agent_mode "simple-routine" 2>/dev/null)
[[ "$mode" == "high" ]] \
    && ok "simple routine configured high resolves to high" \
    || fail "simple routine configured high resolves to high (got: $mode)"

# --- core-surface ultra floor ------------------------------------------------
# CoS (megadesk), LaunchPad, Comms Triage, and Router always resolve to ultra:
# no config, config high, legacy config, invalid config, and env high all floor.

for surface in megadesk launchpad comms-triage router; do
    unset PM_OS_AGENT_MODE
    rm -rf "$FIXTURE/routines/$surface"
    mode=$(resolve_agent_mode "$surface" 2>/dev/null)
    [[ "$mode" == "ultra" ]] \
        && ok "core surface $surface with no config floors to ultra" \
        || fail "core surface $surface with no config floors to ultra (got: $mode)"

    write_config routines "$surface" high
    mode=$(resolve_agent_mode "$surface" 2>/dev/null)
    [[ "$mode" == "ultra" ]] \
        && ok "core surface $surface config high floors to ultra" \
        || fail "core surface $surface config high floors to ultra (got: $mode)"

    write_config routines "$surface" medium
    mode=$(resolve_agent_mode "$surface" 2>/dev/null)
    [[ "$mode" == "ultra" ]] \
        && ok "core surface $surface legacy config floors to ultra" \
        || fail "core surface $surface legacy config floors to ultra (got: $mode)"

    write_config routines "$surface" ultra
    mode=$(PM_OS_AGENT_MODE=high resolve_agent_mode "$surface" 2>/dev/null)
    [[ "$mode" == "ultra" ]] \
        && ok "core surface $surface env high floors to ultra" \
        || fail "core surface $surface env high floors to ultra (got: $mode)"

    mode=$(resolve_agent_mode "$surface" 2>/dev/null)
    [[ "$mode" == "ultra" ]] \
        && ok "core surface $surface config ultra stays ultra" \
        || fail "core surface $surface config ultra stays ultra (got: $mode)"
done

# Floor is by exact routine name, case-insensitive; a workstream with a similar
# name is NOT floored (policy applies to the four surfaces only).
mode=$(resolve_agent_mode "Router" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "floor matches core surface names case-insensitively" \
    || fail "floor matches core surface names case-insensitively (got: $mode)"
write_config workstreams router-followups high
mode=$(resolve_agent_mode "router-followups" 2>/dev/null)
[[ "$mode" == "high" ]] \
    && ok "floor does not leak to similarly named workstreams" \
    || fail "floor does not leak to similarly named workstreams (got: $mode)"

# The floor logs a WARN when it overrides a lower value.
write_config routines launchpad high
warn=$(resolve_agent_mode "launchpad" 2>&1 >/dev/null)
[[ "$warn" == *"always launches ultra"* ]] \
    && ok "floor override logs a WARN" \
    || fail "floor override logs a WARN (got: $warn)"

# --- CoS mode trial (router.yaml cos_mode_trial) ------------------------------
# A dated window lowers CoS (megadesk) alone; every other core surface and
# every worker keeps the ultra floor. Outside the window, or with the kill
# switch set, CoS floors to ultra as before.
trial_cfg="$FIXTURE/router-trial.yaml"
today_pt=$(TZ=America/Los_Angeles date +%F)
printf 'cos_mode_trial:\n  mode: high\n  start: %s\n  end: %s\n' "$today_pt" "$today_pt" > "$trial_cfg"
unset PM_OS_AGENT_MODE
rm -rf "$FIXTURE/routines/megadesk"
mode=$(PM_OS_COS_MODE_TRIAL_DISABLED=0 ROUTER_CONFIG="$trial_cfg" resolve_agent_mode "megadesk" 2>/dev/null)
[[ "$mode" == "high" ]] \
    && ok "CoS mode trial: megadesk resolves to high inside the window" \
    || fail "CoS mode trial: megadesk resolves to high inside the window (got: $mode)"
for surface in launchpad comms-triage router; do
    rm -rf "$FIXTURE/routines/$surface"
    mode=$(PM_OS_COS_MODE_TRIAL_DISABLED=0 ROUTER_CONFIG="$trial_cfg" resolve_agent_mode "$surface" 2>/dev/null)
    [[ "$mode" == "ultra" ]] \
        && ok "CoS mode trial: $surface stays ultra inside the window" \
        || fail "CoS mode trial: $surface stays ultra inside the window (got: $mode)"
done
mode=$(PM_OS_COS_MODE_TRIAL_DISABLED=0 ROUTER_CONFIG="$trial_cfg" resolve_agent_mode "plain-workstream" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "CoS mode trial: a workstream stays ultra inside the window" \
    || fail "CoS mode trial: a workstream stays ultra inside the window (got: $mode)"
mode=$(PM_OS_COS_MODE_TRIAL_DISABLED=1 ROUTER_CONFIG="$trial_cfg" resolve_agent_mode "megadesk" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "CoS mode trial: kill switch floors megadesk to ultra" \
    || fail "CoS mode trial: kill switch floors megadesk to ultra (got: $mode)"
printf 'cos_mode_trial:\n  mode: high\n  start: 2026-10-01\n  end: 2026-10-02\n' > "$trial_cfg"
mode=$(PM_OS_COS_MODE_TRIAL_DISABLED=0 ROUTER_CONFIG="$trial_cfg" resolve_agent_mode "megadesk" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "CoS mode trial: a closed window floors megadesk to ultra" \
    || fail "CoS mode trial: a closed window floors megadesk to ultra (got: $mode)"
printf 'cos_mode_trial:\n  mode: medium\n  start: %s\n  end: %s\n' "$today_pt" "$today_pt" > "$trial_cfg"
mode=$(PM_OS_COS_MODE_TRIAL_DISABLED=0 ROUTER_CONFIG="$trial_cfg" resolve_agent_mode "megadesk" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "CoS mode trial: a retired trial mode is ignored" \
    || fail "CoS mode trial: a retired trial mode is ignored (got: $mode)"
rm -f "$trial_cfg"

# --- real repo configs hold the canonical ultra value -------------------------
# The floor is defense in depth; the documented source of truth must also say ultra.
for cfg in routines/megadesk routines/launchpad routines/comms-triage routines/router; do
    val=$(python3 -c "import yaml,sys; print((yaml.safe_load(open(sys.argv[1])) or {}).get('agent_mode',''))" "$REAL_REPO/$cfg/config.yaml" 2>/dev/null)
    [[ "$val" == "ultra" ]] \
        && ok "repo $cfg/config.yaml agent_mode is ultra" \
        || fail "repo $cfg/config.yaml agent_mode is ultra (got: $val)"
done
val=$(python3 -c "import yaml,sys; print(((yaml.safe_load(open(sys.argv[1])) or {}).get('router') or {}).get('mode',''))" "$REAL_REPO/system/config/router.yaml" 2>/dev/null)
[[ "$val" == "ultra" ]] \
    && ok "repo system/config/router.yaml router.mode is ultra" \
    || fail "repo system/config/router.yaml router.mode is ultra (got: $val)"

# start-day.sh must derive the CoS mode from resolve_agent_mode, never hardcode -m.
if rg -q 'AMP_CMD -m high' "$REAL_REPO/system/start-day.sh"; then
    fail "start-day.sh still hardcodes -m high for CoS"
else
    ok "start-day.sh does not hardcode -m high"
fi
rg -q 'resolve_agent_mode "\$COS_WORKSPACE_NAME"' "$REAL_REPO/system/start-day.sh" \
    && ok "start-day.sh resolves the CoS mode through resolve_agent_mode" \
    || fail "start-day.sh resolves the CoS mode through resolve_agent_mode"

# Same resolution against the real repo: the CoS launch resolves to ultra.
mode=$(REPO_ROOT="$REAL_REPO" resolve_agent_mode "megadesk" 2>/dev/null)
[[ "$mode" == "ultra" ]] \
    && ok "CoS (megadesk) resolves to ultra against the real repo" \
    || fail "CoS (megadesk) resolves to ultra against the real repo (got: $mode)"

# --- LaunchPad / Comms Triage startup emit -m ultra ---------------------------
# Exercise the real launch-command builders with the real repo configs, the
# same path `cmux-helpers.sh launch routine:<name>` and ensure-routine-workspaces
# take through _boot_amp_in_workspace.
for surface in launchpad comms-triage megadesk; do
    mode=$(REPO_ROOT="$REAL_REPO" resolve_agent_mode "$surface" 2>/dev/null)
    cmd=$(local_amp_launch_command "$REAL_REPO" "$mode")
    [[ "$cmd" == *"-m ultra"* ]] \
        && ok "$surface startup launch command carries -m ultra" \
        || fail "$surface startup launch command carries -m ultra (got: $cmd)"
    cmd=$(local_amp_resume_command "$REAL_REPO" "$mode" "T-d6344a8d-b7af-46b4-8e6b-44f5c2334803")
    [[ "$cmd" == *"-m ultra"* ]] \
        && ok "$surface startup resume command carries -m ultra" \
        || fail "$surface startup resume command carries -m ultra (got: $cmd)"
done

# --- local_amp_launch_command (defense in depth) ------------------------------

# No mode arg -> -m ultra in the generated command (the default).
cmd=$(local_amp_launch_command "$FIXTURE/workstreams/plain-workstream")
[[ "$cmd" == *"-m ultra"* ]] \
    && ok "launch command defaults to -m ultra" \
    || fail "launch command defaults to -m ultra (got: $cmd)"

preamble=$(workstream_launch_preamble "workstream:plain-workstream")
[[ "$preamble" == *"ultra is the default for every PM OS thread"* && \
   "$preamble" == *"press Ctrl+S and select ultra"* && \
   "$preamble" == *"cannot change the active mode yourself"* ]] \
    && ok "workstream preamble carries ultra-default guidance" \
    || fail "workstream preamble is missing ultra-default guidance"

cmd=$(local_amp_resume_command "$FIXTURE/workstreams/plain-workstream" "" "T-d6344a8d-b7af-46b4-8e6b-44f5c2334803")
[[ "$cmd" == *"-m ultra"* ]] \
    && ok "resume command defaults to -m ultra" \
    || fail "resume command defaults to -m ultra (got: $cmd)"

# Explicit ultra/high pass through.
cmd=$(local_amp_launch_command "$FIXTURE/workstreams/x" "ultra")
[[ "$cmd" == *"-m ultra"* ]] \
    && ok "launch command keeps explicit ultra" \
    || fail "launch command keeps explicit ultra (got: $cmd)"
cmd=$(local_amp_launch_command "$FIXTURE/workstreams/x" "high")
[[ "$cmd" == *"-m high"* ]] \
    && ok "launch command keeps explicit high" \
    || fail "launch command keeps explicit high (got: $cmd)"

# medium/low never reach Amp even when passed directly.
for legacy in medium low; do
    cmd=$(local_amp_launch_command "$FIXTURE/workstreams/x" "$legacy")
    if [[ "$cmd" == *"-m ultra"* && "$cmd" != *"-m medium"* && "$cmd" != *"-m low"* ]]; then
        ok "launch command coerces $legacy to ultra"
    else
        fail "launch command coerces $legacy to ultra (got: $cmd)"
    fi
done

# Invalid mode arg -> ultra.
cmd=$(local_amp_launch_command "$FIXTURE/workstreams/x" "bogus")
[[ "$cmd" == *"-m ultra"* ]] \
    && ok "launch command defaults invalid mode to ultra" \
    || fail "launch command defaults invalid mode to ultra (got: $cmd)"

# --- new-workstream.py template defaults to ultra ------------------------------
rg -q '"agent_mode": "ultra"' "$REAL_REPO/system/scripts/new-workstream.py" \
    && ok "new-workstream.py config template defaults agent_mode to ultra" \
    || fail "new-workstream.py config template defaults agent_mode to ultra"

# --- cmux_new_contextual_amp_tab.py resolves the same default ------------------
py_mode=$(python3 - "$REAL_REPO/system/scripts/cmux_new_contextual_amp_tab.py" "$FIXTURE" <<'PY' 2>/dev/null
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("tab", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["tab"] = mod  # dataclasses need the module registered before exec
spec.loader.exec_module(mod)
fx = Path(sys.argv[2])
print(mod.resolve_mode(fx / "workstreams/plain-workstream/config.yaml"),
      mod.resolve_mode(fx / "workstreams/high-workstream/config.yaml"),
      mod.resolve_mode(fx / "workstreams/medium-workstream/config.yaml"),
      mod.DEFAULT_MODE)
PY
)
[[ "$py_mode" == "ultra high ultra ultra" ]] \
    && ok "cmux_new_contextual_amp_tab.py resolves default/explicit-high/legacy as ultra/high/ultra" \
    || fail "cmux_new_contextual_amp_tab.py resolves default/explicit-high/legacy as ultra/high/ultra (got: $py_mode)"

# --- end-to-end: resolve + build, real repo configs ---------------------------
# Uses the REAL repo configs (read-only) to prove the shipped routine/workstream
# settings produce the intended -m flags. comms-triage is omitted because it
# can temporarily exist under both roots and the workstream path wins.
export REPO_ROOT="$REAL_REPO"

for pair in "meetings:ultra" "scheduled-jobs:ultra" "todo:ultra" \
            "megadesk:ultra" "pm-os-signal-detection:ultra"; do
    name="${pair%%:*}"
    want="${pair##*:}"
    mode=$(resolve_agent_mode "$name" 2>/dev/null)
    cmd=$(local_amp_launch_command "$REAL_REPO/workstreams/$name" "$mode")
    if [[ "$mode" == "$want" && "$cmd" == *"-m $want"* ]]; then
        ok "shipped config: $name launches -m $want"
    else
        fail "shipped config: $name launches -m $want (mode=$mode, cmd=$cmd)"
    fi
done

export REPO_ROOT="$FIXTURE"

# --- summary ------------------------------------------------------------------
echo ""
echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
