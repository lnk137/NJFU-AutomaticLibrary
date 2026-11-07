"""Guarded root-key enrollment. --check is read only; no password prompts."""
from pathlib import Path
import argparse
import base64
import fcntl
import inspect
import json
import os
import stat
import struct
import subprocess
import sys

sys.dont_write_bytecode = True
HOST, PORT, COMMENT = 'root@183.66.27.20', '23486', 'njfu-inbox-admin'


class GuardError(Exception):
    pass


class Paths:
    def __init__(self, project, home):
        self.project, self.home = Path(project), Path(home)
        self.key_dir = self.home / '.ssh/njfu-inbox-admin'
        self.key = self.key_dir / 'id_ed25519'
        self.public = self.key_dir / 'id_ed25519.pub'
        self.socket = self.project / '.local-data/inbox-setup/root.sock'


def public_identity(public):
    if not isinstance(public, str) or '\n' in public or '\r' in public:
        raise GuardError('invalid_public_key')
    parts = public.strip().split()
    if len(parts) not in (2, 3) or parts[0] != 'ssh-ed25519':
        raise GuardError('invalid_public_key')
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        raise GuardError('invalid_public_key') from None
    if len(raw) != 51 or raw[:19] != struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32):
        raise GuardError('invalid_public_key')
    return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


def safe_fd(fd, uid, *, directory=False, private=False):
    info = os.fstat(fd)
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind or info.st_uid != uid or info.st_mode & 0o022:
        raise GuardError('unsafe_owned_path')
    if not directory and info.st_nlink != 1:
        raise GuardError('unsafe_hardlink')
    if private and stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise GuardError('unsafe_private_permissions')


