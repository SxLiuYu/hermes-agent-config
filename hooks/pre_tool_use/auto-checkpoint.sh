#!/bin/bash
# Auto-checkpoint before file modifications — 对标 Gemini CLI
# 在 write_file / patch 前自动创建检查点

TOOL="$1"
FILE="$2"

case "$TOOL" in
  write_file|patch)
    # 提取文件路径
    TARGET=$(echo "$FILE" | sed 's/.*path[:=] *//' | tr -d '"'"'" | head -1)
    if [ -n "$TARGET" ] && [ -f "$TARGET" ]; then
      python3 "$HOME/.hermes/scripts/checkpoint.py" create \
        --reason "auto: $TOOL $TARGET" \
        --files "$TARGET" 2>&1 | tail -1
    fi
    ;;
esac

exit 0