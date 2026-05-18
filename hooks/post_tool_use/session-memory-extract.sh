#!/bin/bash
# ============================================================
# PostToolUse Hook: session-memory-extract
# 在文件写入操作后触发 session memory 提取
#
# 策略：
#   - 只在 write_file / patch 工具调用后触发（低开销）
#   - 使用 flock 防并发，同一时间只运行一个实例
#   - 后台运行，不阻塞主循环
#   - 间隔限制：至少间隔 15 分钟才再次触发
# ============================================================

set -euo pipefail

# ── 配置 ────────────────────────────────────────────────
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HOOK_LOCK="$HERMES_HOME/.session_memory_hook.lock"
HOOK_THROTTLE="$HERMES_HOME/.session_memory_hook_throttle"
EXTRACT_SCRIPT="$HERMES_HOME/scripts/session_memory_extract.py"
THROTTLE_SECONDS=${SESSION_MEMORY_THROTTLE:-900}  # 默认 15 分钟

# ── 只对文件写入工具触发 ────────────────────────────────
TOOL_NAME="${HOOK_EVENT_TOOL_NAME:-}"
if [ "$TOOL_NAME" != "write_file" ] && [ "$TOOL_NAME" != "patch" ]; then
    exit 0
fi

# ── 节流检查 ────────────────────────────────────────────
if [ -f "$HOOK_THROTTLE" ]; then
    last_run=$(cat "$HOOK_THROTTLE" 2>/dev/null || echo 0)
    now=$(date +%s)
    elapsed=$((now - last_run))
    if [ "$elapsed" -lt "$THROTTLE_SECONDS" ]; then
        # 未到节流时间，跳过
        exit 0
    fi
fi

# ── 执行提取（后台，flock 防并发）───────────────────────
(
    # 获取独占锁，失败则说明已有实例在运行
    exec 200>"$HOOK_LOCK"
    if ! flock -n 200; then
        exit 0  # 已有实例在运行，静默退出
    fi

    # 更新节流时间戳
    date +%s > "$HOOK_THROTTLE"

    # 运行提取（只处理最近 1 小时）
    if [ -f "$EXTRACT_SCRIPT" ]; then
        python3 "$EXTRACT_SCRIPT" run --since 1h >> "$HERMES_HOME/logs/session_memory_extract.log" 2>&1
    fi
) &

# 总是返回 0，不阻塞主循环
exit 0