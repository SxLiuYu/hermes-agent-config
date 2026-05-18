#!/bin/bash
# Deny-list: block rm -rf / --no-preserve-root etc.
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool',''))")
if [ "$TOOL" = "terminal" ]; then
  CMD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('args',{}).get('command',''))")
  if echo "$CMD" | grep -qE 'rm -rf /|rm -rf ~|> /dev/sda|mkfs\.'; then
    echo "BLOCKED: destructive command denied" >&2
    exit 1
  fi
fi
exit 0
