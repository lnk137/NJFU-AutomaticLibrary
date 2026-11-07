"""Unprivileged wake-window supervisor. Never submits or cancels school bookings."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from local_app.elapsed_clock import elapsed_time
from local_app.power import PowerFiles, Decision, alive, decide, native_installation, quiet, seconds
from local_app.native_power import native_ready


def health():
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:5003/health', timeout=1) as response:
            result = json.loads(response.read(8192))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def network_probe():
    # A bounded, verified TLS GET; no proxy, cookie, login, redirect or school POST.
    try:
        result = subprocess.run(['/usr/bin/curl', '--disable', '--silent', '--show-error',
            '--noproxy', '*', '--connect-timeout', '1', '--max-time', '2', '--max-redirs', '0',
            '--output', '/dev/null', '--write-out', '%{http_code}', 'https://libseat.njfu.edu.cn/'],
            capture_output=True, text=True, timeout=3)
        code = int(result.stdout) if result.stdout.isdigit() else 0
        return {'ok': result.returncode == 0 and 200 <= code < 500,
                'http_status': code, 'message': '校园座位网站可达' if result.returncode == 0 and 200 <= code < 500 else '校园网络或学校网站尚未恢复'}
    except (OSError, subprocess.TimeoutExpired):
        return {'ok': False, 'message': '校园网络检查超时，将有限重试'}


def system_probe():
    def read(command):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=1).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ''
    battery = read(['/usr/bin/pmset', '-g', 'batt'])
    lid = read(['/usr/sbin/ioreg', '-r', '-k', 'AppleClamshellState', '-d', '4'])
    assertions = read(['/usr/bin/pmset', '-g', 'assertions'])
    closed = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', lid)
    blockers = sorted(set(re.findall(r'pid \d+\(([^)]+)\)', assertions)))
    return {'ac': 'AC Power' in battery if battery else None,
            'lid_closed': closed.group(1) == 'Yes' if closed else None,
            'blockers': [value for value in blockers if value != 'caffeinate'][:12],
            'note': '只释放本软件的保护；系统及其他应用仍可能阻止休眠'}


class Supervisor:
    def __init__(self, root, *, files=None, spawn=subprocess.Popen, health=health,
                 probe=network_probe, system=system_probe, installed=native_installation, wake_check=None):
        self.root = Path(root).resolve()
        self.files = files or PowerFiles(self.root / '.local-data/power')
        self.spawn, self.health, self.probe, self.system, self.installed = spawn, health, probe, system, installed
        self.awake = self.launcher = None
        self.last_health = self.last_probe = self.last_system = self.last_write = -float('inf')
        self.service_ok = False
        self.cached_config = None
        self.last_pair = None
        self.network, self.environment = {}, {}
        self.last_state = None
        self.wake_check = wake_check or (lambda wall: native_ready(self.root, wall))
        self.last_native = self.last_recovery = -float('inf')
        self.wake_ok = False

    def _awake(self, wanted):
        if self.awake is not None and self.awake.poll() is not None:
            self.awake = None
        if wanted and self.awake is None:
            self.awake = self.spawn(['/usr/bin/caffeinate', '-is', '-w', str(os.getpid())], stdin=subprocess.DEVNULL)
        elif not wanted and self.awake is not None:
            self.awake.terminate()
            self.awake.wait(timeout=3)
            self.awake = None

    def _service(self, config, elapsed):
        if elapsed - self.last_health < 5:
            return
        self.last_health = elapsed
        result = self.health()
        self.service_ok = bool(result and result.get('application') == 'NJFU-AutomaticLibrary-local-multi'
            and result.get('public_enabled') is True and result.get('allow_live') is config['allow_live'])
        if self.service_ok:
            return
        if self.launcher is not None and self.launcher.poll() is None:
            return
        self._start_launcher(config)

    def _start_launcher(self, config, *, quiet_recovery=False):
        if self.launcher is not None and self.launcher.poll() is None:
            return
        command = [str(self.root / '.venv/bin/python'), '-B', str(self.root / 'public_run.py'), '--power-managed']
        if config['allow_live']:
            command.append('--enable-live-booking')
        if quiet_recovery:
            self.launcher = self.spawn(command, cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
        log_path = self.files.base / 'launcher.log'
        if log_path.exists() and not log_path.is_symlink() and log_path.stat().st_size > 1048576:
            os.replace(log_path, self.files.base / 'launcher.previous.log')
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as log:
            self.launcher = self.spawn(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)

    def step(self, wall, elapsed):
        # Hold the existing assertion without probes, DB reads, metadata writes or health traffic.
        if quiet(wall) or 6*3600+59*60+40 <= seconds(wall) <= 7*3600+40:
            self.cached_config = self.files.config()
            if self.cached_config and self.cached_config['enabled'] and not self.cached_config['paused']:
                if self.installed(self.root):
                    self._awake(True)
                    evidence = self.files.read('backend.json')
                    if (not evidence or not alive(evidence.get('pid'))) and elapsed-self.last_recovery>=5:
                        self._start_launcher(self.cached_config, quiet_recovery=True)
                        self.last_recovery = elapsed
            else:
                self._awake(False)
            return
        config = self.cached_config = self.files.config()
        enabled = config['enabled'] and not config['paused'] and self.installed(self.root)
        clock_jump = (self.last_pair is not None and
            abs((wall - self.last_pair[0]) - (elapsed - self.last_pair[1])) > 2)
        self.last_pair = (wall, elapsed)
        ready = False
        decision = Decision(False, '系统唤醒未启用，原预约服务保持原状')
        if enabled:
            if elapsed-self.last_native>=30:
                self.wake_ok = self.wake_check(wall)
                self.last_native = elapsed
            evidence = self.files.read('backend.json')
            if evidence and 'pid' in evidence and not alive(evidence['pid']):
                evidence = None
            decision = decide(wall, elapsed, config, evidence)
            if clock_jump:
                decision = Decision(True, '系统时钟变化，等待新证据后再评估')
            elif not self.wake_ok:
                decision = Decision(True, '系统唤醒读回未确认，继续保护并等待修复')
            try:
                self._awake(decision.hold)
            except (OSError, subprocess.SubprocessError):
                decision = Decision(True, '防休眠断言未建立，原启动器继续独立保护')
            try:
                self._service(config, elapsed)
            except (OSError, subprocess.SubprocessError):
                self.service_ok = False
            ready = self.service_ok and (not decision.hold or self.awake is not None)
            current = seconds(wall)
            if elapsed - self.last_system >= 30 and not 6 * 3600 + 59 * 60 + 40 <= current <= 7 * 3600 + 45:
                self.environment = self.system()
                self.last_system = elapsed
            if 6 * 3600 + 55 * 60 <= current < 6 * 3600 + 59 * 60 + 40 and elapsed - self.last_probe >= 15:
                self.network = self.probe() | {'checked_at': wall}
                self.last_probe = elapsed
        else:
            self._awake(False)
        state_key = (ready, self.awake is not None, decision.reason)
        if elapsed - self.last_write >= 5 or self.last_state != state_key:
            self.files.write('agent.json', {'pid': os.getpid(), 'at': wall, 'elapsed': elapsed,
                'ready': ready, 'hold': self.awake is not None, 'reason': decision.reason,
                'service_online': self.service_ok if enabled else False,
                'network': self.network, 'system': self.environment})
            self.last_write = elapsed
            self.last_state = state_key

    def close(self):
        self._awake(False)


def control(action, root=ROOT):
    files = PowerFiles(Path(root) / '.local-data/power')
    config = files.config()
    if config['revision'] == 0:
        return  # An uninstalled feature never changes legacy launcher behavior.
    files.update({'paused': action == 'pause'}, config['revision'])


def main():
    parser = argparse.ArgumentParser()
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--pause', action='store_true')
    flags.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.pause or args.resume:
        control('pause' if args.pause else 'resume')
        return
    files = PowerFiles(ROOT / '.local-data/power')
    files._base(create=True)
    fd = os.open(files.base / 'agent.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        stopping = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: stopping.set())
        supervisor = Supervisor(ROOT, files=files)
        try:
            while not stopping.is_set():
                try:
                    supervisor.step(time.time(), elapsed_time())
                except Exception:
                    # Leave the runner to use its independent fallback lease on stale evidence.
                    pass
                stopping.wait(1)
        finally:
            supervisor.close()


if __name__ == '__main__':
    main()