def open_dir(path, uid, *, dir_fd=None, private=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        raise GuardError('unsafe_or_missing_directory') from None
    try:
        safe_fd(fd, uid, directory=True, private=private)
    except BaseException:
        os.close(fd)
        raise
    return fd


def preflight_home(home, uid):
    """No creation, chmod, lock file or credential reads."""
    home_fd = open_dir(home, uid)
    try:
        try:
            info = os.stat('.ssh', dir_fd=home_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if not stat.S_ISDIR(info.st_mode):
            raise GuardError('unsafe_ssh_directory')
        ssh_fd = open_dir('.ssh', uid, dir_fd=home_fd)
        try:
            try:
                fd = os.open('authorized_keys', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=ssh_fd)
            except FileNotFoundError:
                return True
            except OSError:
                raise GuardError('unsafe_authorized_keys') from None
            try:
                safe_fd(fd, uid)
            finally:
                os.close(fd)
        finally:
            os.close(ssh_fd)
    finally:
        os.close(home_fd)
    return True


def append_authorized(home, public, uid):
    identity = public_identity(public)
    line = ('restrict ' + identity + ' njfu-inbox-admin').encode()
    preflight_home(home, uid)
    home_fd = open_dir(home, uid)
    try:
        try:
            os.mkdir('.ssh', 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        ssh_fd = open_dir('.ssh', uid, dir_fd=home_fd)
        try:
            try:
                fd = os.open('authorized_keys', os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=ssh_fd)
            except OSError:
                raise GuardError('unsafe_authorized_keys') from None
            try:
                safe_fd(fd, uid)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.fstat(fd).st_size > 4 * 1024 * 1024:
                    raise GuardError('authorized_keys_too_large')
                old = os.read(fd, 4 * 1024 * 1024 + 1)
                found = False
                for entry in old.splitlines():
                    if not entry.strip() or entry.lstrip().startswith(b'#'):
                        continue
                    fields = entry.split()
                    if identity.split()[1].encode() in fields or b'njfu-inbox-admin' in fields:
                        if entry != line:
                            raise GuardError('existing_admin_key_conflict')
                        found = True
                if found:
                    return {'root': uid == 0, 'safe': True, 'appended': False}
                addition = (b'\n' if old and not old.endswith(b'\n') else b'') + line + b'\n'
                if os.write(fd, addition) != len(addition):
                    raise GuardError('append_incomplete_inspect_before_retry')
                os.fsync(fd)
                os.fsync(ssh_fd)
                return {'root': uid == 0, 'safe': True, 'appended': True}
            finally:
                os.close(fd)
        finally:
            os.close(ssh_fd)
    finally:
        os.close(home_fd)


def call(args, *, runner=subprocess.run, data=None, timeout=15):
    # Child diagnostics are deliberately never printed or persisted.
    kwargs = dict(capture_output=True, timeout=timeout,
                  env={**os.environ, 'SSH_ASKPASS_REQUIRE': 'never'})
    if data is None:
        kwargs['stdin'] = subprocess.DEVNULL
    else:
        kwargs['input'] = data
    return runner(args, **kwargs)


def local_key(paths, create):
    ssh_fd = open_dir(paths.home / '.ssh', os.getuid(), private=True)
    try:
        if create:
            try:
                os.mkdir('njfu-inbox-admin', 0o700, dir_fd=ssh_fd)
            except FileExistsError:
                pass
        elif not paths.key_dir.exists() and not paths.key_dir.is_symlink():
            return None
        directory = open_dir('njfu-inbox-admin', os.getuid(), dir_fd=ssh_fd, private=True)
        try:
            lock = None
            if create:
                try:
                    lock = os.open('enable.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    safe_fd(lock, os.getuid(), private=True)
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, GuardError):
                    if lock is not None: os.close(lock)
                    raise GuardError('local_key_lock_unavailable') from None
            try:
                present = [path.exists() or path.is_symlink() for path in (paths.key, paths.public)]
                if any(present) and not all(present):
                    raise GuardError('partial_key_pair_preserved')
                if not any(present):
                    if not create:
                        return None
                    result = call(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', COMMENT, '-f', str(paths.key)])
                    if result.returncode:
                        raise GuardError('key_generation_unconfirmed')
                    os.chmod(paths.public, 0o600, follow_symlinks=False)
                for name in ('id_ed25519', 'id_ed25519.pub'):
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    except OSError:
                        raise GuardError('unsafe_local_key') from None
                    try:
                        safe_fd(fd, os.getuid(), private=True)
                    finally:
                        os.close(fd)
                public = public_identity(paths.public.read_text().strip())
                derived = call(['/usr/bin/ssh-keygen', '-y', '-P', '', '-f', str(paths.key)])
                if derived.returncode or public_identity(derived.stdout.decode().strip()) != public:
                    raise GuardError('local_key_pair_mismatch')
                return public
            finally:
                if lock is not None:
                    os.close(lock)
        finally:
            os.close(directory)
    finally:
        os.close(ssh_fd)


def master_available(paths, runner):
    directory = open_dir(paths.socket.parent, os.getuid(), private=True)
    os.close(directory)
    if paths.socket.is_symlink() or paths.socket.exists() and not paths.socket.is_socket():
        raise GuardError('unsafe_control_socket')
    return call(['/usr/bin/ssh', '-F', '/dev/null', '-S', str(paths.socket), '-O', 'check', '-o', 'UpdateHostKeys=no',
                 '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes', '-p', PORT, HOST], runner=runner).returncode == 0


def master_request(paths, action, public, runner):
    imports = 'from pathlib import Path\nimport base64,fcntl,json,os,pwd,stat,struct\n'
    functions = (GuardError, public_identity, safe_fd, open_dir, preflight_home, append_authorized)
    source = imports + '\n\n'.join(inspect.getsource(item) for item in functions)
    source += '\nrequest=' + repr({'action': action, 'public': public}) + '\n'
    source += """
try:
    if os.geteuid() != 0: raise GuardError('root_required')
    home = Path(pwd.getpwuid(0).pw_dir)
    if request['action'] == 'probe':
        result = {'root': True, 'safe': preflight_home(home, 0)}
    elif request['action'] == 'append':
        result = append_authorized(home, request['public'], 0)
    else: raise GuardError('invalid_action')
    print(json.dumps(result))
except Exception:
    print(json.dumps({'root': False, 'safe': False}))
    raise SystemExit(1)
"""
    args = ['/usr/bin/ssh', '-F', '/dev/null', '-S', str(paths.socket), '-T', '-o', 'ControlMaster=no',
            '-o', 'ProxyCommand=/usr/bin/false', '-o', 'BatchMode=yes', '-o', 'PasswordAuthentication=no',
            '-o', 'KbdInteractiveAuthentication=no', '-o', 'NumberOfPasswordPrompts=0',
            '-o', 'UpdateHostKeys=no', '-o', 'StrictHostKeyChecking=yes',
            '-p', PORT, HOST, '/usr/bin/python3 -B -']
    result = call(args, runner=runner, data=source.encode())
    if result.returncode:
        raise GuardError('master_root_operation_unconfirmed')
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise GuardError('invalid_master_response') from None
    if not isinstance(value, dict) or value.get('root') is not True or value.get('safe') is not True:
        raise GuardError('master_root_policy_failed')
    return value


def auth_command(paths):
    options = ('BatchMode=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none',
               'PreferredAuthentications=publickey', 'PasswordAuthentication=no',
               'KbdInteractiveAuthentication=no', 'NumberOfPasswordPrompts=0',
               'StrictHostKeyChecking=yes', 'UpdateHostKeys=no', 'ConnectTimeout=6',
               'ConnectionAttempts=1', 'UserKnownHostsFile=' + str(paths.home / '.ssh/known_hosts'))
    return ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', str(paths.key), '-p', PORT] + [part for option in options for part in ('-o', option)]


def fresh_login(paths, runner):
    result = call(auth_command(paths) + ['-o', 'ControlMaster=no', '-o', 'ControlPath=none', HOST, '/usr/bin/id -u'], runner=runner)
    return result.returncode == 0 and result.stdout.strip() == b'0'


def execute(action, paths, runner=subprocess.run):
    master = master_available(paths, runner)
    if master:
        master_request(paths, 'probe', None, runner)
    if action == 'enable':
        if not master:
            raise GuardError('existing_root_master_required_no_writes')
        public = local_key(paths, True)
        enrolled = master_request(paths, 'append', public, runner)
        if not fresh_login(paths, runner):
            raise GuardError('fresh_key_login_unconfirmed')
        return {'ok': True, 'master_available': True, 'key_login_verified': True,
                'authorized_key_appended': enrolled.get('appended') is True, 'key_path': str(paths.key)}
    public = local_key(paths, False)
    verified = public is not None and fresh_login(paths, runner)
    if action == 'connect':
        if not verified:
            raise GuardError('fresh_key_login_unconfirmed')
        if not master:
            if paths.socket.exists() or paths.socket.is_symlink():
                raise GuardError('existing_control_socket_preserved')
            args = auth_command(paths) + ['-M', '-S', str(paths.socket), '-o', 'ControlMaster=yes',
                '-o', 'ControlPersist=14400', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-f', '-N', HOST]
            if call(args, runner=runner).returncode or not master_available(paths, runner):
                raise GuardError('new_master_unconfirmed')
            master_request(paths, 'probe', None, runner)
            master = True
    return {'ok': bool(verified), 'master_available': master, 'key_login_verified': verified,
            'key_path': str(paths.key), 'control_socket': str(paths.socket)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for name in ('check', 'enable', 'connect'):
        actions.add_argument('--' + name, action='store_const', const=name, dest='action')
    args = parser.parse_args()
    paths = Paths(Path(__file__).resolve().parents[1], Path.home())
    try:
        result = execute(args.action, paths)
    except (GuardError, OSError, ValueError, subprocess.SubprocessError) as error:
        result = {'ok': False, 'key_login_verified': False,
                  'reason': str(error) if isinstance(error, GuardError) else 'operation_unconfirmed'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
