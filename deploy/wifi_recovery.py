"""Bounded Wi-Fi recovery policy; no booking or credential handling."""
from datetime import date, datetime, time
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time as time_module
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
CHECK_START = time(6, 55)
ACTION_END = time(6, 56, 30)
CHECK_END = time(6, 59)
RETRY_SECONDS = 20
SETTLE_SECONDS = 10
MAX_ACTIONS = 3


def valid_state(value):
    if not isinstance(value, dict) or set(value) != {
            'day', 'attempts', 'last_action', 'disconnected_since', 'last_wall'}:
        return False
    try:
        if date.fromisoformat(value['day']).isoformat() != value['day']:
            return False
    except (TypeError, ValueError):
        return False
    if type(value['attempts']) is not int or not 0 <= value['attempts'] <= MAX_ACTIONS:
        return False
    return all(item is None or (type(item) in (int, float) and math.isfinite(item))
               for item in (value['last_action'], value['disconnected_since'], value['last_wall']))


def portal_status(body, exit_code):
    """Classify only explicit portal evidence; never retain portal content."""
    if exit_code != 0:
        return 'unavailable'
    if not isinstance(body, bytes) or len(body) > 131072:
        return 'unknown'
    text = body.decode('gb18030', errors='replace')
    title = re.search(r'<title[^>]*>\s*([^<]{0,80})\s*</title>', text, re.I)
    if title and title.group(1).strip() == '注销页' and '登录成功' in text:
        return 'authenticated'
    if title and '登录' in title.group(1) and re.search(
            r'<input\b[^>]*\btype\s*=\s*["\']?password\b', text, re.I):
        return 'login_required'
    return 'unknown'


class RecoveryController:
    """Pure policy around an injected, bounded OS adapter and persisted state."""
    def __init__(self, adapter, target, state, *, clock=None, persist=None, guard=None):
        self.adapter, self.target, self.state = adapter, target, state
        self.clock = clock
        self.persist = persist or (lambda _: None)
        self.guard = guard or (lambda: True)

    def step(self, now, *, permitted):
        if not isinstance(now, datetime) or now.tzinfo is None:
            return {'state': 'invalid_clock'}
        now = now.astimezone(ZONE)
        local = now.time().replace(tzinfo=None)
        if permitted is not True or not CHECK_START <= local < CHECK_END:
            return {'state': 'disabled' if permitted is not True else 'outside_window'}
        day, timestamp = now.date().isoformat(), now.timestamp()
        if self.state and not valid_state(self.state):
            return {'state': 'state_invalid'}
        old_day = self.state.get('day')
        if old_day and old_day > day:
            return {'state': 'invalid_clock'}
        if old_day != day:
            self.state.clear()
            self.state.update(day=day, attempts=0, last_action=None, disconnected_since=None,
                              last_wall=None)
        last_wall = self.state.get('last_wall')
        if last_wall is not None and timestamp < last_wall:
            return {'state': 'invalid_clock'}
        self.state['last_wall'] = timestamp
        try:
            observed = self.adapter.observe()
            keys = ('radio', 'association', 'ipv4', 'portal', 'school')
            if not isinstance(observed, dict) or not all(k in observed for k in keys):
                raise ValueError('incomplete observation')
        except Exception:
            return {'state': 'observation_failed'}
        result = dict(observed) | {'state': 'observed', 'attempts': self.state['attempts']}
        disconnected = observed['association'] in ('disconnected', 'other')
        if observed['radio'] == 'off' or disconnected:
            if self.state['disconnected_since'] is None:
                self.state['disconnected_since'] = timestamp
        else:
            self.state['disconnected_since'] = None
        attempts, last = self.state['attempts'], self.state['last_action']
        due = last is None or timestamp - last >= RETRY_SECONDS
        settled = (self.state['disconnected_since'] is not None and
                   timestamp - self.state['disconnected_since'] >= SETTLE_SECONDS)
        action = ('enable' if observed['radio'] == 'off' else
                  'join' if observed['radio'] == 'on' and disconnected and settled else None)
        # The observation itself may take time; check the actual clock again.
        actual = self.clock() if self.clock else now
        if not isinstance(actual, datetime) or actual.tzinfo is None:
            return result | {'state': 'invalid_clock'}
        actual = actual.astimezone(ZONE)
        if (actual.date() != now.date() or actual.timestamp() < timestamp or
                not CHECK_START <= actual.time().replace(tzinfo=None) < ACTION_END):
            action = None
        if action and due and attempts < MAX_ACTIONS:
            self.state['attempts'] += 1
            self.state['last_action'] = timestamp
            try:
                # Commit the attempt before a system call, so a crash cannot reset the limit.
                self.persist(dict(self.state))
            except Exception:
                return result | {'state': 'state_write_failed'}
            try:
                actual = self.clock() if self.clock else now
                if (not isinstance(actual, datetime) or actual.tzinfo is None or
                    actual.astimezone(ZONE).date() != now.date() or
                    actual.timestamp() < timestamp or
                    not CHECK_START <= actual.astimezone(ZONE).time().replace(tzinfo=None) < ACTION_END or
                    self.guard() is not True):
                    return result | {'state': 'action_guard_failed', 'attempts': self.state['attempts']}
                actual = self.clock() if self.clock else now
                if (not isinstance(actual, datetime) or actual.tzinfo is None or
                    actual.astimezone(ZONE).date() != now.date() or actual.timestamp() < timestamp or
                    not CHECK_START <= actual.astimezone(ZONE).time().replace(tzinfo=None) < ACTION_END):
                    return result | {'state': 'action_guard_failed', 'attempts': self.state['attempts']}
            except Exception:
                return result | {'state': 'action_guard_failed', 'attempts': self.state['attempts']}
            try:
                ok = self.adapter.enable_wifi() if action == 'enable' else self.adapter.join(self.target)
            except Exception:
                ok = False
            result.update(action=action, action_ok=bool(ok), attempts=self.state['attempts'])
        return result


