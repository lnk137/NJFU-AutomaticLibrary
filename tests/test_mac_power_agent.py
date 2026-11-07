"""Fake OS/process/network tests. No live power or school operation is permitted."""
from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'AL-BackEnd')]
import power_agent
from local_app.power import PowerFiles


def wall(text):
    return datetime.fromisoformat('2026-10-25T' + text + '+08:00').timestamp()


class Child:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, **kwargs):
        return 0


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-power-agent-')
        self.files = PowerFiles(Path(self.temp.name).resolve() / 'power')
        self.files.update({'enabled': True, 'allow_live': True}, 0)
        self.spawn = Mock(side_effect=lambda *a, **k: Child())
        self.health = Mock(return_value={'application': 'NJFU-AutomaticLibrary-local-multi',
                                        'allow_live': True, 'public_enabled': True})
        self.probe = Mock(return_value={'ok': True})
        self.system = Mock(return_value={'ac': True, 'lid_closed': False, 'blockers': []})
        self.supervisor = power_agent.Supervisor(ROOT, files=self.files, spawn=self.spawn,
            health=self.health, probe=self.probe, system=self.system, installed=lambda _: True,
            wake_check=lambda _: True)

    def tearDown(self):
        self.supervisor.close()
        self.temp.cleanup()

    def evidence(self, now, elapsed, states=('success',)):
        self.files.write('backend.json', {'at': now, 'elapsed': elapsed, 'date': '2026-10-25',
            'busy': 0, 'active': 0, 'uncertain': 0, 'expected': list(states),
            'allow_live': True, 'scheduler_running': True, 'clock_ok': True,
            'last_error': None, 'scheduler_warning': None, 'error': None})

    def test_window_holds_system_but_allows_display_sleep(self):
        self.supervisor.step(wall('06:55:00'), 100.)
        self.assertTrue(self.supervisor.awake)
        command = self.spawn.call_args_list[0].args[0]
        self.assertEqual(command[:2], ['/usr/bin/caffeinate', '-is'])
        self.assertNotIn('sleepnow', str(self.spawn.call_args_list))
        self.probe.assert_called_once()

    def test_fresh_success_releases_only_our_assertion_after_review(self):
        self.supervisor.step(wall('06:55:00'), 100.)
        child = self.supervisor.awake
        self.evidence(wall('07:05:00'), 700.)
        self.supervisor.step(wall('07:05:00'), 700.)
        self.assertTrue(child.terminated)
        self.assertIsNone(self.supervisor.awake)
        self.assertTrue(self.files.read('agent.json')['ready'])

    def test_failed_missing_or_stale_results_retain_the_assertion(self):
        for states in (('failed',), ('missing',), ('uncertain',)):
            now = wall('07:05:00')
            self.evidence(now, 100., states)
            self.supervisor.step(now, 100.)
            self.assertIsNotNone(self.supervisor.awake)

    def test_manual_pause_does_not_restart_backend_or_probe(self):
        self.files.update({'paused': True}, 1)
        self.supervisor.step(wall('06:55:00'), 100.)
        self.spawn.assert_not_called()
        self.health.assert_not_called()
        self.probe.assert_not_called()

    def test_quiet_interval_performs_no_probe_health_or_state_writes(self):
        self.supervisor.step(wall('06:59:39'), 90.)
        self.files.write('backend.json', {'pid': os.getpid()})
        previous = self.files.read('agent.json')
        self.spawn.reset_mock(); self.health.reset_mock(); self.probe.reset_mock(); self.system.reset_mock()
        self.supervisor.step(wall('07:00:10'), 121.)
        self.health.assert_not_called(); self.probe.assert_not_called(); self.system.assert_not_called()
        self.spawn.assert_not_called()
        self.assertEqual(self.files.read('agent.json'), previous)
        self.assertTrue(self.files.delegated(121., wall('07:00:10')))

    def test_wake_after_deadline_recovers_service_without_any_booking_command(self):
        self.health.return_value = None
        self.supervisor.step(wall('07:10:00'), 100.)
        commands = [call.args[0] for call in self.spawn.call_args_list]
        # Inspect executable arguments, not substrings of arbitrary root paths.
        self.assertEqual(commands, [
            ['/usr/bin/caffeinate', '-is', '-w', str(os.getpid())],
            [str(ROOT / '.venv/bin/python'), '-B', str(ROOT / 'public_run.py'),
             '--power-managed', '--enable-live-booking'],
        ])
        self.probe.assert_not_called()

    def test_repeated_missing_health_has_bounded_launcher_retries(self):
        self.health.return_value = None
        self.supervisor.step(wall('06:55:00'), 100.)
        calls = self.spawn.call_count
        for elapsed in (101., 102., 103.):
            self.supervisor.step(wall('06:55:00') + elapsed - 100., elapsed)
        self.assertEqual(self.spawn.call_count, calls)

    def test_clock_jump_never_authorizes_release(self):
        self.supervisor.step(wall('06:55:00'), 100.)
        self.evidence(wall('07:06:00'), 101.)
        self.supervisor.step(wall('07:06:00'), 101.)
        self.assertIsNotNone(self.supervisor.awake)
        self.assertIn('时钟', self.files.read('agent.json')['reason'])

    def test_failed_assertion_is_not_advertised_as_safe_delegation(self):
        self.spawn.side_effect = OSError('fixture')
        self.supervisor.step(wall('06:55:00'), 100.)
        self.assertFalse(self.files.read('agent.json')['ready'])

    def test_lid_and_power_limitations_are_visible_without_changing_global_settings(self):
        self.system.return_value = {'ac': False, 'lid_closed': True, 'blockers': ['other app']}
        self.supervisor.step(wall('06:55:00'), 100.)
        state = self.files.read('agent.json')
        self.assertTrue(state['system']['lid_closed'])
        self.assertFalse(state['system']['ac'])
        self.assertNotIn('disablesleep', str(self.spawn.call_args_list))

    def test_cold_restart_in_quiet_window_still_protects_and_recovers_missing_backend(self):
        self.supervisor.step(wall('07:00:10'), 100.)
        self.assertIsNotNone(self.supervisor.awake)
        self.assertTrue(any(str(ROOT / 'public_run.py') in call.args[0] for call in self.spawn.call_args_list))
        self.probe.assert_not_called()
        self.health.assert_not_called()
        self.assertIsNone(self.files.read('agent.json'))

    def test_warm_backend_death_in_quiet_interval_recovers_without_http_or_writes(self):
        self.supervisor.step(wall('06:59:39'), 90.)
        self.files.write('backend.json', {'pid': 2147483647})
        before = self.files.read('agent.json')
        self.spawn.reset_mock(); self.health.reset_mock(); self.probe.reset_mock()
        self.supervisor.step(wall('07:00:00'), 111.)
        self.assertTrue(any(str(ROOT / 'public_run.py') in call.args[0] for call in self.spawn.call_args_list))
        self.health.assert_not_called(); self.probe.assert_not_called()
        self.assertEqual(self.files.read('agent.json'), before)

    def test_pause_inside_quiet_interval_prevents_restart_and_releases_agent_lease(self):
        self.supervisor.step(wall('06:59:39'), 90.)
        self.files.update({'paused': True}, 1)
        self.spawn.reset_mock(); self.health.reset_mock()
        self.supervisor.step(wall('07:00:00'), 111.)
        self.spawn.assert_not_called(); self.health.assert_not_called()
        self.assertIsNone(self.supervisor.awake)


if __name__ == '__main__':
    unittest.main()
