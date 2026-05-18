#!/bin/bash
# PostToolUse: 追踪 token 消耗 + 坍塌预警
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)

# 估算 token 消耗
RESULT=$(echo "$INPUT" | python3 -c "import sys,json;r=json.load(sys.stdin).get('result',{});t=r if isinstance(r,str) else json.dumps(r)[:5000];print(len(t)//4)" 2>/dev/null || echo "0")

# 归类到正确的层
case "$TOOL" in
    session_search|memory|memory_search)
        LAYER="semantic" ;;
    delegate_task|execute_code)
        LAYER="working" ;;
    terminal|browser_*|web_*)
        LAYER="working" ;;
    skill_view|skill_manage)
        LAYER="procedural" ;;
    *)
        LAYER="working" ;;
esac

# 消费
python3 "$HOME/.hermes/tools/context_budget.py" consume --layer "$LAYER" --amount "$RESULT" 2>/dev/null || true

# 检查是否需要压缩
RATE=$(python3 "$HOME/.hermes/tools/context_budget.py" quota --layer "working" 2>/dev/null | \
    python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('usage_pct',0))" 2>/dev/null || echo "0")

if [ "$(echo "$RATE > 0.85" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
    python3 "$HOME/.hermes/tools/context_budget.py" collapse 2>/dev/null || true
fi

exit 0