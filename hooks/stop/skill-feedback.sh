#!/bin/bash
# =============================================================================
# skill-feedback.sh — Stop: 记录 skill 使用反馈
#
# 对标 LangMem procedural memory + EverOS 反馈闭环:
#   每次会话结束时，如果用了 skill，记录成功/失败反馈
#   用于后续剪枝和版本管理
# =============================================================================
set -euo pipefail

INPUT=$(cat)

# 从 session_memory 提取 skill 使用信息
SESSION_MEMORY="$HOME/.hermes/session_memory.md"

if [ ! -f "$SESSION_MEMORY" ]; then
    exit 0
fi

# 检测是否使用了 skill
SKILL_USED=$(grep -o "skill_view\|skill_manage\|Loaded.*skill" "$SESSION_MEMORY" 2>/dev/null | head -5 | tr '\n' ' ' | cut -c1-200)

if [ -z "$SKILL_USED" ]; then
    exit 0
fi

# 提取评分
SCORE=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);r=d.get('result',{});print(r.get('score',0)or r.get('grade',0)or 0)" 2>/dev/null || echo "0")

# 判断成功
SUCCESS="true"
if [ "$(echo "$SCORE < 5" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
    SUCCESS="false"
fi

# 记录到 skill_distiller
python3 "$HOME/.hermes/tools/skill_distiller.py" feedback \
    --skill "auto-learned" \
    --success "$SUCCESS" \
    --score "$SCORE" \
    --notes "skill_used=$SKILL_USED" \
    2>/dev/null || true

exit 0