#!/bin/bash
# Stop: 生成元认知会话报告
set -euo pipefail
python3 "$HOME/.hermes/tools/metacognition.py" report 2>/dev/null
exit 0