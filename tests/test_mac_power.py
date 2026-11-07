"""Isolated power-window policy, storage and supervisor lease contracts."""
from datetime import datetime
from pathlib import Path
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app import power
from local_app.models import AppError


def epoch(value):
    return datetime.fromisoformat(value + '+08:00').timestamp()


def healthy(now, states=('success',)):
    return {'at': now, 'elapsed': 100., 'date': '2026-10-25', 'scheduler_running': True,
            'clock_ok': True, 'allow_live': True, 'last_error': None, 'scheduler_warning': None,
            'busy': 0, 'active': 0, 'uncertain': 0, 'expected': list(states), 'error': None}


class PowerPolicyTests(unittest.TestCase):
    def test_native_confirmation_requires_current_typed_evidence_and_expected_generation(self):
        now = epoch('2026-10-25T07:05:00')
        data = {'at': now, 'armed': True, 'next_wake': now + 86400 - 600,
                'error': '', 'generation': 'a' * 32}
        self.assertTrue(power.wake_confirmed(data, now, 'a' * 32))
        for changes in ({'at': now - 200}, {'at': now + 30}, {'at': True},
                        {'armed': 1}, {'next_wake': now - 1}, {'next_wake': now + 100},
                        {'error': 'failed'}, {'error': False}, {'generation': 'b' * 32}):
            with self.subTest(changes=changes):
                self.assertFalse(power.wake_confirmed(data | changes, now, 'a' * 32))

    def test_fixed_beijing_window_acquires_at_start_and_reviews_at_end(self):
        config = {'enabled': True, 'paused': False}
        for value, hold in [('06:54:59', False), ('06:55:00', True),
                            ('07:04:59', True), ('07:05:00', False)]:
            now = epoch('2026-10-25T' + value)
            self.assertEqual(power.decide(now, 100., config, healthy(now)).hold, hold)

    def test_no_fresh_evidence_is_never_treated_as_completed(self):
        now = epoch('2026-10-25T07:05:00')
        for change in ({'elapsed': 50.}, {'at': now-40}, {'date': '2026-10-24'},
                       {'error': 'read failed'}, {'clock_ok': False}, {'scheduler_running': False},
                       {'scheduler_warning': 'loop blocked'}, {'last_error': 'clock error'},
                       {'busy': 1}, {'active': 1}, {'uncertain': 1}, {'expected': ['missing']},
                       {'expected': ['failed']}, {'expected': ['uncertain']}, {'expected': ['skipped']}):
            with self.subTest(change=change):
                self.assertTrue(power.decide(now, 100., {'enabled': True}, healthy(now) | change).hold)
        self.assertTrue(power.decide(now, 100., {'enabled': True}, None).hold)

    def test_no_tasks_can_release_only_with_fresh_healthy_backend(self):
        now = epoch('2026-10-25T07:06:00')
        self.assertFalse(power.decide(now, 100., {'enabled': True}, healthy(now, ())).hold)
        self.assertTrue(power.decide(now, 100., {'enabled': True}, healthy(now, ()) | {'allow_live': False}).hold)

    def test_explicit_pause_and_disable_release_only_app_protection(self):
        now = epoch('2026-10-25T07:00:00')
        for config in ({'enabled': False}, {'enabled': True, 'paused': True}):
            self.assertFalse(power.decide(now, 100., config, None).hold)

    def test_in_progress_manual_work_is_protected_outside_morning_window(self):
        now = epoch('2026-10-25T02:00:00')
        self.assertTrue(power.decide(now, 100., {'enabled': True}, healthy(now) | {'busy': 1}).hold)

    def test_no_probe_or_metadata_work_during_submit_quiet_interval(self):
        for value, want in [('06:59:49', False), ('06:59:50', True),
                            ('07:00:40', True), ('07:00:41', False)]:
            self.assertEqual(power.quiet(epoch('2026-10-25T' + value)), want)


class PowerFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-power-test-')
        self.directory = Path(self.temp.name).resolve()
        self.files = power.PowerFiles(self.directory / 'power')

    def tearDown(self):
        self.temp.cleanup()

    def test_reading_missing_config_creates_no_installation_or_files(self):
        self.assertFalse(self.files.config()['enabled'])
        self.assertFalse(self.files.base.exists())

    def test_private_atomic_config_and_revision_conflict_preserve_existing_value(self):
        first = self.files.update({'enabled': True, 'allow_live': True}, 0)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(self.files.base.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.files.base / 'config.plist').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(AppError):
            self.files.update({'paused': True}, 0)
        self.assertFalse(self.files.config()['paused'])

    def test_symlink_and_world_readable_config_fail_closed(self):
        self.files.update({'enabled': True}, 0)
        config = self.files.base / 'config.plist'
        config.chmod(0o644)
        with self.assertRaises(AppError):
            self.files.config()
        config.chmod(0o600)
        other = self.files.base / 'other'
        config.rename(other)
        config.symlink_to(other)
        with self.assertRaises(AppError):
            self.files.config()

    def test_parent_symlink_is_rejected_before_any_write(self):
        target = self.directory / 'target'; target.mkdir()
        self.files.base.symlink_to(target, target_is_directory=True)
        with self.assertRaises(AppError):
            self.files.update({'enabled': True}, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_runner_delegation_requires_enabled_fresh_live_agent(self):
        self.files.update({'enabled': True}, 0)
        self.assertFalse(self.files.delegated(100., epoch('2026-10-25T07:00:00')))
        now = epoch('2026-10-25T07:00:00')
        self.files.write('agent.json', {'pid': os.getpid(), 'at': now, 'elapsed': 100., 'ready': True})
        self.assertTrue(self.files.delegated(101., now+1))
        self.assertFalse(self.files.delegated(130., now+30))
        self.files.update({'paused': True}, 1)
        self.assertFalse(self.files.delegated(101., now+1))


if __name__ == '__main__':
    unittest.main()
