#!/bin/bash
set -euo pipefail
umask 077
NJFU_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$NJFU_ROOT"
"$NJFU_ROOT/.venv/bin/python" -B "$NJFU_ROOT/power_agent.py" --resume
exec "$NJFU_ROOT/.venv/bin/python" -B "$NJFU_ROOT/public_run.py" --enable-live-booking --open-browser --power-managed
