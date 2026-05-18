#!/bin/bash
# SubagentStop: build trace tree + run failure diagnosis + report to stderr
# Calls execution_tracer.py build-tree and diagnose for the session
set -euo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','') or d.get('subagent_id',''))" 2>/dev/null || echo "${HOOK_EVENT_SESSION_ID:-${HERMES_SESSION_ID:-unknown}}")

TRACER="$HOME/.hermes/tools/execution_tracer.py"
[ -f "$TRACER" ] || exit 0

# Build the hierarchical trace tree
python3 "$TRACER" build-tree --session-id "$SESSION_ID" 2>/dev/null || true

# Run failure diagnosis
DIAG=$(python3 "$TRACER" diagnose --session-id "$SESSION_ID" 2>&1)

# Check if diagnosis found failures
HAS_FAILURE=$(echo "$DIAG" | python3 -c "import sys,json; d=json.load(sys.stdin); fs=d.get('failure_stage','') or ''; print('yes' if fs else 'no')" 2>/dev/null || echo "no")

if [ "$HAS_FAILURE" = "yes" ]; then
    echo "[execution-diagnose] === FAILURE DIAGNOSIS: session=$SESSION_ID ===" >&2
    echo "$DIAG" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'Diagnosis: {d.get(\"diagnosis\",\"\")}')
print(f'Failure Stage: {d.get(\"failure_stage\",\"\")}')
print(f'Failed: {d.get(\"failed_nodes\",0)}/{d.get(\"total_nodes\",0)} nodes')
print(f'Mutate Nodes: {d.get(\"mutating_nodes\",0)}')
if d.get('error_chain'):
    print('Error Chain:')
    for e in d['error_chain']:
        print(f'  [{e.get(\"action\",\"?\")}] {e.get(\"tool\",\"?\")}: {e.get(\"error\",\"\")[:120]}')
if d.get('evidence'):
    print(f'Evidence: {len(d[\"evidence\"])} items collected')
" >&2 2>/dev/null || echo "$DIAG" >&2
fi

exit 0