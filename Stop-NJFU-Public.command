#!/bin/bash
set -euo pipefail
NJFU_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
"$NJFU_ROOT/.venv/bin/python" -B "$NJFU_ROOT/power_agent.py" --pause
NJFU_PID="$(/usr/sbin/lsof -t -- "$NJFU_ROOT/.local-data/public/launcher.lock" 2>/dev/null || true)"
if [[ -z "$NJFU_PID" ]]; then
  echo '公网连接未运行。'
  exit 0
fi
if [[ ! "$NJFU_PID" =~ ^[0-9]+$ ]]; then
  echo '无法确定唯一后台进程，未停止任何程序。' >&2
  exit 1
fi
NJFU_COMMAND="$(ps -p "$NJFU_PID" -o command=)"
if [[ "$NJFU_COMMAND" != "$NJFU_ROOT/.venv/bin/python -B $NJFU_ROOT/public_run.py "* ]]; then
  echo '进程不属于本项目公网入口，未停止。' >&2
  exit 1
fi
kill -TERM "$NJFU_PID"
echo '已请求停止公网连接和本机调度；学校已有预约、配置与历史保留。'
