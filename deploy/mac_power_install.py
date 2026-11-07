"""One native administrator grant for a fixed, root-owned wake-only helper."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from local_app.elapsed_clock import elapsed_time
from local_app.power import (AGENT_LABEL, INSTALL_ID, SYSTEM_DIR, WAKE_LABEL, PowerFiles,
                             native_installation, native_status)
from local_app.native_power import DAEMON_PATH, installation_info, registered, validate_paths, wake_confirmed


def owner(uid):
    return 'org.njfu.autolib.wake.' + str(uid)


def render_helper(config, uid, *, system_dir=SYSTEM_DIR, pmset=Path('/usr/bin/pmset'), run_uid=0, generation='0'*32):
    """Arguments are internal renderer/test inputs, never accepted by the web or CLI."""
    config = Path(config)
    q = shlex.quote
    ancestor_checks = '\n'.join(f'[ ! -L {q(str(path))} ] || invalid=true'
                               for path in (config, *config.parents))
    system_parents = ' '.join(q(str(path)) for path in Path(system_dir).parents)
    return f'''#!/bin/sh
# {INSTALL_ID}. Data-only input; no user code is ever executed as root.
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LC_ALL=C
export LC_ALL
umask 077
SYSTEM={q(str(system_dir))}
CONFIG={q(str(config))}
PMSET={q(str(pmset))}
OWNER={q(owner(uid))}
[ "$(/usr/bin/id -u)" = {run_uid} ] || exit 1
[ -d "$SYSTEM" ] && [ ! -L "$SYSTEM" ] || exit 1
[ "$(/usr/bin/stat -f '%u' "$SYSTEM")" = {run_uid} ] || exit 1
mode=$(/usr/bin/stat -f '%Lp' "$SYSTEM")
[ "$((0$mode & 022))" = 0 ] || exit 1
for path in {system_parents}; do
  [ -d "$path" ] && [ ! -L "$path" ] || exit 1
  actual=$(/usr/bin/stat -f '%u' "$path")
  [ "$actual" = 0 ] || [ "$actual" = {run_uid} ] || exit 1
  mode=$(/usr/bin/stat -f '%Lp' "$path")
  [ "$((0$mode & 022))" = 0 ] || exit 1
done
clock=$(TZ=Asia/Shanghai /bin/date '+%H%M%S')
if [ "$clock" -ge 65940 ] && [ "$clock" -le 70040 ]; then exit 0; fi
armed=false
next_wake=0
error=''
publish() {{
  part=$(/usr/bin/mktemp "$SYSTEM/.status.XXXXXX") || return
  /usr/bin/plutil -create xml1 "$part"
  /usr/libexec/PlistBuddy -c "Add :at integer $(/bin/date +%s)" "$part"
  /usr/libexec/PlistBuddy -c "Add :armed bool $armed" "$part"
  /usr/libexec/PlistBuddy -c "Add :next_wake integer $next_wake" "$part"
  /usr/libexec/PlistBuddy -c "Add :error string $error" "$part"
  /usr/libexec/PlistBuddy -c 'Add :generation string {generation}' "$part"
  /bin/chmod 644 "$part"
  /bin/mv -f "$part" "$SYSTEM/status.plist"
}}
trap publish EXIT
invalid=false
{ancestor_checks}
if [ ! -f "$CONFIG" ] || [ -L "$CONFIG" ]; then invalid=true; fi
if [ "$invalid" = false ]; then
  [ "$(/usr/bin/stat -f '%u' "$CONFIG")" = {uid} ] || invalid=true
  [ "$(/usr/bin/stat -f '%Lp' "$CONFIG")" = 600 ] || invalid=true
  [ "$(/usr/bin/stat -f '%l' "$CONFIG")" = 1 ] || invalid=true
  [ "$(/usr/bin/stat -f '%z' "$CONFIG")" -le 16384 ] || invalid=true
fi
enabled=false
paused=true
if [ "$invalid" = false ]; then
  enabled=$(/usr/bin/plutil -extract enabled raw -expect bool -o - "$CONFIG" 2>/dev/null) || invalid=true
  paused=$(/usr/bin/plutil -extract paused raw -expect bool -o - "$CONFIG" 2>/dev/null) || invalid=true
fi
if [ "$invalid" = true ]; then error=config_invalid; enabled=false; fi
if [ "$enabled" = true ] && [ "$paused" = false ]; then
  today=$(TZ=Asia/Shanghai /bin/date '+%Y-%m-%d')
  next_wake=$(TZ=Asia/Shanghai /bin/date -j -f '%Y-%m-%d %H:%M:%S' "$today 06:55:00" '+%s')
  if [ "$next_wake" -le "$(/bin/date +%s)" ]; then
    tomorrow=$(TZ=Asia/Shanghai /bin/date -v+1d '+%Y-%m-%d')
    next_wake=$(TZ=Asia/Shanghai /bin/date -j -f '%Y-%m-%d %H:%M:%S' "$tomorrow 06:55:00" '+%s')
  fi
fi
schedule=$("$PMSET" -g sched) || {{ error=schedule_read_failed; exit 0; }}
owned=$(printf '%s\n' "$schedule" | /usr/bin/awk -v owner="$OWNER" '
  index($0,"by \\047" owner "\\047") {{
    for(i=2;i<=NF;i++) if($i=="at" && $(i-1)=="wake") print $(i+1) " " $(i+2)
  }}')
kept=false
while IFS= read -r when; do
  [ -n "$when" ] || continue
  timestamp=$(/bin/date -j -f '%m/%d/%Y %H:%M:%S' "$when" '+%s' 2>/dev/null) || \
    timestamp=$(/bin/date -j -f '%m/%d/%y %H:%M:%S' "$when" '+%s' 2>/dev/null) || {{ error=schedule_parse_failed; exit 0; }}
  if [ "$timestamp" = "$next_wake" ] && [ "$kept" = false ]; then
    kept=true
  else
    cancel_when=$(/bin/date -r "$timestamp" '+%m/%d/%y %H:%M:%S')
    "$PMSET" schedule cancel wake "$cancel_when" "$OWNER" || {{ error=schedule_cancel_failed; exit 0; }}
  fi
done <<OWNED
$owned
OWNED
if [ "$next_wake" -gt 0 ] && [ "$kept" = false ]; then
  when=$(/bin/date -r "$next_wake" '+%m/%d/%y %H:%M:%S')
  "$PMSET" schedule wake "$when" "$OWNER" || {{ error=schedule_create_failed; exit 0; }}
fi
if [ "$next_wake" -gt 0 ]; then
  verify=$("$PMSET" -g sched) || {{ error=schedule_readback_failed; exit 0; }}
  wanted=$(/bin/date -r "$next_wake" '+%m/%d/%Y %H:%M:%S')
  short=$(/bin/date -r "$next_wake" '+%m/%d/%y %H:%M:%S')
  if printf '%s\n' "$verify" | /usr/bin/awk -v owner="$OWNER" -v full="$wanted" -v short="$short" '
    index($0,"by \\047" owner "\\047") && (index($0,"wake at " full) || index($0,"wake at " short)) {{found=1}}
    END {{exit !found}}'; then armed=true; else error=schedule_not_confirmed; fi
fi
'''


def daemon_plist(*, system_dir=SYSTEM_DIR):
    return {'Label': WAKE_LABEL, 'ProgramArguments': ['/bin/sh', str(Path(system_dir) / 'wake-helper.sh')],
            'RunAtLoad': True, 'StartInterval': 60, 'StartCalendarInterval': {'Minute': 50},
            'ProcessType': 'Background', 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def agent_plist(root):
    root = Path(root).resolve()
    return {'Label': AGENT_LABEL,
            'ProgramArguments': [str(root / '.venv/bin/python'), '-B', str(root / 'power_agent.py')],
            'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 5, 'ProcessType': 'Background', 'LimitLoadToSessionType': 'Aqua',
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def install_script(root, uid, *, system_dir=SYSTEM_DIR, daemon_path=DAEMON_PATH,
                   run_uid=0, launchctl=Path('/bin/launchctl'), generation=None):
    """Fixed native transaction. Path overrides are only used by unprivileged tests."""
    root, system_dir, daemon_path = Path(root).resolve(), Path(system_dir), Path(daemon_path)
    generation = generation or secrets.token_hex(16)
    if len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
        raise ValueError('Invalid installation generation')
    q = shlex.quote
    marker = plistlib.dumps({'installation': INSTALL_ID, 'root': str(root), 'uid': uid, 'generation': generation})
    files = [('installation.plist', marker, '644'),
             ('wake-helper.sh', render_helper(root / '.local-data/power/config.plist', uid,
                system_dir=system_dir, run_uid=run_uid, generation=generation).encode(), '755'),
             ('daemon.plist', plistlib.dumps(daemon_plist(system_dir=system_dir)), '644')]
    parents = sorted(set(system_dir.parents) | set(daemon_path.parents), key=lambda p: len(p.parts))
    parent_words = ' '.join(q(str(path)) for path in parents)
    script = f'''set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077
SYSTEM={q(str(system_dir))}
DAEMON={q(str(daemon_path))}
LAUNCHCTL={q(str(launchctl))}
[ "$(/usr/bin/id -u)" = {run_uid} ] || exit 1
trusted_dir() {{
  [ -d "$1" ] && [ ! -L "$1" ] || return 1
  actual=$(/usr/bin/stat -f '%u' "$1")
  [ "$actual" = 0 ] || [ "$actual" = {run_uid} ] || return 1
  mode=$(/usr/bin/stat -f '%Lp' "$1")
  [ "$((0$mode & 022))" = 0 ]
}}
trusted_file() {{
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  [ "$(/usr/bin/stat -f '%u' "$1")" = {run_uid} ] || return 1
  [ "$(/usr/bin/stat -f '%l' "$1")" = 1 ] || return 1
  mode=$(/usr/bin/stat -f '%Lp' "$1")
  [ "$((0$mode & 022))" = 0 ]
}}
for path in {parent_words}; do trusted_dir "$path" || exit 1; done
OLD_SYSTEM=false
OLD_DAEMON=false
OLD_LOADED=false
if [ -e "$SYSTEM" ] || [ -L "$SYSTEM" ]; then
  trusted_dir "$SYSTEM" || exit 1
  [ "$(/usr/bin/stat -f '%u' "$SYSTEM")" = {run_uid} ] || exit 1
  trusted_file "$SYSTEM/installation.plist" || exit 1
  trusted_file "$SYSTEM/wake-helper.sh" || exit 1
  [ "$(/usr/libexec/PlistBuddy -c 'Print :installation' "$SYSTEM/installation.plist")" = {q(INSTALL_ID)} ] || exit 1
  [ "$(/usr/libexec/PlistBuddy -c 'Print :root' "$SYSTEM/installation.plist")" = {q(str(root))} ] || exit 1
  [ "$(/usr/libexec/PlistBuddy -c 'Print :uid' "$SYSTEM/installation.plist")" = {uid} ] || exit 1
  /usr/bin/head -n 2 "$SYSTEM/wake-helper.sh" | /usr/bin/grep -F {q(INSTALL_ID)} >/dev/null || exit 1
  OLD_SYSTEM=true
fi
if [ -e "$DAEMON" ] || [ -L "$DAEMON" ]; then
  [ "$OLD_SYSTEM" = true ] || exit 1
  trusted_file "$DAEMON" || exit 1
  [ "$(/usr/libexec/PlistBuddy -c 'Print :Label' "$DAEMON")" = {q(WAKE_LABEL)} ] || exit 1
  [ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$DAEMON")" = /bin/sh ] || exit 1
  [ "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' "$DAEMON")" = "$SYSTEM/wake-helper.sh" ] || exit 1
  if /usr/libexec/PlistBuddy -c 'Print :ProgramArguments:2' "$DAEMON" >/dev/null 2>&1; then exit 1; fi
  OLD_DAEMON=true
fi
if [ -e "$SYSTEM/previous" ] || [ -L "$SYSTEM/previous" ]; then
  trusted_dir "$SYSTEM/previous" || exit 1
  [ "$(/usr/bin/stat -f '%u' "$SYSTEM/previous")" = {run_uid} ] || exit 1
  for name in installation.plist wake-helper.sh daemon.plist; do
    if [ -e "$SYSTEM/previous/$name" ] || [ -L "$SYSTEM/previous/$name" ]; then trusted_file "$SYSTEM/previous/$name" || exit 1; fi
  done
fi
if "$LAUNCHCTL" print system/{WAKE_LABEL} >/dev/null 2>&1; then
  [ "$OLD_DAEMON" = true ] || exit 1
  OLD_LOADED=true
fi
STAGE=$(/usr/bin/mktemp -d {q(str(system_dir.parent / '.njfu-power-stage.XXXXXX'))})
/bin/mkdir -m 700 "$STAGE/new" "$STAGE/old"
STOPPED=false
COMPONENTS_CHANGED=false
DAEMON_CHANGED=false
SUCCESS=false
finish() {{
  code=$?
  trap - EXIT HUP INT TERM
  set +e
  if [ "$SUCCESS" != true ]; then
    if [ "$STOPPED" = true ]; then "$LAUNCHCTL" bootout system/{WAKE_LABEL} >/dev/null 2>&1; fi
    if [ "$OLD_SYSTEM" = true ] && [ "$COMPONENTS_CHANGED" = true ]; then
      /bin/cp -p "$STAGE/old/wake-helper.sh" "$SYSTEM/wake-helper.sh"
      /bin/cp -p "$STAGE/old/installation.plist" "$SYSTEM/installation.plist"
    fi
    if [ "$DAEMON_CHANGED" = true ]; then
      if [ "$OLD_DAEMON" = true ]; then /bin/cp -p "$STAGE/old/daemon.plist" "$DAEMON";
      elif trusted_file "$DAEMON"; then /bin/rm -f "$DAEMON"; fi
    fi
    if [ "$OLD_LOADED" = true ] && [ "$STOPPED" = true ]; then "$LAUNCHCTL" bootstrap system "$DAEMON" >/dev/null 2>&1; fi
  fi
  for name in installation.plist wake-helper.sh daemon.plist; do
    /bin/rm -f "$STAGE/new/$name" "$STAGE/old/$name"
  done
  /bin/rmdir "$STAGE/new" "$STAGE/old" "$STAGE" 2>/dev/null
  exit "$code"
}}
trap finish EXIT
trap 'exit 1' HUP INT TERM
if [ "$OLD_SYSTEM" = true ]; then
  /bin/cp -p "$SYSTEM/installation.plist" "$STAGE/old/installation.plist"
  /bin/cp -p "$SYSTEM/wake-helper.sh" "$STAGE/old/wake-helper.sh"
fi
if [ "$OLD_DAEMON" = true ]; then /bin/cp -p "$DAEMON" "$STAGE/old/daemon.plist"; fi
'''
    for name, payload, mode in files:
        encoded = base64.b64encode(payload).decode()
        script += f'''printf '%s' {q(encoded)} | /usr/bin/base64 -D > "$STAGE/new/{name}"
/bin/chmod {mode} "$STAGE/new/{name}"
'''
    script += f'''# phase: staged
if [ "$OLD_LOADED" = true ]; then "$LAUNCHCTL" bootout system/{WAKE_LABEL}; STOPPED=true; fi
# phase: bootout
COMPONENTS_CHANGED=true
if [ "$OLD_SYSTEM" = true ]; then
  /bin/mv -f "$STAGE/new/wake-helper.sh" "$SYSTEM/wake-helper.sh"
  /bin/mv -f "$STAGE/new/installation.plist" "$SYSTEM/installation.plist"
else
  /bin/mv "$STAGE/new/daemon.plist" "$STAGE/daemon.plist"
  /bin/chmod 755 "$STAGE/new"
  /bin/mv "$STAGE/new" "$SYSTEM"
  /bin/mkdir -m 700 "$STAGE/new"
  /bin/mv "$STAGE/daemon.plist" "$STAGE/new/daemon.plist"
fi
# phase: system-published
DAEMON_CHANGED=true
/bin/mv -f "$STAGE/new/daemon.plist" "$DAEMON"
# phase: daemon-published
"$LAUNCHCTL" enable system/{WAKE_LABEL}
"$LAUNCHCTL" bootstrap system "$DAEMON"
# phase: bootstrapped
if [ "$OLD_SYSTEM" = true ] && [ ! -e "$SYSTEM/previous" ]; then
  /bin/mkdir -m 700 "$SYSTEM/previous"
  /bin/cp -p "$STAGE/old/installation.plist" "$SYSTEM/previous/installation.plist"
  /bin/cp -p "$STAGE/old/wake-helper.sh" "$SYSTEM/previous/wake-helper.sh"
  if [ "$OLD_DAEMON" = true ]; then /bin/cp -p "$STAGE/old/daemon.plist" "$SYSTEM/previous/daemon.plist"; fi
fi
SUCCESS=true
'''
    return script


def register_agent(root, *, home=None, launchctl=Path('/bin/launchctl')):
    """User registration can be retried independently of the root component."""
    from local_app.native_power import trust_parents, trusted
    home = Path.home() if home is None else Path(home)
    path = home / 'Library/LaunchAgents' / (AGENT_LABEL + '.plist')
    path.parent.mkdir(exist_ok=True)
    trust_parents(path, os.getuid())
    expected = agent_plist(root)
    before = None
    if path.exists() or path.is_symlink():
        trusted(path, uid=os.getuid())
        before = path.read_bytes()
        if plistlib.loads(before) != expected:
            raise RuntimeError('已有同名用户代理不属于此安装')
    import tempfile
    def publish(payload):
        fd, temp = tempfile.mkstemp(prefix='.njfu-power-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    domain = 'gui/' + str(os.getuid())
    try:
        publish(plistlib.dumps(expected))
        loaded = subprocess.run([str(launchctl), 'print', domain + '/' + AGENT_LABEL], capture_output=True)
        if loaded.returncode:
            subprocess.run([str(launchctl), 'bootstrap', domain, str(path)], check=True, capture_output=True)
        else:
            subprocess.run([str(launchctl), 'kickstart', '-k', domain + '/' + AGENT_LABEL], check=True, capture_output=True)
    except Exception:
        if before is not None:publish(before)
        # A new valid, private plist remains identifiable and safely repairable.
        raise


def install(root=ROOT):
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise RuntimeError('请从登录中的Mac普通用户账户运行安装')
    root = Path(root).resolve()
    validate_paths(root, os.getuid())
    files = PowerFiles(root / '.local-data/power')
    files._base(create=True)
    fd = os.open(files.base / 'install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.path.insert(0, str(root))
        from power_agent import health
        current = health()
        if (not current or current.get('application') != 'NJFU-AutomaticLibrary-local-multi'
                or type(current.get('allow_live')) is not bool or current.get('public_enabled') is not True):
            raise RuntimeError('请先启动现有公网预约服务，再安装唤醒功能')
        before = files.config()
        pending = files.update({'enabled': True, 'paused': False, 'allow_live': current['allow_live']}, before['revision'])
        try:
            generation = secrets.token_hex(16)
            apple = 'on run argv\n do shell script (item 1 of argv) with administrator privileges\nend run'
            result = subprocess.run(['/usr/bin/osascript', '-e', apple, install_script(root, os.getuid(), generation=generation)],
                                    capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise RuntimeError('系统授权未完成或原生安装被拒绝；未改变全局睡眠设置')
            info = installation_info(root)
            if not info or info['generation'] != generation:
                raise RuntimeError('系统安装标记未通过校验')
            register_agent(root)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                native = native_status()
                if (wake_confirmed(native, time.time(), generation) and registered('system', WAKE_LABEL)
                        and registered('gui/' + str(os.getuid()), AGENT_LABEL)
                        and files.delegated(elapsed_time(), time.time())):
                    print('系统唤醒与后台守护已安装并通过读回校验。每天北京时间06:55唤醒；合盖仍受macOS限制。', flush=True)
                    return
                time.sleep(1)
            raise RuntimeError('安装组件已写入，唤醒或守护读回尚未确认，请检查管理员电源面板')
        except Exception:
            latest = files.config()
            if latest['revision'] == pending['revision']:
                files.update({key: before[key] for key in ('enabled', 'paused', 'allow_live')}, latest['revision'])
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.install:
            install()
        else:
            print(json.dumps({'installed': native_installation(ROOT), 'native': native_status()}, ensure_ascii=False))
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else '原生安装未完成，请检查权限与安装日志', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
