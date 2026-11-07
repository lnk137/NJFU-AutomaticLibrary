#!/bin/bash
set -euo pipefail
umask 077
NJFU_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
NJFU_SETUP="$NJFU_ROOT/.local-data/inbox-setup"
if [[ -L "$NJFU_SETUP" ]]; then
  echo '连接目录不能是符号链接。' >&2
  exit 1
fi
mkdir -p "$NJFU_SETUP"
chmod 700 "$NJFU_SETUP"
NJFU_SOCKET="$NJFU_SETUP/root.sock"
NJFU_ADMIN_KEYS="$HOME/.ssh/njfu-inbox-admin"
if [[ -e "$NJFU_ADMIN_KEYS" || -L "$NJFU_ADMIN_KEYS" ]]; then
  NJFU_PYTHON="$NJFU_ROOT/.venv/bin/python"
  NJFU_ADMIN_HELPER="$NJFU_ROOT/deploy/inbox_admin_connection.py"
  if [[ ! -x "$NJFU_PYTHON" || ! -f "$NJFU_ADMIN_HELPER" || -L "$NJFU_ADMIN_HELPER" ]]; then
    echo 'admin_key_helper_unavailable: 管理密钥工具不可用；未回退到密码。' >&2
    exit 1
  fi
  if "$NJFU_PYTHON" -I -B "$NJFU_ADMIN_HELPER" --connect; then
    echo '管理公钥连接已验证，可以返回 Codex。'
    exit 0
  fi
  echo 'admin_key_connect_failed_no_password_fallback: 管理密钥连接未确认；未回退到密码。' >&2
  exit 1
fi
if /usr/bin/ssh -S "$NJFU_SOCKET" -O check -o UpdateHostKeys=no -p 23486 root@183.66.27.20 >/dev/null 2>&1; then
  echo '已有本次维护连接，可以返回 Codex。'
  exit 0
fi
echo '请在此终端输入公网服务器的 SSH 密码；输入不会显示。'
echo '密码不会保存到文件，也不需要发送到聊天。'
/usr/bin/ssh -M -S "$NJFU_SOCKET" -o ControlMaster=yes -o ControlPersist=14400 \
  -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ConnectTimeout=10 -f -N -p 23486 root@183.66.27.20
/usr/bin/ssh -S "$NJFU_SOCKET" -O check -o UpdateHostKeys=no -p 23486 root@183.66.27.20
printf '维护连接已就绪。请回到 Codex 回复“已连接”。\n'
