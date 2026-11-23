#!/usr/bin/env bash
set -euo pipefail

# ensure-p0-workspaces.sh -- make every open P0 to-do visible (and workable)
# on the cmux board.
#
# Sidebar contract: workspace titles contain "P0 ·" so the switchboard sidebar
# (system/cmux-sidebars/switchboard.swift) buckets them into the
# "P0 to-dos" lane rendered between Idle and Background.
#
# Behavior (workstreams are woven into the P0s):
#   * Selects surfaced P0 items from system/state/todo.json: active
#     (open|actionable, not snoozed into the future) or review-needed
#     (waiting with review_on arrived). See scripts/lib/p0-todos.py.
#   * P0 linked to a valid workstream -> ONE canonical workspace per
#     workstream, titled "<workstream emoji> P0 · <display name>", seeded with the
#     workstream's full launch preamble + startup instruction + a P0 header.
#     This tab IS the workstream's execution/review space; no separate normal
#     workstream tab is launched (all launch paths in cmux-helpers.sh target
#     the same title). An already-open normal tab is consolidated by RENAME
#     (live session preserved); a true duplicate is closed only when provably
#     idle with an empty input box (cig_safe), else deferred to the next run.
#   * P0 without a linked workstream -> reminder-card workspace titled
#     "<emoji> P0 · <todo title>" (emoji hashed deterministically from the
#     todo id — varied per card, never a uniform 🔥). The agent renders the
#     card and WAITS — it must not start executing the task unprompted.
#   * Idempotent: existing workspaces with the expected titles are reused.
#   * Reconciles: stale unlinked reminder tabs are closed (only when safe);
#     stale linked P0 tabs (P0 completed/downgraded) are RENAMED back to the
#     normal workstream title, never closed.
#
# Called by start-day.sh Phase 4b; safe to run manually any time.
#
# Usage: ensure-p0-workspaces.sh [--dry-run]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export REPO_ROOT

# shellcheck source=../cmux-helpers.sh
source "$REPO_ROOT/system/cmux-helpers.sh"

TODO_JSON="${TODO_JSON:-$SWITCHBOARD_TODO_JSON}"
TODO_CLI="$HOME/.agents/skills/todo/scripts/todo_cli.py"

DRY_RUN="false"
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="true"

if [[ ! -f "$TODO_JSON" ]]; then
    log_warn "No todo store at $TODO_JSON; nothing to do"
    exit 0
fi

# ---- 1) Collect surfaced P0s as unit-separator (\x1f) delimited lines:
#          id \x1f title \x1f workstream \x1f detail
# Classification (active open|actionable, or review-needed waiting whose
# review_on has arrived; never snoozed) lives in scripts/lib/p0-todos.py —
# the single source of truth shared with cmux-helpers.sh.
US=$'\x1f'
collect_p0s() {
    python3 "$REPO_ROOT/system/scripts/lib/p0-todos.py" "$TODO_JSON"
}

p0_lines="$(collect_p0s)"

valid_workstream() {
    local slug="$1"
    [[ -n "$slug" && -d "$REPO_ROOT/workstreams/$slug" && -f "$REPO_ROOT/workstreams/$slug/CONTEXT.md" ]]
}

# Partition P0s: linked (valid workstream) vs unlinked (reminder cards).
# linked_slugs: unique slugs, one canonical workspace each.
# linked_entries lines: slug \x1f title  (for badges / logging).
# unlinked_entries lines: id \x1f title  (for reminder naming / migration).
linked_slugs=""
linked_entries=""
unlinked_entries=""
expected_p0_titles=""
while IFS="$US" read -r _id p0_title p0_ws _detail; do
    [[ -z "$p0_title" ]] && continue
    if valid_workstream "$p0_ws"; then
        linked_entries+="${p0_ws}${US}${p0_title}"$'\n'
        if ! printf '%s\n' "$linked_slugs" | grep -qxF "$p0_ws"; then
            linked_slugs+="$p0_ws"$'\n'
        fi
        expected_p0_titles+="$(p0_workspace_name_for_workstream "$p0_ws")"$'\n'
    else
        unlinked_entries+="${_id}${US}${p0_title}"$'\n'
        expected_p0_titles+="$(p0_reminder_workspace_name "$_id" "$p0_title")"$'\n'
    fi
