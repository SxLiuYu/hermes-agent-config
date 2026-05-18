#!/bin/bash
# SessionStart: 注入元认知策略指导
# 对标 SE-Agent: 每次会话开始时告诉 agent 应采取什么推理策略
set -euo pipefail
python3 "$HOME/.hermes/tools/metacognition.py" inject 2>/dev/null
exit 0