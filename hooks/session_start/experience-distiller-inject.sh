#!/bin/bash
set -euo pipefail
python3 /Users/sxliuyu/.hermes/tools/experience_distiller.py inject --context "session start" 2>/dev/null || true
exit 0
