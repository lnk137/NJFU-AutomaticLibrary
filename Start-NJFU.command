#!/bin/bash
set -euo pipefail
umask 077
NJFU_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$NJFU_ROOT"
exec "$NJFU_ROOT/.venv/bin/python" -B "$NJFU_ROOT/local_run.py" --open-browser
