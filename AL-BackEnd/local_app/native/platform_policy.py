"""Explicit platform durability policy; POSIX fsync is not Apple fullfsync."""
import sys


def durability_policy():
    if sys.platform == 'darwin':
        return {'platform': 'darwin', 'durability_profile': 'darwin_fullfsync',
                'synchronous': 3, 'fullfsync': 1, 'checkpoint_fullfsync': 1}
    if sys.platform.startswith('linux'):
        return {'platform': 'linux', 'durability_profile': 'linux_posix_fsync',
                'synchronous': 3, 'fullfsync': 0, 'checkpoint_fullfsync': 0}
    raise ValueError('unsupported native durability platform')


def validate_native_policy(config):
    expected = durability_policy()
    if not isinstance(config, dict) or config.get('variant') != 'wal-raw-t':
        raise ValueError('retained native sender required')
    if any(type(config.get(key)) is not type(value) or config.get(key) != value
           for key, value in expected.items()):
        raise ValueError('native platform durability profile mismatch')
    return expected


def configure_sqlite(connection):
    expected = durability_policy()
    connection.execute('PRAGMA synchronous=EXTRA')
    for key in ('fullfsync', 'checkpoint_fullfsync'):
        connection.execute('PRAGMA '+key+'='+str(expected[key]))
    actual = [connection.execute('PRAGMA '+key).fetchone()[0]
              for key in ('synchronous', 'fullfsync', 'checkpoint_fullfsync')]
    if actual != [expected[key] for key in ('synchronous', 'fullfsync', 'checkpoint_fullfsync')]:
        raise ValueError('required platform durability unavailable')
    return expected
