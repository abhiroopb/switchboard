#!/usr/bin/env bash
# cos-ack.sh: one tool call per CoS routing turn. CoS is an orchestrator that routes
# and then moves on to the next thing.
#
# CoS used to spend 4 to 8 tool calls before replying to a routing ask: close a todo,
# look up an id, record a CoS Review ruling, read a note to print a parked draft, then
# dispatch. Each call costs a model round trip at 5 to 7 s. This script folds every
# deterministic bookkeeping step and the dispatch into one call and prints the dispatch
# `message` line last, so CoS copies it and stops.
#
# Usage:
#   cos-ack.sh [--origin cos|launchpad] [bookkeeping...] [dispatch args...] '<prompt>'
#   cos-ack.sh [bookkeeping...]                      # bookkeeping only, no dispatch
#
# Bookkeeping (each repeatable, all run before the dispatch, all best-effort):
#   --close <todo-id> [--reason "<why>"]   todo_cli.py complete --id <todo-id> (full id or a unique prefix such as the 8-hex short id)
#   --rule <rt-id>:<go|veto|drop|hold|sent|approved>[:"<reason>"]
#                                          cos_review_actions.py complete (go, sent, approved) or skip
#                                          (veto, drop, hold), --by cos
#   --dismiss-draft <N>                    triage_ledger.py draft --draft-id draft-N --status dismissed --by owner
#   --print-draft <note.md>                print the note's "Draft for the owner to send" block verbatim, fenced,
#                                          so CoS reads it with one call (external-replies Rule 8)
# Dispatch: every remaining argument is passed to router-dispatch.sh unchanged
# (--steer, --task-id, --depends-on, --continuation-of, --delivery, ... and the prompt).
#
# Output: one JSON object on stdout with bookkeeping results and the dispatch JSON, then the
# dispatch `message` line alone on the final line (or `ack: <bookkeeping summary>` when nothing
# was dispatched). Exit code is router-dispatch.sh's when it ran, else 0, or 1 when a bookkeeping
# step failed. A failure is recorded in the JSON (`ok: false`, `bookkeeping_ok: false`), printed as
# `ack: FAILED ...` on stderr and above the final line, and never blocks the dispatch (start-first).
# With no dispatch, `ack: FAILED ...` is the final line instead of `ack: bookkeeping done`.
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export REPO_ROOT
SCRIPTS="$REPO_ROOT/system/scripts"
TODO_CLI="${TODO_CLI:-$HOME/.agents/skills/todo/scripts/todo_cli.py}"
PY="${PYTHON:-python3}"

if (( $# == 0 )) || [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit $(( $# == 0 ? 2 : 0 ))
fi

ORIGIN="${SWITCHBOARD_DISPATCH_ORIGIN:-cos}"
CLOSE_IDS=()
CLOSE_REASON=""
RULES=()
DISMISS_DRAFTS=()
PRINT_DRAFTS=()
DISPATCH_ARGS=()
while (( $# > 0 )); do
    case "$1" in
        --origin) ORIGIN="${2:-cos}"; shift 2 ;;
        --close) CLOSE_IDS+=("${2:-}"); shift 2 ;;
        --reason) CLOSE_REASON="${2:-}"; shift 2 ;;
        --rule) RULES+=("${2:-}"); shift 2 ;;
        --dismiss-draft) DISMISS_DRAFTS+=("${2:-}"); shift 2 ;;
        --print-draft) PRINT_DRAFTS+=("${2:-}"); shift 2 ;;
        *) DISPATCH_ARGS+=("$1"); shift ;;
    esac
done

RESULTS_FILE="$(mktemp "${TMPDIR:-/tmp}/cos-ack.XXXXXX")"
trap 'rm -f "$RESULTS_FILE"' EXIT
printf '[]' >"$RESULTS_FILE"

record() {  # record <kind> <target> <ok 0|1> <detail>
    "$PY" - "$RESULTS_FILE" "$1" "$2" "$3" "$4" <<'PYEOF'
import json, sys
path, kind, target, ok, detail = sys.argv[1:6]
rows = json.load(open(path))
good = ok == "0"
detail = detail.strip()
rows.append({"kind": kind, "target": target, "ok": good, "detail": detail.splitlines()[0][:160] if good and detail else detail[-400:]})
json.dump(rows, open(path, "w"))
PYEOF
}

for tid in ${CLOSE_IDS[@]+"${CLOSE_IDS[@]}"}; do
    [[ -z "$tid" ]] && continue
    if [[ -f "$TODO_CLI" ]]; then
        args=(complete --id "$tid")
        [[ -n "$CLOSE_REASON" ]] && args+=(--reason "$CLOSE_REASON")
        out="$("$PY" "$TODO_CLI" "${args[@]}" 2>&1)"; rc=$?
        record close "$tid" "$rc" "$out"
    else
        record close "$tid" 1 "todo_cli.py not found at $TODO_CLI"
    fi
done

for rule in ${RULES[@]+"${RULES[@]}"}; do
    IFS=':' read -r rt verdict reason <<<"$rule"
    if [[ ! "$rt" =~ ^rt-[0-9a-f]{12}$ ]]; then
        record rule "$rule" 1 "expected rt-xxxxxxxxxxxx:<go|veto|drop|hold|sent|approved>[:reason]"
        continue
    fi
    case "$verdict" in
        go|sent|approved|scheduled) action=complete ;;
        veto|drop|hold|skip) action=skip ;;
        *) record rule "$rule" 1 "unknown verdict '$verdict'"; continue ;;
    esac
    args=("$action" --task-id "$rt" --by cos --reason "${reason:-the owner: $verdict}")
    out="$("$PY" "$SCRIPTS/cos_review_actions.py" "${args[@]}" 2>&1)"; rc=$?
    record rule "$rt:$verdict" "$rc" "$out"
