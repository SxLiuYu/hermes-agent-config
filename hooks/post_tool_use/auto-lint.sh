#!/bin/bash
# ============================================================
# PostToolUse Hook: auto-lint + auto-test gate
# 
# 对标 Aider: 每次 AI 编辑后自动跑 lint + test，输出错误让 agent 修复
# 
# 触发：write_file / patch 操作后
# 功能：
#   1. Python: ruff check + pytest（仅影响的测试文件）
#   2. Shell: shellcheck
#   3. JSON/YAML: 语法检查
#   4. 项目根检测：只在 Python 项目根目录的写操作触发 test
# ============================================================

set -euo pipefail

# ── 从 stdin 读取 tool event JSON ────────────────────────────
INPUT=$(cat 2>/dev/null || echo "{}")
TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || echo "")

# 只在写文件操作时触发
if [ "$TOOL" != "write_file" ] && [ "$TOOL" != "patch" ]; then
    exit 0
fi

# ── 获取被修改的文件路径 ──────────────────────────────────────
FILE_PATH=""
if [ "$TOOL" = "write_file" ]; then
    FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('args',{}).get('path',''))" 2>/dev/null || echo "")
elif [ "$TOOL" = "patch" ]; then
    FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('args',{}).get('path',''))" 2>/dev/null || echo "")
fi

[ -n "$FILE_PATH" ] || exit 0
[ -f "$FILE_PATH" ] || exit 0

EXT="${FILE_PATH##*.}"
HAD_ISSUES=0
LINT_OUTPUT=""

# ══════════════════════════════════════════════════════════════
# 1. Lint — 按文件类型选择合适的 linter
# ══════════════════════════════════════════════════════════════

case "$EXT" in
    py)
        # Ruff check (优先) 或 flake8 回退
        if command -v ruff &>/dev/null; then
            LINT_RESULT=$(ruff check "$FILE_PATH" 2>&1) || true
        elif command -v flake8 &>/dev/null; then
            LINT_RESULT=$(flake8 "$FILE_PATH" 2>&1) || true
        elif command -v python3 &>/dev/null; then
            # Python 语法检查（最快，零依赖）
            LINT_RESULT=$(python3 -m py_compile "$FILE_PATH" 2>&1) || true
        else
            LINT_RESULT=""
        fi

        if [ -n "$LINT_RESULT" ]; then
            HAD_ISSUES=1
            LINT_OUTPUT="$LINT_OUTPUT
### 🐍 Python Lint Issues in \`$FILE_PATH\`
\`\`\`
$LINT_RESULT
\`\`\`"
        fi

        # ── 2. Auto-fix: 尝试自动修复简单的 lint 问题 ──────────
        if [ $HAD_ISSUES -eq 1 ] && command -v ruff &>/dev/null; then
            ruff check --fix "$FILE_PATH" 2>/dev/null || true
        fi
        ;;

    sh|bash)
        if command -v shellcheck &>/dev/null; then
            LINT_RESULT=$(shellcheck "$FILE_PATH" 2>&1) || true
            if [ -n "$LINT_RESULT" ]; then
                HAD_ISSUES=1
                LINT_OUTPUT="$LINT_OUTPUT
### 🐚 Shell Lint Issues in \`$FILE_PATH\`
\`\`\`
$LINT_RESULT
\`\`\`"
            fi
        fi
        ;;

    json)
        if command -v python3 &>/dev/null; then
            LINT_RESULT=$(python3 -c "import json; json.load(open('$FILE_PATH'))" 2>&1) || true
            if [ -n "$LINT_RESULT" ]; then
                HAD_ISSUES=1
                LINT_OUTPUT="$LINT_OUTPUT
### 📋 JSON Syntax Error in \`$FILE_PATH\`
\`\`\`
$LINT_RESULT
\`\`\`"
            fi
        fi
        ;;

    yaml|yml)
        if command -v python3 &>/dev/null; then
            LINT_RESULT=$(python3 -c "import yaml; yaml.safe_load(open('$FILE_PATH'))" 2>&1) || true
            if [ -n "$LINT_RESULT" ]; then
                HAD_ISSUES=1
                LINT_OUTPUT="$LINT_OUTPUT
### 📄 YAML Syntax Error in \`$FILE_PATH\`
\`\`\`
$LINT_RESULT
\`\`\`"
            fi
        fi
        ;;
esac

# ══════════════════════════════════════════════════════════════
# 3. Test — 只在 Python 项目根目录触发（避免每次写都跑全量）
# ══════════════════════════════════════════════════════════════

if [ "$EXT" = "py" ] && { [ -f "pyproject.toml" ] || [ -f "setup.py" ] || [ -f "setup.cfg" ]; }; then
    # 找对应的测试文件
    TEST_FILE=""
    BASENAME=$(basename "$FILE_PATH" .py)

    # 尝试常见测试文件命名
    for candidate in \
        "tests/test_${BASENAME}.py" \
        "test/test_${BASENAME}.py" \
        "${FILE_PATH%/*}/test_${BASENAME}.py" \
        "tests/${FILE_PATH#*/}" \
        ; do
        if [ -f "$candidate" ]; then
            TEST_FILE="$candidate"
            break
        fi
    done

    if [ -n "$TEST_FILE" ] && command -v pytest &>/dev/null; then
        # 只跑对应的测试（轻量级，不阻塞）
        TEST_RESULT=$(python3 -m pytest "$TEST_FILE" -x -q --tb=short 2>&1) || true
        EXIT_CODE=$?

        if [ $EXIT_CODE -ne 0 ]; then
            HAD_ISSUES=1
            # 只取关键信息
            TEST_SUMMARY=$(echo "$TEST_RESULT" | grep -E "FAILED|ERROR|assert|short test summary" | head -15)
            LINT_OUTPUT="$LINT_OUTPUT
### 🧪 Test Failures in \`$TEST_FILE\`
\`\`\`
$TEST_SUMMARY
\`\`\`"
        fi
    fi
fi

# ══════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════

if [ $HAD_ISSUES -eq 1 ]; then
    echo ""
    echo "---"
    echo "## 🔧 Auto Gate: Lint + Test Results"
    echo "$LINT_OUTPUT"
    echo ""
    echo "> 💡 *Please fix these issues before continuing.*"
    echo ""
fi

exit 0