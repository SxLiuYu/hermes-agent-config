#!/bin/bash
# Stop: 生成健康仪表盘
python3 /Users/sxliuyu/.hermes/tools/health_dashboard.py collect 2>/dev/null
python3 /Users/sxliuyu/.hermes/tools/health_dashboard.py report 2>/dev/null
exit 0