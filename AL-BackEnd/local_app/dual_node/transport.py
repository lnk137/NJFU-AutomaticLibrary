"""Bounded persistent SSH stdio RPC. Failures never imply a mutation did not commit."""
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import threading
import time

MAX_FRAME = 65536


class TransportError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _encode(value):
    try:
        if type(value) is not dict:
            raise ValueError()
        frame = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(frame) > MAX_FRAME:
            raise ValueError()
        return frame
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise TransportError('invalid_rpc_frame') from None


class SSHTransport:
    """One request at a time; no implicit reconnect or retry of an unknown result."""
    def __init__(self, config, host, *, timeout=5, popen=subprocess.Popen):
        config = Path(config).absolute()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', host):
            raise TransportError('invalid_ssh_alias')
        try:
            info = config.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or config.is_symlink()):
                raise ValueError()
        except (OSError, ValueError):
            raise TransportError('unsafe_ssh_config') from None
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise TransportError('invalid_rpc_timeout')
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._closed = False
        command = ['/usr/bin/ssh', '-F', str(config), '-T', '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'PasswordAuthentication=no',
                   '-o', 'KbdInteractiveAuthentication=no', '-o', 'IdentitiesOnly=yes',
                   '-o', 'ClearAllForwardings=yes', '-o', 'ControlMaster=no',
                   '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=10',
                   '-o', 'ServerAliveCountMax=2', host]
        try:
            self._child = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
        except OSError:
            raise TransportError('ssh_start_failed') from None
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._child.stdout, selectors.EVENT_READ)

    def call(self, request):
        frame = _encode(request)
        with self._lock:
            if self._closed:
                raise TransportError('rpc_closed')
            deadline = time.monotonic() + self.timeout
            try:
                # Small bounded frames, and only one outstanding request: use a
                # nonblocking descriptor so a stalled server cannot block writes.
                fd = self._child.stdin.fileno()
                os.set_blocking(fd, False)
                sent = 0
                with selectors.DefaultSelector() as writable:
                    writable.register(fd, selectors.EVENT_WRITE)
                    while sent < len(frame):
                        left = deadline - time.monotonic()
                        if left <= 0 or not writable.select(left):
                            raise TransportError('rpc_timeout_unknown')
                        try:
                            sent += os.write(fd, frame[sent:])
                        except BlockingIOError:
                            continue
                while b'\n' not in self._buffer:
                    left = deadline - time.monotonic()
                    if left <= 0 or not self._selector.select(left):
                        raise TransportError('rpc_timeout_unknown')
                    chunk = os.read(self._child.stdout.fileno(), 8192)
                    if not chunk:
                        raise TransportError('rpc_disconnected_unknown')
                    self._buffer.extend(chunk)
                    if len(self._buffer) > MAX_FRAME:
                        raise TransportError('rpc_response_too_large')
                line, remainder = self._buffer.split(b'\n', 1)
                # No notifications or unsolicited responses are in this protocol.
                if remainder:
                    raise TransportError('rpc_unsolicited_output')
                self._buffer.clear()
                result = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(result) is not dict:
                    raise ValueError()
                return result
            except TransportError:
                self.close()
                raise
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                self.close()
                raise TransportError('rpc_invalid_or_lost_response_unknown') from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._selector.close()
        for stream in (self._child.stdin, self._child.stdout):
            if stream is not None:
                stream.close()
        if self._child.poll() is None:
            self._child.terminate()
            try:
                self._child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._child.kill()
                self._child.wait(timeout=1)
        else:
            self._child.wait()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
