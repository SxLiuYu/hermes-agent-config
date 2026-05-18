#!/bin/bash
# Agent Team workflow integration — 子 agent 协作脚本
#
# 用法（子 agent 在 terminal 中调用）:
#   team-bus msg-check              → 检查未读消息
#   team-bus msg-send TO "内容"     → 发送消息给队友
#   team-bus task-claim TASK_ID     → 认领任务
#   team-bus task-complete TASK_ID  → 完成任务
#   team-bus task-check             → 列出我的待办
#   team-bus file-lock PATH         → 锁定文件（防冲突）
#   team-bus file-unlock PATH       → 解锁文件
#
# 环境变量（由 delegate_task 设置）:
#   TEAM_NAME     — 团队名
#   AGENT_ID      — 当前 agent ID
#   AGENT_ROLE    — 角色: lead / worker

BUS_SCRIPT="$HOME/.hermes/scripts/agent_bus.py"
TEAM_NAME="${TEAM_NAME:-default-team}"
AGENT_ID="${AGENT_ID:-unknown-agent}"

[ -f "$BUS_SCRIPT" ] || { echo "agent_bus.py not found"; exit 1; }

cmd="$1"
shift

case "$cmd" in
    msg-check)
        python3 "$BUS_SCRIPT" msg-check --agent "$AGENT_ID"
        ;;
    msg-send)
        to="$1"; content="$2"
        [ -z "$to" ] && { echo "Usage: team-bus msg-send TO 'content'"; exit 1; }
        python3 "$BUS_SCRIPT" msg-send --team "$TEAM_NAME" \
            --from "$AGENT_ID" --to "$to" --content "$content"
        ;;
    task-claim)
        task_id="$1"
        [ -z "$task_id" ] && { echo "Usage: team-bus task-claim TASK_ID"; exit 1; }
        python3 "$BUS_SCRIPT" task-claim --task "$task_id" --agent "$AGENT_ID"
        ;;
    task-complete)
        task_id="$1"; result="${2:-}"
        [ -z "$task_id" ] && { echo "Usage: team-bus task-complete TASK_ID [result]"; exit 1; }
        python3 "$BUS_SCRIPT" task-complete --task "$task_id" --result "$result"
        ;;
    task-check)
        # 列出自己的待办任务
        python3 "$BUS_SCRIPT" task-list --team "$TEAM_NAME" --status "in_progress" | grep "$AGENT_ID" || echo "(无进行中任务)"
        ;;
    task-list)
        python3 "$BUS_SCRIPT" task-list --team "$TEAM_NAME" "$@"
        ;;
    file-lock)
        path="$1"
        [ -z "$path" ] && { echo "Usage: team-bus file-lock PATH"; exit 1; }
        python3 "$BUS_SCRIPT" file-lock --path "$path" --agent "$AGENT_ID" --team "$TEAM_NAME"
        ;;
    file-unlock)
        path="$1"
        [ -z "$path" ] && { echo "Usage: team-bus file-unlock PATH"; exit 1; }
        python3 "$BUS_SCRIPT" file-unlock --path "$path" --agent "$AGENT_ID"
        ;;
    file-check)
        python3 "$BUS_SCRIPT" file-check --path "$1"
        ;;
    status)
        python3 "$BUS_SCRIPT" team-status --name "$TEAM_NAME"
        ;;
    *)
        echo "Agent Team Bus 命令:"
        echo "  msg-check             检查未读消息"
        echo "  msg-send TO '内容'    发送消息"
        echo "  task-claim TASK_ID    认领任务"
        echo "  task-complete TASK_ID 完成任务"
        echo "  task-list             列出团队任务"
        echo "  file-lock PATH        锁定文件"
        echo "  file-unlock PATH      解锁文件"
        echo "  status                团队概览"
        ;;
esac