"""Private Mac power policy and read-only evidence, independent of booking workers."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .elapsed_clock import elapsed_time
from .models import AppError, ZONE, resolve_runs, validate_plan
from .native_power import (AGENT_LABEL, INSTALL_ID, SYSTEM_DIR, WAKE_LABEL,
                           installation_info, native_installation, native_status, registered, wake_confirmed)

WINDOW_START = 6 * 3600 + 55 * 60
WINDOW_END = 7 * 3600 + 5 * 60


def seconds(wall):
    value = datetime.fromtimestamp(wall, ZONE)
    return value.hour * 3600 + value.minute * 60 + value.second


def quiet(wall):
    return 6 * 3600 + 59 * 60 + 50 <= seconds(wall) <= 7 * 3600 + 40


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def fresh(data, wall, elapsed, *, maximum=15):
    if not isinstance(data, dict) or not all(finite(data.get(k)) for k in ('at', 'elapsed')):
        return False
    wall_age, elapsed_age = wall - data['at'], elapsed - data['elapsed']
    return (-1 <= wall_age <= maximum and 0 <= elapsed_age <= maximum
            and abs(wall_age - elapsed_age) <= 2)


def alive(pid):
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class Decision:
    hold: bool
    reason: str


def decide(wall, elapsed, config, snapshot):
    if config.get('enabled') is not True or config.get('paused') is True:
        return Decision(False, '已停用或暂停本软件的时段保护')
    current = seconds(wall)
    if WINDOW_START <= current < WINDOW_END:
        return Decision(True, '06:55–07:05 预约保护时段')
    if isinstance(snapshot, dict) and (snapshot.get('busy', 0) or snapshot.get('active', 0)):
        return Decision(True, '本机仍有账号任务执行中')
    if current < WINDOW_START:
        return Decision(False, '等待下一次06:55唤醒')
    if not fresh(snapshot, wall, elapsed):
        return Decision(True, '后台证据过期或时钟变化，继续保护')
    if snapshot.get('date') != datetime.fromtimestamp(wall, ZONE).date().isoformat():
        return Decision(True, '尚未获得本轮执行结果')
    if (snapshot.get('error') or snapshot.get('last_error') or snapshot.get('scheduler_warning')
            or snapshot.get('scheduler_running') is not True or snapshot.get('clock_ok') is not True
            or snapshot.get('allow_live') is not True):
        return Decision(True, '后台、时钟或调度状态需检查')
    for name in ('busy', 'active', 'uncertain'):
        value = snapshot.get(name)
        if type(value) is not int or value != 0:
            return Decision(True, '仍有执行中或待核对任务')
    expected = snapshot.get('expected')
    if not isinstance(expected, list) or any(value != 'success' for value in expected):
        return Decision(True, '本轮存在未完成、失败或待核对任务')
    return Decision(False, '本轮任务已结束且后台正常，允许系统按原设置休眠')


class PowerFiles:
    """Atomic private files; reading an uninstalled feature has no side effects."""
    def __init__(self, base):
        self.base = Path(base)

    def _base(self, create=False):
        for path in (self.base, *self.base.parents):
            if path.is_symlink():
                raise AppError('unsafe_power', '电源状态路径不能为符号链接', 503)
        if create:
            self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.base.exists():
            info = self.base.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AppError('unsafe_power', '电源状态目录须由当前用户私有持有', 503)

    def read(self, name):
        if name not in {'config.plist', 'agent.json', 'backend.json', 'runner.json'}:
            raise ValueError('Unknown power file')
        self._base()
        path = self.base / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise AppError('unsafe_power', '无法安全读取电源状态文件', 503) from None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 262144):
                raise AppError('unsafe_power', '电源状态文件须为当前用户的0600普通文件', 503)
            try:
                result = plistlib.load(stream) if name.endswith('.plist') else json.load(stream)
            except (ValueError, plistlib.InvalidFileException):
                raise AppError('invalid_power', '电源状态文件无效', 503) from None
        if not isinstance(result, dict):
            raise AppError('invalid_power', '电源状态格式无效', 503)
        return result

    def config(self):
        value = self.read('config.plist')
        if value is None:
            return {'revision': 0, 'enabled': False, 'paused': False, 'allow_live': False}
        if (set(value) != {'revision', 'enabled', 'paused', 'allow_live'}
                or type(value['revision']) is not int or value['revision'] < 1
                or any(type(value[key]) is not bool for key in ('enabled', 'paused', 'allow_live'))):
            raise AppError('invalid_power', '电源配置无效', 503)
        return value

    def write(self, name, value):
        if name not in {'config.plist', 'agent.json', 'backend.json', 'runner.json'}:
            raise ValueError('Unknown power file')
        self._base(create=True)
        # Refuse a foreign/symlink destination before creating any staging file.
        self.read(name)
        payload = plistlib.dumps(value) if name.endswith('.plist') else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > 262144:
            raise AppError('invalid_power', '电源状态文件过大')
        fd, temp = tempfile.mkstemp(prefix='.power-', dir=self.base)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.base / name)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def update(self, changes, revision):
        if (not isinstance(changes, dict) or not changes or set(changes) - {'enabled', 'paused', 'allow_live'}
                or any(type(value) is not bool for value in changes.values())
                or type(revision) is not int or revision < 0):
            raise AppError('invalid_power', '请提交有效的电源开关与版本')
        self._base(create=True)
        fd = os.open(self.base / 'config.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.config()
            if current['revision'] != revision:
                raise AppError('power_conflict', '电源配置已更新，请刷新后重试', 409)
            result = current | changes | {'revision': revision + 1}
            self.write('config.plist', result)
            return result

    def delegated(self, elapsed, wall):
        config = self.config()
        if not config['enabled'] or config['paused']:
            return False
        agent = self.read('agent.json')
        maximum = 75 if quiet(wall) and isinstance(agent, dict) and agent.get('hold') is True else 15
        return bool(agent and agent.get('ready') is True and alive(agent.get('pid'))
                    and fresh(agent, wall, elapsed, maximum=maximum))


class PowerService:
    def __init__(self, settings, accounts, store, scheduler, catalog, *, status_provider=None):
        self.settings, self.accounts, self.store = settings, accounts, store
        self.scheduler, self.catalog = scheduler, catalog
        self.status_provider = status_provider or scheduler.status
        self.files = PowerFiles(Path(settings.data_dir) / 'power')
        self.stopping, self.thread = threading.Event(), None
        self.install_process = None
        self.install_lock = threading.Lock()

    def snapshot(self, wall=None, elapsed=None):
        wall = time.time() if wall is None else wall
        elapsed = elapsed_time() if elapsed is None else elapsed
        now = datetime.fromtimestamp(wall, ZONE)
        lower = datetime.combine(now.date(), datetime.min.time(), ZONE).timestamp() + WINDOW_START
        upper = lower + WINDOW_END - WINDOW_START
        result = {'at': wall, 'elapsed': elapsed, 'date': now.date().isoformat(), 'pid': os.getpid(),
                  'busy': len(self.accounts.busy), 'active': 0, 'uncertain': 0, 'expected': [], 'details': [], 'error': None}
        try:
            result.update(self.status_provider())
        except Exception:
            result.update(ready_to_submit=False, error='执行状态暂不可核对；保留防休眠保护',
                          scheduler_warning='执行状态暂不可核对，请检查后台诊断')
        try:
            query = {'spec.due_at': {'$gte': lower, '$lt': upper}, 'spec.kind': {'$ne': 'manual'}}
            rows = list(self.store.runs.find(query, {'status': 1, 'spec': 1, 'account_id': 1,
                'target_date': 1, 'dual_binding': 1, 'dual_task': 1, 'dual_result': 1,
                'native_binding': 1, 'dual_progress': 1, 'dual_mirror': 1,
                'dual_reconciliation': 1, 'result': 1}).limit(5001).max_time_ms(750))
            if len(rows) > 5000:
                raise ValueError('Too many power outcome rows')
            by_key = {row['_id']: row for row in rows}
            expected = {key: row.get('status', 'missing') for key, row in by_key.items()}
            for account in self.store.list_accounts():
                if not account.plan or account.plan.get('enabled') is not True:
                    continue
                plan = validate_plan(account.plan, self.catalog)
                runs = resolve_runs(account.id, account.revision, plan, now.date(), account.date_exceptions)
                for run in runs:
                    if not lower <= run.due_at < upper:
                        continue
                    row = by_key.get(run.key)
                    expected_key = run.key
                    matched_dual = False
                    gate = getattr(self.store, 'managed_gate', None)
                    if run.operation_id is None and gate is not None and gate.is_managed(account.id):
                        from .dual_node.result_bridge import dual_power_row
                        mirrored = dual_power_row(rows, run,
                            credential_revision=getattr(account, 'credential_revision', None),
                            account={'_id': account.id, 'student_id': account.student_id})
                        if mirrored is not None:
                            # Keep old history untouched; only the power projection
                            # uses the matching managed execution's current outcome.
                            for key, previous in by_key.items():
                                spec = previous.get('spec', {})
                                if (previous.get('account_id') == account.id
                                        and spec.get('target_date') == run.target_date
                                        and spec.get('due_at') == run.due_at
                                        and spec.get('kind') == 'scheduled'):
                                    expected.pop(key, None)
                            row, expected_key = mirrored, mirrored['_id']
                            matched_dual = True
                    state = row.get('status', 'missing') if row and (matched_dual or row.get('spec', {}).get('revision') == run.revision) else 'missing'
                    expected[expected_key] = state
                    result['details'].append({'student_id': account.student_id, 'name': account.alias,
                                              'begin': run.begin, 'end': run.end, 'state': state})
            result['expected'] = list(expected.values())
            result['active'] = self.store.runs.count_documents({'status': {'$in': ['preparing', 'submitting']}}, maxTimeMS=750)
            result['uncertain'] = self.store.runs.count_documents({'status': 'uncertain'}, maxTimeMS=750)
        except Exception:
            result['error'] = '无法完整核对本轮任务；保留防休眠保护'
        return result

    def status(self):
        config = self.files.config()
        agent, backend, runner = self.files.read('agent.json'), self.files.read('backend.json'), self.files.read('runner.json')
        wall, elapsed = time.time(), elapsed_time()
        installed = sys.platform == 'darwin' and native_installation(self.settings.root)
        info = installation_info(self.settings.root) if installed else None
        native = native_status() if installed else {}
        native_registered = installed and registered('system', WAKE_LABEL)
        native_fresh = bool(info and native_registered and wake_confirmed(native, wall, info['generation']))
        agent_fresh = bool(fresh(agent, wall, elapsed, maximum=75 if quiet(wall) else 15)
                           and alive((agent or {}).get('pid')))
        install_exit = self.install_process.poll() if self.install_process is not None else None
        return {'supported': sys.platform == 'darwin', 'installed': installed, 'config': config,
                'window': {'timezone': 'Asia/Shanghai', 'wake': '06:55', 'release': '07:00', 'review': '07:05'},
                'agent_online': self.files.delegated(elapsed, wall), 'agent': agent,
                'agent_fresh': agent_fresh, 'native_fresh': native_fresh,
                'native_registered': native_registered,
                'runner': runner, 'runner_fresh': bool(fresh(runner, wall, elapsed,
                    maximum=75 if quiet(wall) else 15) and alive((runner or {}).get('pid'))),
                'backend_fresh': fresh(backend, wall, elapsed), 'backend': backend,
                'native': native,
                'installing': self.install_process is not None and install_exit is None,
                'install_error': '系统安装或授权未完成，请重试安装并核对原生提示' if install_exit not in (None, 0) else None}

    def update(self, data):
        if not isinstance(data, dict) or set(data) - {'enabled', 'paused', 'expected_revision'}:
            raise AppError('invalid_power', '只能调整本软件的唤醒与保护开关')
        if sys.platform != 'darwin' or not native_installation(self.settings.root):
            raise AppError('power_not_installed', '请先安装系统唤醒组件', 409)
        changes = {key: value for key, value in data.items() if key != 'expected_revision'}
        self.files.update(changes, data.get('expected_revision'))
        return self.status()

    def install(self):
        if sys.platform != 'darwin':
            raise AppError('unsupported_power', '自动唤醒仅支持本机macOS')
        with self.install_lock:
            if self.install_process is None or self.install_process.poll() is not None:
                self.files._base(create=True)
                log = self.files.base / 'install.log'
                fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'ab') as stream:
                    self.install_process = subprocess.Popen([sys.executable, '-B',
                        str(self.settings.root / 'deploy/mac_power_install.py'), '--install'],
                        cwd=self.settings.root, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)
        return self.status()

    def start(self):
        def loop():
            while not self.stopping.is_set():
                try:
                    self.sample()
                except Exception:
                    # Power evidence failure must never pause or kill the scheduler.
                    pass
                self.stopping.wait(5)
        self.thread = threading.Thread(target=loop, name='njfu-power-evidence', daemon=True)
        self.thread.start()

    def sample(self, wall=None):
        wall = time.time() if wall is None else wall
        if self.files.config()['enabled'] and not quiet(wall):
            evidence = self.snapshot(wall)
            if not quiet(time.time()):
                self.files.write('backend.json', evidence)

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=3)
