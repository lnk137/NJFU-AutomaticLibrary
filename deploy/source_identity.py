"""Guarded source-identity maintenance; never switches executor or native binary."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid


class IdentityConflict(RuntimeError):
    pass


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _private_bytes(path):
    path = Path(path)
    aliases = {Path('/var'): Path('/private/var'), Path('/tmp'): Path('/private/tmp')}
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink() and not (parent in aliases and parent.resolve() == aliases[parent]):
            raise IdentityConflict('Unsafe selector directory')
    folder = path.parent.stat()
    if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid() or folder.st_mode & 0o077:
        raise IdentityConflict('Selector directory must be private')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 131072):
                raise IdentityConflict('Unsafe selector file')
            return stream.read(131073)
    except OSError:
        raise IdentityConflict('Selector is unavailable') from None


def prepare_refresh(path, *, expected_selector_sha256, expected_source_sha256,
                    source_sha256, sources, binary_sha256):
    """Bind only reviewed Python source to the already selected native binary."""
    old_bytes = _private_bytes(path)
    if _digest(old_bytes) != expected_selector_sha256:
        raise IdentityConflict('Selector changed since review')
    if (source_sha256 != expected_source_sha256 or not isinstance(sources, dict)
            or _digest(json.dumps(sources, sort_keys=True).encode()) != source_sha256):
        raise IdentityConflict('Reviewed source identity differs')
    try:
        old = json.loads(old_bytes)
        if (set(old) != {'schema', 'executor', 'release_sha256', 'sources', 'binary_sha256'}
                or type(old['schema']) is not int or old['schema'] != 1 or old['executor'] != 'cpp'
                or old['binary_sha256'] != binary_sha256 or not isinstance(old['sources'], dict)
                or _digest(json.dumps(old['sources'], sort_keys=True).encode()) != old['release_sha256']):
            raise IdentityConflict('Executor or binary identity differs')
        prefix = 'AL-BackEnd/native_executor/'
        old_native = {k: v for k, v in old['sources'].items() if k.startswith(prefix)}
        new_native = {k: v for k, v in sources.items() if k.startswith(prefix)}
        required = {prefix + n for n in ('executor.cpp', 'authority.hpp', 'profile.hpp', 'peer_threads.hpp', 'wire.hpp')}
        # Both historical five-file and current platform-header builds are
        # recognized. Every native file must remain byte-identical.
        if set(old_native) not in (required, required | {prefix + 'platform.hpp'}) or old_native != new_native:
            raise IdentityConflict('Native sources changed; requires a separate release')
    except (ValueError, KeyError, TypeError, AttributeError):
        raise IdentityConflict('Selector structure differs') from None
    updated = dict(old, release_sha256=source_sha256, sources=sources)
    return {'old_bytes': old_bytes, 'new_bytes': (json.dumps(updated, sort_keys=True, indent=2) + '\n').encode(),
            'expected_selector_sha256': expected_selector_sha256, 'source_sha256': source_sha256,
            'binary_sha256': binary_sha256}


def _apply_locked(path, prepared, *, recheck):
    """Atomic replacement after fresh source/binary and selector guards."""
    path = Path(path)
    try:
        target = json.loads(prepared['new_bytes'])
        validated = prepare_refresh(path, expected_selector_sha256=prepared['expected_selector_sha256'],
            expected_source_sha256=prepared['source_sha256'], source_sha256=prepared['source_sha256'],
            sources=target['sources'], binary_sha256=prepared['binary_sha256'])
        if validated['new_bytes'] != prepared['new_bytes']:
            raise IdentityConflict('Prepared identity changed')
    except (KeyError, TypeError, ValueError):
        raise IdentityConflict('Prepared identity is invalid') from None
    def check():
        recheck()
        if _digest(_private_bytes(path)) != prepared['expected_selector_sha256']:
            raise IdentityConflict('Selector changed before apply')
    check()
    temporary = path.parent / ('.native-selection.merge-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(prepared['new_bytes']); stream.flush(); os.fsync(stream.fileno())
        check()
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return _digest(prepared['new_bytes'])


def apply_refresh(path, prepared, *, recheck):
    """Serialize maintainers using a stable private lock; never unlink its inode."""
    path = Path(path)
    _private_bytes(path)
    lock_path = path.parent / '.native-selection.merge.lock'
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        raise IdentityConflict('Maintenance lock is unavailable') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise IdentityConflict('Unsafe maintenance lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IdentityConflict('Another identity maintenance is active') from None
        return _apply_locked(path, prepared, recheck=recheck)
    finally:
        os.close(fd)
