#!/bin/bash
# SessionStart: auto-generate repo map for codebase directories
# Injects PageRank-ranked codebase structure into context
# Exit 1 = skip (not a codebase), Exit 0 = inject output

CWD="${HERMES_CWD:-$(pwd)}"
SCRIPT="$HOME/.hermes/scripts/repo_map.py"
CACHE_DIR="$HOME/.hermes/cache/repo-map"
CACHE_FILE="$CACHE_DIR/$(echo "$CWD" | md5).txt"
BUDGET="${REPO_MAP_BUDGET:-1500}"

[ -f "$SCRIPT" ] || exit 1

# Must have at least 5 source files
CODE_FILES=$(find "$CWD" -maxdepth 3 \( -name "*.py" -o -name "*.js" -o -name "*.ts" \) 2>/dev/null | head -20 | wc -l | tr -d ' ')
[ "$CODE_FILES" -ge 5 ] || exit 1

# Skip excluded dirs
case "$(basename "$CWD")" in
  .hermes|.git|node_modules|venv|.venv|__pycache__) exit 1 ;;
esac

# Use cache if fresh (< 30 min)
mkdir -p "$CACHE_DIR"
if [ -f "$CACHE_FILE" ]; then
  AGE=$(($(date +%s) - $(stat -f %m "$CACHE_FILE" 2>/dev/null || stat -c %Y "$CACHE_FILE" 2>/dev/null)))
  [ "$AGE" -lt 1800 ] && cat "$CACHE_FILE" && exit 0
fi

# Generate repo map (stderr=progress, stdout=map)
MAP_OUTPUT=$(python3 "$SCRIPT" map "$CWD" --budget "$BUDGET" 2>/dev/null) || exit 1

# Strip only the header progress lines (📂, 🔗, 📊 and their indented children "  进度:")
CLEAN=$(echo "$MAP_OUTPUT" | grep -v "^📂\|^  进度\|^🔗\|^📊\|^  构建\|^  收敛\|^  完成")

[ -n "$CLEAN" ] || exit 1
echo "$CLEAN" > "$CACHE_FILE"

echo "## 📂 Repo Map (PageRank)"
echo ""
echo "$CLEAN"
echo ""
echo "*Auto-generated. Refresh: \`rm $CACHE_FILE\`*"
exit 0