class SystemAdapter:
    """Read network state and perform only fixed, password-free system operations."""
    def __init__(self, target, portal, *, run=subprocess.run):
        if target != '@f-Young' or portal != 'http://10.11.2.3/':
            raise ValueError('unexpected network configuration')
        self.target, self.portal, self.run = target, portal, run

    def _run(self, args, timeout):
        try:
            return self.run(args, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def observe(self):
        power = self._run(['/usr/sbin/networksetup', '-getairportpower', 'en0'], 3)
        output = power.stdout.decode('utf-8', 'replace') if power and power.returncode == 0 else ''
        match = re.search(r'Wi-Fi Power \(en0\):\s*(On|Off)\s*$', output, re.I)
        radio = match.group(1).lower() if match else 'unknown'
        iface = self._run(['/sbin/ifconfig', 'en0'], 3)
        raw = iface.stdout.decode('utf-8', 'replace') if iface and iface.returncode == 0 else ''
        active = bool(re.search(r'^\s*status:\s*active\s*$', raw, re.M))
        inactive = bool(re.search(r'^\s*status:\s*inactive\s*$', raw, re.M))
        ipv4 = False
        for value in re.findall(r'^\s*inet\s+(\d+\.\d+\.\d+\.\d+)', raw, re.M):
            try:
                ip = ipaddress.IPv4Address(value)
                ipv4 = ipv4 or not (ip.is_link_local or ip.is_loopback or ip.is_unspecified)
            except ipaddress.AddressValueError:
                pass
        network = self._run(['/usr/sbin/networksetup', '-getairportnetwork', 'en0'], 3)
        raw_network = network.stdout.decode('utf-8', 'replace') if network and network.returncode == 0 else ''
        found = re.search(r'^Current Wi-Fi Network:\s*(.+?)\s*$', raw_network, re.M)
        if found:
            association = 'target' if found.group(1) == self.target else 'other'
        elif inactive and not ipv4:
            association = 'disconnected'
        else:
            # The OS can claim "not associated" when SSID access is redacted.
            association = 'unknown'
        portal, school = 'unavailable', 'unreachable'
        if radio == 'on' and active and ipv4:
            common = ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--noproxy', '*',
                      '--connect-timeout', '2', '--max-time', '3', '--max-redirs', '0']
            page = self._run(common + ['--interface', 'en0', '--max-filesize', '131072',
                                      '--write-out', '\nCODE:%{http_code}', self.portal], 4)
            if page is not None and page.returncode == 0:
                body, marker, code = page.stdout.rpartition(b'\nCODE:')
                portal = portal_status(body, 0) if marker and code == b'200' else 'unknown'
            result = self._run(common + ['--output', '/dev/null', '--write-out', '%{http_code}',
                                         'https://libseat.njfu.edu.cn/'], 4)
            if result is not None and result.returncode == 0 and result.stdout == b'200':
                school = 'reachable'
        return {'radio': radio, 'association': association, 'ipv4': ipv4,
                'portal': portal, 'school': school}

    def enable_wifi(self):
        result = self._run(['/usr/sbin/networksetup', '-setairportpower', 'en0', 'on'], 5)
        return result is not None and result.returncode == 0

    def join(self, ssid):
        if ssid != self.target:
            return False
        result = self._run(['/usr/sbin/networksetup', '-setairportnetwork', 'en0', ssid], 8)
        return result is not None and result.returncode == 0


def private_directory(base):
    base = Path(base).absolute()
    for path in (base, *base.parents):
        if path.is_symlink():
            raise ValueError('symlink in private path')
    info = base.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private directory required')
    return base