done <<< "$p0_lines"

expected_p0_title_exists() {
    local target="$1"
    local expected
    while IFS= read -r expected; do
        [[ "$expected" == "$target" ]] && return 0
    done <<< "$expected_p0_titles"
    return 1
}

# Map a P0 workspace title (new "<emoji> P0 · <display>" or legacy
# "🔥 P0 · <display>") back to its slug, for renaming linked tabs — either
# migrating a legacy title to the new format or renaming a stale tab back to
# the normal workstream title. rc=1 if the title does not correspond to any
# current workstream directory.
slug_for_p0_workspace_title() {
    local title="$1"
    local ws_dir slug
    for ws_dir in "$REPO_ROOT/workstreams"/*/; do
        [[ -d "$ws_dir" ]] || continue
        slug=$(basename "$ws_dir")
        [[ "$slug" == "_archive" || "$slug" == "_inbox" || "$slug" == "shared" ]] && continue
        [[ -f "$ws_dir/CONTEXT.md" ]] || continue
        if [[ "$(p0_workspace_name_for_workstream "$slug")" == "$title" ]] \
            || [[ "$(legacy_p0_workspace_name_for_workstream "$slug")" == "$title" ]]; then
            echo "$slug"
            return 0
        fi
    done
    return 1
}

# New-format reminder name for a legacy "🔥 P0 · <title>" reminder card whose
# todo is still surfaced. rc=1 when the title matches no surfaced unlinked P0.
reminder_migration_name_for_title() {
    local ws_name="$1"
    local u_id u_title
    while IFS="$US" read -r u_id u_title; do
        [[ -z "$u_id" ]] && continue
        if [[ "$ws_name" == "🔥 P0 · $u_title" ]]; then
            p0_reminder_workspace_name "$u_id" "$u_title"
            return 0
        fi
    done <<< "$unlinked_entries"
    return 1
}

# ---- 2) Reconcile: handle P0-owned workspaces ("<emoji> P0 · " tag or the
# legacy "🔥 P0 ·" prefix) whose to-do is gone, closed, downgraded, or
# re-linked — and migrate legacy-titled tabs to the per-emoji format.
listing=$(cmux_cmd list-workspaces 2>/dev/null || true)
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    ref=$(echo "$line" | grep -oE 'workspace:[0-9]+' || true)
    [[ -z "$ref" ]] && continue
    ws_name=$(echo "$line" | sed -E 's/^[* ]*workspace:[0-9]+  //' | sed 's/ *\[selected\]$//')
    # Only reconcile workspaces this mechanism owns — never close a
    # user/workstream workspace that merely mentions "P0 ·" mid-title.
    is_p0_owned_title "$ws_name" || continue
    if expected_p0_title_exists "$ws_name"; then
        continue
    fi

    # LINKED tab under a non-canonical P0 title: rename to the current
    # canonical name — the new per-emoji P0 title while the P0 is still
    # surfaced (legacy-format migration), or the normal workstream title when
    # its P0s all closed. The session, thread, and scrollback survive.
    if stale_slug=$(slug_for_p0_workspace_title "$ws_name"); then
        target_name=$(workstream_canonical_display_name "$stale_slug")
        if [[ "$target_name" == "$ws_name" ]]; then
            continue
        fi
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "[DRY RUN] Would rename P0 workstream tab: $ws_name -> $target_name"
        else
            log_info "Renaming P0 workstream tab: $ws_name -> $target_name"
            cmux_cmd rename-workspace --workspace "$ref" "$target_name" &>/dev/null || \
                log_warn "Failed to rename P0 workstream tab $ref"
        fi
        continue
    fi

    # Legacy-titled reminder card whose todo is still surfaced: migrate to
    # the new "<emoji> P0 · <title>" format by rename (never close).
    if new_reminder_name=$(reminder_migration_name_for_title "$ws_name"); then
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "[DRY RUN] Would rename P0 reminder card: $ws_name -> $new_reminder_name"
        else
            log_info "Renaming P0 reminder card: $ws_name -> $new_reminder_name"
            cmux_cmd rename-workspace --workspace "$ref" "$new_reminder_name" &>/dev/null || \
                log_warn "Failed to rename P0 reminder card $ref"
        fi
        continue
    fi

    # Stale reminder tab: close, but only when provably safe (idle + empty
    # input box). the owner may have started working inside a reminder card.
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would close stale P0 workspace: $ws_name ($ref)"
    elif declare -F cig_safe >/dev/null 2>&1 && ! cig_safe "$ref"; then
        log_warn "Stale P0 workspace $ws_name ($ref) is busy or has a draft; leaving open (will retry next run)"
    else
        log_info "Closing stale P0 workspace: $ws_name ($ref)"
        cmux_cmd close-workspace --workspace "$ref" --force &>/dev/null || \
            log_warn "Failed to close stale P0 workspace $ref"
    fi
done <<< "$listing"

# ---- 2b) Badge canonical linked tabs (one aggregate badge per workstream)
# and clear stale badges. Which refs THIS script badged is tracked in a state
# file, so cleanup only ever touches refs we previously set — never a "p0"
# status some other tool might own.
P0_BADGE_STATE="${P0_BADGE_STATE:-$HOME/.config/amp/p0-badged-refs}"

current_owner_refs=""
badge_for_ref=""   # ref \x1f badge lines
while IFS= read -r slug; do
    [[ -z "$slug" ]] && continue
    canonical_name=$(p0_workspace_name_for_workstream "$slug")
    owner_ref=$(find_local_workspace_by_name "$canonical_name" 2>/dev/null || true)
    # Pre-creation (phase 3 hasn't run yet): fall back to the still-open
    # normal tab so the badge lands somewhere visible this run too.
    [[ -z "$owner_ref" ]] && owner_ref=$(workspace_ref_if_exists "$slug" 2>/dev/null || true)
    [[ -z "$owner_ref" ]] && continue
    owner_titles=$(printf '%s' "$linked_entries" | awk -F "$US" -v s="$slug" '$1 == s {print $2}')
    owner_count=$(printf '%s\n' "$owner_titles" | grep -c . || true)
    first_title=$(printf '%s\n' "$owner_titles" | head -1)
    if [[ "$owner_count" -gt 1 ]]; then
        badge="🔥 ${owner_count} P0s: ${first_title} +$((owner_count - 1))"
    else
        badge="🔥 P0: ${first_title}"
    fi
    current_owner_refs+="$owner_ref"$'\n'
    badge_for_ref+="${owner_ref}${US}${badge}"$'\n'
done <<< "$linked_slugs"

# Clear badges on refs badged in a previous run but no longer canonical.
if [[ -f "$P0_BADGE_STATE" ]]; then
    while IFS= read -r prev_ref; do
        [[ -z "$prev_ref" ]] && continue
        if ! printf '%s' "$current_owner_refs" | grep -qxF "$prev_ref"; then
            if [[ "$DRY_RUN" == "true" ]]; then
                log_info "[DRY RUN] Would clear stale P0 badge on $prev_ref"
            else
                log_info "Clearing stale P0 badge on $prev_ref"
                clear_workspace_status "$prev_ref" "p0"
            fi
        fi
    done < "$P0_BADGE_STATE"
fi

while IFS="$US" read -r owner_ref badge; do
    [[ -z "$owner_ref" ]] && continue
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would badge $owner_ref with: $badge"
    else
        set_workspace_status "$owner_ref" "p0" "$badge" "" "#FF4136"
        log_ok "Badged canonical P0 tab: $badge ($owner_ref)"
    fi
done <<< "$badge_for_ref"

# Persist the refs we badged this run (skip in dry-run to keep it read-only).
if [[ "$DRY_RUN" != "true" ]]; then
    mkdir -p "$(dirname "$P0_BADGE_STATE")"
    printf '%s' "$current_owner_refs" | grep . > "$P0_BADGE_STATE" 2>/dev/null || : > "$P0_BADGE_STATE"
fi

if [[ -z "$p0_lines" ]]; then
    log_info "No open P0 to-dos; no P0 workspaces to open"
    exit 0
fi

# ---- 3) Canonical workspaces for LINKED P0s: one per workstream, seeded with
# the workstream's full context. prepare_workspace targets the P0 title and
# renames an existing normal tab into it (consolidation without interruption).
while IFS= read -r slug; do
    [[ -z "$slug" ]] && continue
    canonical_name=$(p0_workspace_name_for_workstream "$slug")

    if [[ "$DRY_RUN" == "true" ]]; then
        if find_local_workspace_by_name "$canonical_name" >/dev/null 2>&1; then
            log_info "[DRY RUN] Canonical P0 workspace already open: $canonical_name"
        elif workspace_ref_if_exists "$slug" >/dev/null 2>&1; then
            log_info "[DRY RUN] Would rename open workstream tab into P0 space: $slug -> $canonical_name"
        else
            log_info "[DRY RUN] Would open canonical P0 workspace: $canonical_name (seeded with workstreams/$slug context)"
        fi
        continue
    fi

    # Case 1: canonical tab already open -> leave its live session untouched;
    # just clean up any leftover duplicate normal tab.
    if ws_ref=$(find_local_workspace_by_name "$canonical_name" 2>/dev/null); then
        log_info "Canonical P0 workspace already open: $canonical_name ($ws_ref)"
        consolidate_duplicate_workstream_tab "$slug" "$ws_ref"
        continue
    fi

    # Case 2: a normal workstream tab is open -> consolidate by RENAME only.
    # No reset, no re-seed: the live session already carries the workstream
    # context, and prepare_workspace's surface reset would kill it. Add the P0
    # header via a guarded send (deferred if busy — the title + badge still
    # carry the signal).
    if ws_ref=$(workspace_ref_if_exists "$slug" 2>/dev/null); then
        log_info "Consolidating open workstream tab into P0 space: $slug ($ws_ref) -> $canonical_name"
        cmux_cmd rename-workspace --workspace "$ws_ref" "$canonical_name" &>/dev/null || \
            log_warn "Failed to rename workstream tab $ws_ref to: $canonical_name"
        if declare -F cig_safe_send >/dev/null 2>&1; then
            p0_header="$(p0_prompt_header_for_workstream "$slug" || true)"
            if [[ -n "$p0_header" ]]; then
                # Pure hydration (P0 header into a live session): mark it so
                # the review-lane plugin neither treats its arrival as the owner
                # responding nor pins the turn to To Review.
                mark_startup_hydration "$ws_ref" "ensure-p0-header" || true
                if ! cig_safe_send "$ws_ref" "$p0_header" 30 3; then
                    clear_startup_hydration "$ws_ref"
                    log_info "P0 header deferred for $slug (tab busy); title/badge carry the signal"
                fi
            fi
        fi
        consolidate_duplicate_workstream_tab "$slug" "$ws_ref"
        continue
    fi

    # Case 3: no tab at all -> create fresh (prepare_workspace creates a new
    # workspace targeting the P0 title; is_fresh=true so no surface reset) and
    # seed the full workstream launch prompt + P0 header.
    # The prompt is written before the boot so a fresh Claude launch carries
    # it as its first turn (cmux-helpers.sh launch_seed_pointer).
    preamble="$(workstream_launch_preamble "workstream:$slug")"
    p0_header="$(p0_prompt_header_for_workstream "$slug" || true)"
    startup="$(workstream_startup_prompt "$slug")"
    prompt_file=$(write_launch_prompt_file "p0-ws-$slug" "$preamble
$p0_header
$startup")
    if ! ws_ref=$(SWITCHBOARD_LAUNCH_SEED_FILE="$prompt_file" SWITCHBOARD_LAUNCH_SEED_HYDRATION="ensure-p0-fresh-seed" \
        prepare_workspace "$slug" "$REPO_ROOT"); then
        log_error "Failed to prepare canonical P0 workspace for $slug"
        continue
    fi

    # Pure startup seed: a freshly opened P0 stays in the P0 lane when this
    # machine-seeded turn finishes (review-lane hydration contract).
    mark_startup_hydration "$ws_ref" "ensure-p0-fresh-seed" || true
    send_prompt_via_file "$ws_ref" "$prompt_file" || {
        clear_startup_hydration "$ws_ref"
        false
    }
    log_ok "Opened canonical P0 workspace: $canonical_name ($ws_ref)"
done <<< "$linked_slugs"

# ---- 4) Reminder-card workspaces for UNLINKED P0s (unchanged behavior).
while IFS="$US" read -r todo_id title ws_slug detail; do
    [[ -z "$todo_id" ]] && continue
    if valid_workstream "$ws_slug"; then
        continue  # handled by the canonical workstream workspace above
    fi
    display_name=$(p0_reminder_workspace_name "$todo_id" "$title")

    if ref=$(find_local_workspace_by_name "$display_name"); then
        log_info "P0 workspace already open: $display_name ($ref)"
        continue
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would open P0 workspace: $display_name"
        continue
    fi

    # Share the coarse launch lock with normal workstream creation. Re-check
    # after acquiring it so concurrent launchers cannot create duplicates.
    create_result=$(
        acquire_ws_launch_lock || exit 1
        trap 'release_ws_launch_lock' EXIT
        if existing_ref=$(find_local_workspace_by_name "$display_name"); then
            printf 'existing\t%s\n' "$existing_ref"
            exit 0
        fi

        log_info "Creating P0 workspace: $display_name"
        output=$(cmux_cmd new-workspace --name "$display_name" --cwd "$REPO_ROOT" 2>&1) || exit 1
        ws_ref=$(workspace_ref_after_create "$output" "$display_name") || exit 1
        cmux_cmd rename-workspace --workspace "$ws_ref" "$display_name" &>/dev/null || \
            log_warn "Failed to rename P0 workspace to: $display_name"
        printf 'created\t%s\n' "$ws_ref"
    ) || {
        log_error "Failed to create P0 workspace: $display_name"
        continue
    }
    IFS=$'\t' read -r create_status ws_ref <<< "$create_result"
    if [[ "$create_status" == "existing" ]]; then
        log_info "P0 workspace already open after launch lock: $display_name ($ws_ref)"
        continue
    fi

    if ! _boot_amp_in_workspace "$ws_ref" "$display_name" "$REPO_ROOT" "true"; then
        log_error "Amp failed to start in P0 workspace: $display_name"
        continue
    fi

    prompt="IMPORTANT: Skip all AGENTS.md session-start steps (todo, kb-distill, start-of-day, daemon, skill loading). You are a P0 reminder workspace opened by start-of-day.
This workspace exists so the owner sees this P0 to-do on their board and is reminded to complete it.
P0 to-do (id: $todo_id) — $detail
Do exactly this, then stop:
1. Render a compact reminder card: a 2-column table with the to-do title, why it is P0 if known, due date, source link(s), and one suggested first step.
2. WAIT for the owner. Do NOT start executing the task, opening documents, or messaging anyone until the owner asks.
When the owner asks you to work the task, help them complete it. When the owner confirms it is done (or says drop it):
- Mark it: python3 $TODO_CLI complete $todo_id   (or: python3 $TODO_CLI set-status $todo_id dropped)
- Then close this workspace: CMUX=/Applications/cmux.app/Contents/Resources/bin/cmux; \$CMUX close-workspace --workspace \$(\$CMUX identify | jq -r '.caller.workspace_ref') --force"

    prompt_file=$(write_launch_prompt_file "p0-$todo_id" "$prompt")
    # Pure startup seed: the reminder card is hydration, not reviewable work —
    # the card workspace stays in the P0 lane until real work lands.
    mark_startup_hydration "$ws_ref" "ensure-p0-reminder-seed" || true
    send_prompt_via_file "$ws_ref" "$prompt_file" || {
        clear_startup_hydration "$ws_ref"
        false
    }
    log_ok "Opened P0 workspace: $display_name ($ws_ref)"
done <<< "$p0_lines"
