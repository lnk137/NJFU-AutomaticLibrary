"""Own an Interactive launchd sender while retaining the controller lifeline."""
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PREFIX = 'org.codex.njfu.prodcandidate.sender.'


def peer_pid(sock):
    return int.from_bytes(sock.getsockopt(0, 2, 4), byteorder='little', signed=True)


def private_directory(path):
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or path.resolve() != path):
        raise ValueError('private fixture directory required')
    return info


def remove_owned(path, expected):
    """Unlink only the exact private file created for this sender."""
    try:info = path.lstat()
    except FileNotFoundError:return
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or (info.st_dev, info.st_ino) != expected):
        raise RuntimeError('owned launch file identity changed')
    try:path.unlink()
    except FileNotFoundError:pass  # The authenticated guardian may have removed it first.


def validate(command, runtime, expected_sha256, environment, *, root=None, allow_school=False):
    runtime = Path(runtime)
    private_directory(runtime)
    if len(command) != 8:
        raise ValueError('native command shape denied')
    binary = Path(command[0])
    try:info = binary.lstat()
    except OSError:raise ValueError('native binary identity denied') from None
    if (binary.is_symlink() or binary.resolve() != binary or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1
            or not os.access(binary, os.X_OK) or hashlib.sha256(binary.read_bytes()).hexdigest() != expected_sha256):
        raise ValueError('native binary identity denied')
    socket_path, state = Path(command[1]), Path(command[5])
    if socket_path.parent != runtime or state != runtime / 'ledger':
        raise ValueError('native path boundary denied')
    private_directory(state)
    if command[6] not in tuple(str(n) for n in range(1, 25)) or command[7] not in ('init', 'open'):
        raise ValueError('invalid native configuration')
    if command[2] == 'fixture':
        if root is None:
            raise ValueError('fixture isolation root required')
        root = Path(root)
        private_directory(root)
        if (root.parent != Path('/private/tmp') or not root.name.startswith('njfu-prodcandidate-')
                or root not in runtime.parents):
            raise ValueError('fixture root boundary denied')
        for parent in runtime.parents:
            if parent == root:
                break
            private_directory(parent)
        cert = Path(command[4])
        if (not command[3].isdigit() or not 1024 <= int(command[3]) <= 65535
                or int(command[3]) in (5003, 27019) or cert.is_symlink() or not cert.is_file()
                or root not in cert.resolve().parents):
            raise ValueError('fixture endpoint boundary denied')
        if not environment.get('NJFU_TEST_WALL_DUE_NS', '').isdigit():
            raise ValueError('fixture wall deadline required')
    elif command[2:] and (command[2] != 'school' or command[3] != '443' or command[4] != '-'
                           or not allow_school or root is not None):
        raise ValueError('verified school release required')


class BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ('flags','status','xstatus','pid','ppid','uid','gid',
                  'ruid','rgid','svuid','svgid','rfu1')] + [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + \
               [(name, ctypes.c_uint32) for name in ('nfiles','pgid','pjobc','tdev','tpgid')] + \
               [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]


def identity(pid):
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    info = BSDInfo()
    for attempt in range(5):
        length = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if length:
            break
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        if attempt < 4:
            time.sleep(.01)
    else:
        status = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=3)
        if status.returncode == 0 and status.stdout.strip().startswith('Z'):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        raise RuntimeError('live fixture process identity unavailable')
    if length != ctypes.sizeof(info) or info.pid != pid or info.uid != os.getuid():
        raise RuntimeError('fixture process identity mismatch')
    return (info.pid, info.start_sec, info.start_usec, info.ppid)


