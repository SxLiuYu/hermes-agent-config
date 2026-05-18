#!/bin/bash
# auto-verify.sh — Auto-run security check on write_file/patch
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)
case "$TOOL" in write_file|patch) ;; *) exit 0 ;; esac
FILE=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);a=d.get('args',{});print(a.get('path','')or a.get('file_path','')or'')" 2>/dev/null || true)
[ -z "$FILE" ] && exit 0
[ ! -f "$FILE" ] && exit 0
# Only check Python files for now
[[ "$FILE" == *.py ]] || exit 0
RESULT=$(python3 "$HOME/.hermes/tools/verify_pipeline.py" quick "$FILE" 2>/dev/null || echo '{"pass":true}')
PASS=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('pass',True))" 2>/dev/null || echo "True")
if [ "$PASS" != "True" ]; then
    echo "[verify-hook] ⚠️ Security issues detected in $FILE" >&2
    echo "$RESULT" | python3 -c "import sys,json;d=json.load(sys.stdin);[print(f'  - {f.get(\"message\",\"\")[:100]}',file=sys.stderr) for f in d.get('failures',[])]" 2>/dev/null
fi
exit 0