done

for n in ${DISMISS_DRAFTS[@]+"${DISMISS_DRAFTS[@]}"}; do
    [[ -z "$n" ]] && continue
    n="${n#draft-}"
    out="$("$PY" "$SCRIPTS/triage_ledger.py" draft --draft-id "draft-$n" --status dismissed --by owner 2>&1)"; rc=$?
    record dismiss-draft "draft-$n" "$rc" "$out"
    if (( rc == 0 )); then
        out2="$("$PY" "$SCRIPTS/cos_review_actions.py" skip --item-id "draft:$n" --by cos --reason "the owner: dismiss draft $n" 2>&1)" || true
        record rule "draft:$n:skip" 0 "$out2"
    fi
done

DRAFT_BLOCKS=""
for note in ${PRINT_DRAFTS[@]+"${PRINT_DRAFTS[@]}"}; do
    [[ -z "$note" ]] && continue
    path="$note"
    [[ "$path" != /* ]] && path="$REPO_ROOT/$path"
    block="$("$PY" - "$path" <<'PYEOF'
import re, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.exists():
    print(f"(note not found: {p})"); sys.exit(1)
text = p.read_text(encoding="utf-8", errors="replace")
m = re.search(r"^#{2,6}\s*Draft for the owner to send.*?$", text, re.M)
if not m:
    print("(no 'Draft for the owner to send' heading in note)"); sys.exit(1)
body = text[m.end():]
nxt = re.search(r"^#{1,6}\s", body, re.M)
if nxt:
    body = body[:nxt.start()]
fence = re.search(r"```[^\n]*\n(.*?)```", body, re.S)
print((fence.group(1) if fence else body).strip())
PYEOF
)"; rc=$?
    record print-draft "$note" "$rc" "$(printf '%s' "$block" | head -c 200)"
    DRAFT_BLOCKS+=$'\n'"Draft (from $note):"$'\n''```'$'\n'"$block"$'\n''```'$'\n'
done

DISPATCH_JSON=""
DISPATCH_RC=0
if (( ${#DISPATCH_ARGS[@]} > 0 )); then
    DISPATCH_JSON="$(bash "$SCRIPTS/router-dispatch.sh" --origin "$ORIGIN" ${DISPATCH_ARGS[@]+"${DISPATCH_ARGS[@]}"} 2>&1)" || DISPATCH_RC=$?
fi

"$PY" - "$RESULTS_FILE" "$DISPATCH_RC" "$ORIGIN" <<'PYEOF' "$DISPATCH_JSON"
import json, sys
results = json.load(open(sys.argv[1]))
rc = int(sys.argv[2]); origin = sys.argv[3]
raw = sys.argv[4] if len(sys.argv) > 4 else ""
dispatch = None
message = ""
if raw.strip():
    try:
        dispatch = json.loads(raw)
        message = str(dispatch.get("message") or "").strip() or f"{dispatch.get('status', 'unknown')}"
    except json.JSONDecodeError:
        dispatch = {"status": "failed", "raw": raw.strip()[-600:]}
        message = "failed: router-dispatch.sh returned no JSON; see raw"
summary = ", ".join(f"{r['kind']} {r['target']} {'ok' if r['ok'] else 'FAILED'}" for r in results) or "nothing to do"
out = {"origin": origin, "bookkeeping": results, "bookkeeping_ok": all(r["ok"] for r in results),
       "dispatch": dispatch, "dispatch_rc": rc, "message": message or f"ack: {summary}"}
print(json.dumps(out, indent=2))
PYEOF
[[ -n "$DRAFT_BLOCKS" ]] && printf '%s' "$DRAFT_BLOCKS"
# A failed bookkeeping step is never reported as done, so a --close of an unknown id
# never prints "ack: bookkeeping done". FAILED_LINE names each failed step
# with its error; it goes to stderr and, just above the final line, to stdout.
FAILED_LINE="$("$PY" - "$RESULTS_FILE" <<'PYEOF'
import json, sys
bad = [r for r in json.load(open(sys.argv[1])) if not r["ok"]]
if bad:
    parts = []
    for r in bad:
        detail = (r.get("detail") or "").strip().splitlines()
        parts.append(f"{r['kind']} {r['target']} ({detail[-1][:160] if detail else 'no output'})")
    print(f"ack: FAILED {len(bad)} bookkeeping step(s): " + "; ".join(parts))
PYEOF
)"
if [[ -n "$FAILED_LINE" ]]; then
    printf '%s\n' "$FAILED_LINE" >&2
fi
# Final line: the message CoS replies with verbatim.
if [[ -n "$DISPATCH_JSON" ]]; then
    [[ -n "$FAILED_LINE" ]] && printf '%s\n' "$FAILED_LINE"
    "$PY" -c 'import json,sys
try: print(json.loads(sys.argv[1]).get("message") or "")
except Exception: print("failed: router-dispatch.sh returned no JSON")' "$DISPATCH_JSON"
    exit "$DISPATCH_RC"
fi
if [[ -n "$FAILED_LINE" ]]; then
    printf '%s\n' "$FAILED_LINE"
    exit 1
fi
echo "ack: bookkeeping done"
exit 0
