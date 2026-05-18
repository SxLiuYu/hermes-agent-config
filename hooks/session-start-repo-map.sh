#!/bin/bash
# SessionStart hook: auto-inject repo map into context
# Exit codes: 0=pass (output injected), 1=block (skip, not a codebase), 2=retry
set -euo pipefail

CWD="${HERMES_CWD:-$(pwd)}"
SCRIPT="$HOME/.hermes/scripts/repo_map.py"
CACHE_DIR="$HOME/.hermes/cache/repo-map"
CACHE_FILE="$CACHE_DIR/$(echo "$CWD" | md5).txt"
BUDGET="${REPO_MAP_BUDGET:-1500}"  # default 1500 tokens, override via env

# Only trigger for codebase directories
if [ ! -f "$SCRIPT" ]; then
    exit 1  # script not found, skip
fi

# Check if CWD has at least 5 Python/JS/TS files (is a codebase)
CODE_FILES=$(find "$CWD" -maxdepth 3 -name "*.py" -o -name "*.js" -o -name "*.ts" 2>/dev/null | head -20 | wc -l | tr -d ' ')
if [ "$CODE_FILES" -lt 5 ]; then
    exit 1  # not enough source files, skip
fi

# Skip excluded dirs
BASE=$(basename "$CWD")
EXCLUDES=".hermes .git node_modules venv .venv __pycache__"
for ex in $EXCLUDES; do
    if [ "$BASE" = "$ex" ]; then
        exit 1
    fi
done

# Use cached map if fresh (< 30 min)
mkdir -p "$CACHE_DIR"
if [ -f "$CACHE_FILE" ]; then
    AGE=$(($(date +%s) - $(stat -f %m "$CACHE_FILE" 2>/dev/null || stat -c %Y "$CACHE_FILE" 2>/dev/null)))
    if [ "$AGE" -lt 1800 ]; then
        cat "$CACHE_FILE"
        exit 0
    fi
fi

# Generate repo map (compact, for context injection)
MAP_OUTPUT=$(python3 "$SCRIPT" map "$CWD" --budget "$BUDGET" 2>/dev/null) || exit 1

# Extract the meaningful part (skip progress logs)
CLEAN=$(echo "$MAP_OUTPUT" | grep -v "^📂\|^  \|^🔗\|^📊" | head -40)

if [ -z "$CLEAN" ]; then
    exit 1
fi

# Save to cache
echo "$CLEAN" > "$CACHE_FILE"

# Output for context injection
cat <<CONTEXT
## 📂 Repo Map (PageRank)

$CLEAN

---

*Auto-generated. Refresh: delete $CACHE_FILE or run \`python3 ~/.hermes/scripts/repo_map.py map \"$CWD\"\`*
CONTEXT

exit 0