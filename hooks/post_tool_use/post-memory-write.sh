#!/bin/bash
# =============================================================================
# post-memory-write.sh — Auto-commit memory changes after memory tool writes
# =============================================================================
# Triggered on PostToolUse. Reads JSON from stdin with {tool, args, result}.
# Detects memory tool writes, then git add + commit with descriptive message.
# Uses a .lock file to handle concurrent writes safely.
# =============================================================================

set -euo pipefail
INPUT=$(cat)

MEMORY_DIR="$HOME/.hermes/memory"
LOCK_FILE="$MEMORY_DIR/.git-memory.lock"
LOCK_TIMEOUT=10  # seconds to wait for lock

# --- Parse tool name from hook JSON ---
TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)

# Only act on memory tools
case "$TOOL" in
    memory_add|memory_replace|memory_remove|memory_merge|memory_set)
        ;;
    *)
        exit 0
        ;;
esac

# --- Extract action description for commit message ---
ACTION=$(echo "$TOOL" | sed 's/memory_//')
DESC=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    args = data.get('args', {})
    # Try common memory arg keys for a short description
    desc = args.get('content','') or args.get('key','') or args.get('query','') or ''
    # Truncate to 72 chars for commit message
    print(desc[:72].replace('\n',' '))
except:
    print('')
" 2>/dev/null || true)

# --- Acquire lock (non-blocking with timeout) ---
for ((i=0; i<LOCK_TIMEOUT; i++)); do
    if mkdir "$LOCK_FILE" 2>/dev/null; then
        trap 'rm -rf "$LOCK_FILE"' EXIT
        break
    fi
    sleep 1
done

if [ ! -d "$LOCK_FILE" ]; then
    echo "[memory-hook] WARNING: Could not acquire lock after ${LOCK_TIMEOUT}s, skipping commit" >&2
    exit 0
fi

# --- Commit changes ---
cd "$MEMORY_DIR"

# Only commit if there are actual changes (includes untracked files)
if [ -z "$(git status --porcelain)" ]; then
    echo "[memory-hook] No changes detected, skipping commit" >&2
    exit 0
fi

git add -A

# Build commit message
if [ -n "$DESC" ]; then
    MSG="memory: $ACTION — $DESC"
else
    MSG="memory: $ACTION"
fi

git commit -m "$MSG" >&2
echo "[memory-hook] Committed: $MSG" >&2

exit 0