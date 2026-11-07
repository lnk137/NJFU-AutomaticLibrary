"""Read-only native trust and wake evidence checks, with no privileged side effects."""
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess

from .models import ZONE

SYSTEM_DIR = Path('/Library/Application Support/NJFUAutoLib')
DAEMON_PATH = Path('/Library/LaunchDaemons/org.njfu.autolib.wake.plist')
INSTALL_ID = 'NJFU-AUTO-LIB-mac-power-v1'
WAKE_LABEL = 'org.njfu.autolib.wake'
AGENT_LABEL = 'org.njfu.autolib.power'


def trusted(path, *, directory=False, uid=0):
    path = Path(path)
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):
        raise RuntimeError('原生组件路径的类型、owner或权限不可信')


def trust_parents(path, uid=0):
    for parent in Path(path).parents:
        info = parent.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, uid} or info.st_mode & 0o022):
            raise RuntimeError('原生组件父路径不可信')


def marker_matches(data, root, user_uid):
    return (isinstance(data, dict) and set(data) == {'installation', 'root', 'uid', 'generation'}
            and data['installation'] == INSTALL_ID and data['root'] == str(Path(root).resolve())
            and type(data['uid']) is int and data['uid'] == user_uid
            and isinstance(data['generation'], str) and re.fullmatch('[0-9a-f]{32}', data['generation']))


def validate_paths(root, user_uid, *, system_dir=SYSTEM_DIR, daemon_path=DAEMON_PATH, uid=0):
    """Allow an absent installation or a marked partial installation; reject foreign paths."""
    system_dir, daemon_path = Path(system_dir), Path(daemon_path)
    trust_parents(system_dir, uid)
    trust_parents(daemon_path, uid)
    if not system_dir.exists() and not system_dir.is_symlink():
        if daemon_path.exists() or daemon_path.is_symlink():
            raise RuntimeError('同名系统任务不属于此安装')
        return None
    trusted(system_dir, directory=True, uid=uid)
    marker = system_dir / 'installation.plist'
    helper = system_dir / 'wake-helper.sh'
    trusted(marker, uid=uid)
    trusted(helper, uid=uid)
    data = plistlib.loads(marker.read_bytes())
    if not marker_matches(data, root, user_uid) or INSTALL_ID not in helper.read_text()[:200]:
        raise RuntimeError('已有原生组件不属于此安装')
    if daemon_path.exists() or daemon_path.is_symlink():
        trusted(daemon_path, uid=uid)
        daemon = plistlib.loads(daemon_path.read_bytes())
        if daemon.get('Label') != WAKE_LABEL or daemon.get('ProgramArguments') != ['/bin/sh', str(helper)]:
            raise RuntimeError('已有同名系统任务身份不符')
    for directory in (system_dir / 'previous',):
        if directory.exists() or directory.is_symlink():
            trusted(directory, directory=True, uid=uid)
            for name in ('installation.plist', 'wake-helper.sh', 'daemon.plist'):
                file = directory / name
                if file.exists() or file.is_symlink():
                    trusted(file, uid=uid)
    return data


def installation_info(root):
    try:
        data = validate_paths(root, os.getuid())
        return data if data and DAEMON_PATH.is_file() else None
    except (OSError, RuntimeError, ValueError, plistlib.InvalidFileException):
        return None


def native_installation(root):
    return installation_info(root) is not None


def native_status():
    try:
        trust_parents(SYSTEM_DIR / 'status.plist')
        trusted(SYSTEM_DIR, directory=True)
        path = SYSTEM_DIR / 'status.plist'
        trusted(path)
        if path.stat().st_size > 8192:
            return {}
        value = plistlib.loads(path.read_bytes())
        return {key: value[key] for key in ('at', 'armed', 'next_wake', 'error', 'generation') if key in value}
    except (OSError, RuntimeError, ValueError, plistlib.InvalidFileException):
        return {}


def expected_wake(wall):
    now = datetime.fromtimestamp(wall, ZONE)
    result = now.replace(hour=6, minute=55, second=0, microsecond=0)
    if result.timestamp() <= wall:
        result += timedelta(days=1)
    return result.timestamp()


def wake_confirmed(data, wall, generation):
    return bool(isinstance(data, dict) and data.get('armed') is True and data.get('error') in ('', None)
        and type(data.get('at')) in (int, float) and math.isfinite(data['at'])
        and -1 <= wall - data['at'] <= 130
        and type(data.get('next_wake')) in (int, float) and math.isfinite(data['next_wake'])
        and abs(data['next_wake'] - expected_wake(wall)) < 1
        and isinstance(generation, str) and re.fullmatch('[0-9a-f]{32}', generation)
        and data.get('generation') == generation)


def registered(domain, label):
    try:
        return subprocess.run(['/bin/launchctl', 'print', domain + '/' + label],
            capture_output=True, timeout=1).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def native_ready(root, wall):
    info = installation_info(root)
    return bool(info and wake_confirmed(native_status(), wall, info['generation'])
                and registered('system', WAKE_LABEL))
