#!/bin/bash
# journal-subagent: Auto-log subagent tasks for self-evolution
INPUT=$(cat)
GOAL=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);r=d.get('result',{});print((r.get('goal','') or r.get('task','') or d.get('goal','') or 'subagent task')[:200])" 2>/dev/null || echo "subagent task")
SCORE=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);r=d.get('result',{});print(r.get('score',0)or r.get('grade',0)or 0)" 2>/dev/null || echo "0")
OUTCOME=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);r=d.get('result',{});o=r.get('outcome','')or r.get('status','');print('success'if o in('success','completed','done')else('partial'if o in('partial','warning')else('failure'if o in('failure','error','failed')else('interrupted'if o=='interrupted'else'unknown'))))" 2>/dev/null || echo "unknown")
python3 "$HOME/.hermes/tools/task_journal.py" log "$GOAL" "$OUTCOME" "$SCORE" "subagent" 2>/dev/null
echo "[journal-subagent] Logged: $GOAL ($OUTCOME)" >&2
exit 0