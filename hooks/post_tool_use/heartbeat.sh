#!/bin/bash
# Heartbeat: log every tool call with timestamp
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool',''))")
echo "[$(date '+%H:%M:%S')] $TOOL" >> ~/.hermes/logs/heartbeat.log
exit 0
