"""Keep an owned sender alive only while its management owner remains connected."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import socket
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_app.native.interactive import PREFIX, peer_pid, remove_owned, validate


def main():
    manifest_path = Path(sys.argv[1])
    info = manifest_path.lstat()
    if (manifest_path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError('unsafe fixture manifest')
    manifest = json.loads(manifest_path.read_text())
    runtime = Path(manifest['runtime'])
    if (manifest_path.parent != runtime or not manifest['label'].startswith(PREFIX)
            or not re.fullmatch(r'm[0-9a-f]{8}\.json',manifest_path.name)):
        raise ValueError('fixture manifest boundary denied')
    plist_path=runtime/('j'+manifest_path.name[1:-5]+'.plist')
    plist_info=plist_path.lstat()
    if (plist_path.is_symlink() or not stat.S_ISREG(plist_info.st_mode) or plist_info.st_uid!=os.getuid()
            or plist_info.st_mode & 0o077 or plist_info.st_nlink!=1):
        raise ValueError('unsafe launch file')
    job=plistlib.loads(plist_path.read_bytes())
    if job.get('Label')!=manifest['label'] or job.get('ProgramArguments',[])[-1:]!=[str(manifest_path)]:
        raise ValueError('launch file does not match manifest')
    owned={manifest_path:(info.st_dev,info.st_ino),plist_path:(plist_info.st_dev,plist_info.st_ino)}
    validate(manifest['command'], runtime, manifest['expected_sha256'], manifest['environment'],
             root=Path(manifest['root']) if manifest['root'] else None, allow_school=manifest['allow_school'])
    owner_path = Path(manifest['owner_socket'])
    if owner_path.parent != runtime:
        raise ValueError('fixture owner socket boundary denied')
    owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    child = None
    owner_lost = True

    def stopping(*_):
        raise InterruptedError('fixture guardian stopped')

    signal.signal(signal.SIGTERM, stopping)
    try:
        owner.settimeout(8)
        owner.connect(str(owner_path))
        if peer_pid(owner) != manifest['owner_pid']:
            raise RuntimeError('fixture owner process mismatch')
        child = subprocess.Popen(manifest['command'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=manifest['environment'])
        readable, _, _ = select.select([owner, child.stdout], [], [], 8)
        if owner in readable and not owner.recv(1):
            raise RuntimeError('fixture owner disappeared before startup')
        if child.stdout not in readable:
            raise RuntimeError('fixture sender startup timeout')
        banner = child.stdout.readline(8192).strip()
        if not banner.startswith('READY '):
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=3)
            detail = child.stderr.read(2048).lower()
            kind = 'state_invalid' if any(word in detail for word in ('ledger', 'identity', 'state fingerprint', 'private state')) else 'startup_failed'
            owner.sendall((json.dumps({'nonce': manifest['nonce'], 'guardian_pid': os.getpid(),
                                       'error_kind': kind}) + '\n').encode())
            # Keep the authenticated peer alive until the owner receives the error.
            readable, _, _ = select.select([owner], [], [], 3)
            if readable:
                owner.recv(1)
            raise RuntimeError('fixture sender startup failed')
        hello = {'nonce': manifest['nonce'], 'pid': child.pid, 'guardian_pid': os.getpid(), 'banner': banner}
        owner.sendall((json.dumps(hello) + '\n').encode())
        owner.setblocking(False)
        owner_lost = False
        while child.poll() is None:
            readable, _, _ = select.select([owner], [], [], .05)
            if readable and not owner.recv(1):
                owner_lost = True
                break
        if not owner_lost and child.poll() is not None:
            try:
                owner.settimeout(1)
                owner.sendall((json.dumps({'type':'exit','exit_code':child.returncode})+'\n').encode())
            except OSError:
                owner_lost = True
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
        owner.close()
        try:
            for path,expected in owned.items():
                remove_owned(path,expected)
        finally:
            # Remove this private service even if the owner dies after child exit.
            subprocess.Popen(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/'+manifest['label']],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)


if __name__ == '__main__':
    main()