def private_read(base, name):
    base = private_directory(base)
    try:
        fd = os.open(base / name, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 262144):
            raise ValueError('private file required')
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('file too large')
    data = plistlib.loads(raw) if name.endswith('.plist') else json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('invalid private file')
    return data


class PrivateState:
    def __init__(self, base):
        self.base = Path(base).absolute()

    def read(self, name):
        if name not in ('state.json', 'status.json'):
            raise ValueError('unknown state file')
        return private_read(self.base, name)

    def write(self, name, value):
        base = private_directory(self.base)
        self.read(name)  # Reject an unsafe existing destination before replacement.
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > 262144:
            raise ValueError('state too large')
        fd, temporary = tempfile.mkstemp(prefix='.wifi-', dir=base)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, base / name)
            directory = os.open(base, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def lock(self):
        base = private_directory(self.base)
        fd = os.open(base / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+b') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError('unsafe lock')
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def continuous_time():
    if sys.platform != 'darwin':
        return time_module.monotonic()
    import ctypes
    class Timebase(ctypes.Structure):
        _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    library.mach_continuous_time.restype = ctypes.c_uint64
    library.mach_continuous_time.argtypes = []
    library.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
    library.mach_timebase_info.restype = ctypes.c_int
    base = Timebase()
    if library.mach_timebase_info(ctypes.byref(base)) or not base.denom:
        raise RuntimeError('continuous clock unavailable')
    return library.mach_continuous_time() * base.numer / base.denom / 1_000_000_000


class PowerGuard:
    """Fail closed on stale, unsafe, paused or invalid scheduler evidence."""
    def __init__(self, base, *, wall=time_module.time, elapsed=continuous_time):
        self.base, self.wall, self.elapsed = Path(base), wall, elapsed

    def __call__(self):
        try:
            config = private_read(self.base, 'config.plist')
            backend = private_read(self.base, 'backend.json')
            if (not isinstance(config, dict) or set(config) != {'revision', 'enabled', 'paused', 'allow_live'}
                    or type(config['revision']) is not int or config['revision'] < 1
                    or config['enabled'] is not True or config['paused'] is not False
                    or config['allow_live'] is not True or not isinstance(backend, dict)):
                return False
            if any(backend.get(k) is not True for k in ('scheduler_running', 'clock_ok', 'allow_live')):
                return False
            if any(backend.get(k) for k in ('error', 'last_error', 'scheduler_warning')):
                return False
            at, elapsed, pid = backend.get('at'), backend.get('elapsed'), backend.get('pid')
            if (type(at) not in (int, float) or not math.isfinite(at) or
                    type(elapsed) not in (int, float) or not math.isfinite(elapsed) or
                    type(pid) is not int or pid <= 1):
                return False
            wall_age, elapsed_age = self.wall() - at, self.elapsed() - elapsed
            if not (-1 <= wall_age <= 15 and 0 <= elapsed_age <= 15 and
                    abs(wall_age - elapsed_age) <= 2):
                return False
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, TypeError, OverflowError, KeyError, RuntimeError, plistlib.InvalidFileException):
            return False


def in_window(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    local = value.astimezone(ZONE).time().replace(tzinfo=None)
    return CHECK_START <= local < CHECK_END


def run_window(files, adapter, guard, *, now=lambda: datetime.now(ZONE), sleep=time_module.sleep):
    """Run only in the morning window. Store no portal content or credentials."""
    if not in_window(now()):
        return
    with files.lock():
        state = files.read('state.json') or {}
        controller = RecoveryController(adapter, '@f-Young', state, clock=now,
                                        persist=lambda value: files.write('state.json', value),
                                        guard=guard)
        # The iteration cap also terminates the worker if the wall clock moves backwards.
        for _ in range(30):
            current = now()
            if not in_window(current):
                break
            result = controller.step(current, permitted=guard())
            # Only the fixed, non-secret classification is persisted.
            status = {'checked_at': current.astimezone(ZONE).isoformat()} | result
            files.write('status.json', status)
            sleep(10)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Bounded @f-Young recovery; never books or authenticates.')
    parser.add_argument('action', choices=('run', 'inspect', 'status'))
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parent.parent
    adapter = SystemAdapter('@f-Young', 'http://10.11.2.3/')
    files = PrivateState(root / '.local-data/wifi-recovery')
    try:
        if args.action == 'inspect':
            print(json.dumps(adapter.observe(), ensure_ascii=False))
        elif args.action == 'status':
            print(json.dumps(files.read('status.json') or {'state': 'not_checked'}, ensure_ascii=False))
        else:
            # launchd schedules in the system timezone; refuse when local and Shanghai offsets differ.
            if datetime.now().astimezone().utcoffset() != datetime.now(ZONE).utcoffset():
                return 1
            run_window(files, adapter, PowerGuard(root / '.local-data/power'))
        return 0
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError, BlockingIOError):
        print('Wi-Fi recovery stopped: private state or runtime unavailable.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