class ManagedPopen:
    """Small Popen-compatible lifecycle handle, backed by a private guardian."""
    def __init__(self, command, environment, runtime, *, expected_sha256, allow_school=False):
        self.args = list(command)
        self.runtime = Path(runtime)
        fixture_root = os.environ.get('NJFU_TEST_ISOLATION_ROOT')
        self.root = Path(fixture_root).resolve() if fixture_root else None
        allowed = ('NJFU_TEST_WALL_DUE_NS',)
        child_env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                     **{key: environment[key] for key in allowed if key in environment}}
        validate(self.args, self.runtime, expected_sha256, child_env, root=self.root, allow_school=allow_school)
        self.label = PREFIX + secrets.token_hex(10)
        self.domain = f'gui/{os.getuid()}'
        suffix = secrets.token_hex(4)
        self.owner_path = self.runtime / ('o' + suffix + '.sock')
        self.manifest = self.runtime / ('m' + suffix + '.json')
        self.plist = self.runtime / ('j' + suffix + '.plist')
        self.connection = None
        self.pid = None
        self.registered = False
        self._owned_files = {}
        self._returncode = None
        self._exit_buffer = bytearray()
        self.stderr = io.StringIO('')
        self.stdout = None
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.owner_path))
            listener.listen(1)
            listener.settimeout(12)
            nonce = secrets.token_hex(16)
            manifest = {'command': self.args, 'runtime': str(self.runtime), 'root': str(self.root) if self.root else None,
                        'expected_sha256': expected_sha256, 'allow_school': allow_school,
                        'environment': child_env, 'owner_socket': str(self.owner_path),
                        'owner_pid': os.getpid(), 'nonce': nonce, 'label': self.label}
            fd = os.open(self.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd); self._owned_files[self.manifest] = (info.st_dev, info.st_ino)
            with os.fdopen(fd, 'w') as stream:
                json.dump(manifest, stream)
            job = {'Label': self.label, 'ProgramArguments': [sys.executable, '-B', str(HERE / 'interactive_guardian.py'), str(self.manifest)],
                   'RunAtLoad': True, 'KeepAlive': False, 'ProcessType': 'Interactive',
                   'LimitLoadToSessionType': 'Aqua', 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
                   'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONDONTWRITEBYTECODE': '1'}}
            fd = os.open(self.plist, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd); self._owned_files[self.plist] = (info.st_dev, info.st_ino)
            with os.fdopen(fd, 'wb') as stream:
                plistlib.dump(job, stream)
            if subprocess.run(['/bin/launchctl','print',self.domain+'/'+self.label],capture_output=True).returncode == 0:
                raise RuntimeError('fixture sender label already exists')
            launch = subprocess.run(['/bin/launchctl','bootstrap',self.domain,str(self.plist)],capture_output=True,timeout=12)
            if launch.returncode:
                raise RuntimeError('fixture sender bootstrap failed')
            self.registered = True
            self.connection, _ = listener.accept()
            self.connection.settimeout(12)
            guardian_pid = peer_pid(self.connection)
            data = bytearray()
            while not data.endswith(b'\n') and len(data) < 16384:
                chunk = self.connection.recv(4096)
                if not chunk:
                    raise RuntimeError('fixture guardian disconnected before startup')
                data.extend(chunk)
            hello = json.loads(data)
            if hello.get('nonce') != nonce or hello.get('guardian_pid') != guardian_pid:
                raise RuntimeError('fixture guardian handshake failed')
            if hello.get('error_kind') == 'state_invalid':
                from local_app.models import AppError
                self.connection.sendall(b'E')
                raise AppError('native_state_invalid','执行记录校验失败，已暂停；请先核对安装与记录',503)
            if not hello.get('banner','').startswith('READY '):
                raise RuntimeError('fixture guardian startup failed')
            self.pid = int(hello['pid'])
            self._identity = identity(self.pid)
            if self._identity is None or self._identity[3] != hello['guardian_pid']:
                raise RuntimeError('fixture process parent not verified')
            read_fd, write_fd = os.pipe()
            os.write(write_fd, (hello['banner'] + '\n').encode())
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, 'r')
            self.connection.setblocking(False)
        except BaseException:
            self.cleanup()
            raise
        finally:
            listener.close()
            if self.owner_path.exists() and not self.owner_path.is_symlink():
                info = self.owner_path.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                    self.owner_path.unlink()

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        current = identity(self.pid)
        if current is not None and current[:3] == self._identity[:3]:
            return None
        # The guardian is the actual parent and reports the reaped exit status
        # over the authenticated lifeline.  Missing/invalid status is failure.
        deadline = time.monotonic() + .5
        while self.connection is not None and time.monotonic() < deadline:
            try:
                ready, _, _ = select.select([self.connection], [], [], max(0, deadline-time.monotonic()))
                if not ready:break
                chunk = self.connection.recv(1024)
                if not chunk:break
                self._exit_buffer.extend(chunk)
                if len(self._exit_buffer) > 1024:break
                if b'\n' in self._exit_buffer:
                    frame = json.loads(bytes(self._exit_buffer).split(b'\n',1)[0])
                    status = frame.get('exit_code') if isinstance(frame,dict) and frame.get('type') == 'exit' else None
                    if type(status) is int and -255 <= status <= 255:
                        self._returncode = status
                    break
            except (OSError,ValueError,TypeError,json.JSONDecodeError):
                break
        if self._returncode is None:
            self._returncode = 1  # Lost status is fail-closed, never an invitation to replay.
        return self._returncode

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.02)
        return self._returncode

    def send_signal(self, sig):
        current = identity(self.pid)
        if current is None or current[:3] != self._identity[:3]:
            return
        os.kill(self.pid, sig)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def cleanup(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.pid is not None and hasattr(self, '_identity') and self.poll() is None:
            self.terminate()
            try:
                self.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.kill()
                self.wait(timeout=3)
        if self.registered:
            subprocess.run(['/bin/launchctl','bootout',self.domain+'/'+self.label],capture_output=True,timeout=12)
            if subprocess.run(['/bin/launchctl','print',self.domain+'/'+self.label],capture_output=True,timeout=12).returncode == 0:
                raise RuntimeError('fixture sender agent remains registered')
            self.registered = False
        for path, expected in self._owned_files.items():
            remove_owned(path, expected)
