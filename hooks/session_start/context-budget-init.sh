#!/bin/bash
# SessionStart: 初始化上下文预算
set -euo pipefail
python3 "$HOME/.hermes/tools/context_budget.py" init 2>/dev/null
exit 0