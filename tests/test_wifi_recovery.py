"""Network recovery safety contract; fake OS, clock and portal only."""
from datetime import datetime, timedelta
import inspect
from pathlib import Path
import json
import os
import plistlib
import sys
import tempfile
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
try:
    import wifi_recovery as recovery
except ModuleNotFoundError as exc:
    if exc.name != 'wifi_recovery':
        raise
    recovery = None

ZONE = ZoneInfo('Asia/Shanghai')
START = datetime(2026, 10, 28, 6, 55, 0, tzinfo=ZONE)


class FixtureAdapter:
    def __init__(self, *, radio='on', association='target', ipv4=True,
                 portal='authenticated', school='reachable'):
        self.radio, self.association, self.ipv4 = radio, association, ipv4
        self.portal, self.school = portal, school
        self.actions, self.reads = [], 0
        self.fail_actions = False

    def observe(self):
        self.reads += 1
        return {'radio': self.radio, 'association': self.association, 'ipv4': self.ipv4,
                'portal': self.portal, 'school': self.school}

    def enable_wifi(self):
        self.actions.append(('enable',))
        return not self.fail_actions

    def join(self, ssid):
        self.actions.append(('join', ssid))
        return not self.fail_actions


class RecoveryContractTests(unittest.TestCase):
    def controller(self, adapter, state=None):
        self.assertIsNotNone(recovery, 'Wi-Fi recovery feature is not implemented')
        return recovery.RecoveryController(adapter, '@f-Young', {} if state is None else state)

    def test_target_connected_and_authenticated_never_reconnects(self):
        a = FixtureAdapter(); c = self.controller(a)
        status = c.step(START, permitted=True)
        self.assertEqual(status['portal'], 'authenticated')
        self.assertEqual(a.actions, [])

    def test_wifi_off_is_enabled_before_attempting_target_join(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False, portal='unavailable')
        c = self.controller(a); c.step(START, permitted=True)
        self.assertEqual(a.actions, [('enable',)])
        a.radio = 'on'; c.step(START + timedelta(seconds=21), permitted=True)
        self.assertEqual(a.actions, [('enable',), ('join', '@f-Young')])

    def test_disconnected_network_reconnects_only_to_configured_ssid(self):
        a = FixtureAdapter(association='disconnected', ipv4=False, portal='unavailable')
        c = self.controller(a); c.step(START, permitted=True)
        c.step(START + timedelta(seconds=12), permitted=True)
        self.assertEqual(a.actions, [('join', '@f-Young')])

    def test_redacted_ssid_with_active_ip_never_forces_a_rejoin(self):
        a = FixtureAdapter(association='unknown', ipv4=True, portal='unavailable', school='unknown')
        c = self.controller(a); status = c.step(START, permitted=True)
        c.step(START + timedelta(seconds=30), permitted=True)
        self.assertEqual(a.actions, [])
        self.assertEqual(status['association'], 'unknown')

    def test_explicit_other_network_can_switch_to_authorized_target(self):
        a = FixtureAdapter(association='other', portal='unavailable')
        c = self.controller(a); c.step(START, permitted=True)
        c.step(START + timedelta(seconds=12), permitted=True)
        self.assertEqual(a.actions, [('join', '@f-Young')])

    def test_portal_login_required_does_not_cycle_wifi_or_send_password(self):
        a = FixtureAdapter(portal='login_required', school='unreachable')
        c = self.controller(a); status = c.step(START, permitted=True)
        c.step(START + timedelta(seconds=30), permitted=True)
        self.assertEqual(a.actions, [])
        self.assertEqual(status['portal'], 'login_required')

    def test_retries_are_bounded_and_shared_across_restart(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False, portal='unavailable')
        a.fail_actions = True; state = {}; c = self.controller(a, state)
        for seconds in (0, 10, 21, 42, 63):
            if seconds == 42:
                c = self.controller(a, state)
            c.step(START + timedelta(seconds=seconds), permitted=True)
        self.assertEqual(len(a.actions), 3)

    def test_no_network_actions_or_probes_when_disabled_or_outside_window(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False)
        c = self.controller(a)
        c.step(START, permitted=False)
        c.step(START - timedelta(seconds=1), permitted=True)
        c.step(START + timedelta(minutes=4, seconds=30), permitted=True)
        self.assertEqual(a.actions, [])
        self.assertEqual(a.reads, 0)

    def test_no_reconnect_after_action_cutoff(self):
        a = FixtureAdapter(association='disconnected', ipv4=False, portal='unavailable')
        c = self.controller(a)
        c.step(START + timedelta(minutes=1, seconds=31), permitted=True)
        c.step(START + timedelta(minutes=2), permitted=True)
        self.assertEqual(a.actions, [])

    def test_gbk_portal_status_requires_specific_positive_evidence(self):
        self.assertIsNotNone(recovery, 'Wi-Fi recovery feature is not implemented')
        online = '<html><title>注销页</title><body>登录成功</body></html>'.encode('gbk')
        login = '<html><title>登录页</title><input type="password"></html>'.encode('gbk')
        self.assertEqual(recovery.portal_status(online, 0), 'authenticated')
        self.assertEqual(recovery.portal_status(login, 0), 'login_required')
        self.assertEqual(recovery.portal_status(b'<html>other</html>', 0), 'unknown')
        self.assertEqual(recovery.portal_status(b'', 7), 'unavailable')

    def test_action_is_never_taken_when_durable_record_fails(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False, portal='unavailable')
        self.assertIn('persist', inspect.signature(recovery.RecoveryController).parameters,
                      'durable recording hook is not implemented')
        def refuse(_):
            raise OSError('synthetic write failure')
        c = recovery.RecoveryController(a, '@f-Young', {}, persist=refuse)
        status = c.step(START, permitted=True)
        self.assertEqual(a.actions, [])
        self.assertEqual(status['state'], 'state_write_failed')

    def test_clock_crossing_cutoff_during_probe_blocks_network_changes(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False)
        c = recovery.RecoveryController(a, '@f-Young', {},
                                        clock=lambda: START + timedelta(minutes=1, seconds=31))
        c.step(START + timedelta(minutes=1, seconds=29), permitted=True)
        self.assertEqual(a.actions, [])

    def test_corrupt_retry_state_does_not_reset_and_reconnect(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False)
        state = {'day': '2026-10-28', 'attempts': 'broken', 'last_action': None,
                 'disconnected_since': None, 'last_wall': None}
        c = self.controller(a, state)
        try:
            status = c.step(START, permitted=True)
        except Exception as exc:
            self.fail('corrupt state must fail closed: '+type(exc).__name__)
        self.assertEqual(a.actions, [])
        self.assertEqual(status['state'], 'state_invalid')

    def test_privacy_redaction_with_active_ip_is_not_disconnection(self):
        self.assertTrue(hasattr(recovery, 'SystemAdapter'), 'system adapter is not implemented')
        responses = {
            ('/usr/sbin/networksetup', '-getairportpower', 'en0'): b'Wi-Fi Power (en0): On\n',
            ('/sbin/ifconfig', 'en0'): b'en0: flags\n\tinet 10.15.0.5 netmask 0xffff0000\n\tstatus: active\n',
            ('/usr/sbin/networksetup', '-getairportnetwork', 'en0'): b'You are not associated with an AirPort network.\n',
        }
        calls = []
        def run(args, **kwargs):
            calls.append((tuple(args), kwargs))
            if tuple(args) in responses:
                return SimpleNamespace(returncode=0, stdout=responses[tuple(args)])
            if 'http://10.11.2.3/' in args:
                body = '<title>注销页</title>登录成功'.encode('gbk')
                return SimpleNamespace(returncode=0, stdout=body+b'\nCODE:200')
            if 'https://libseat.njfu.edu.cn/' in args:
                return SimpleNamespace(returncode=0, stdout=b'200')
            raise AssertionError('unexpected command')
        adapter = recovery.SystemAdapter('@f-Young', 'http://10.11.2.3/', run=run)
        seen = adapter.observe()
        self.assertEqual(seen['association'], 'unknown')
        self.assertTrue(seen['ipv4'])
        self.assertEqual(seen['portal'], 'authenticated')
        self.assertEqual(seen['school'], 'reachable')
        portal_call = next(x for x in calls if 'http://10.11.2.3/' in x[0])
        self.assertIn('--interface', portal_call[0])
        self.assertIn('en0', portal_call[0])
        self.assertNotIn('shell', portal_call[1])

    def test_reconnect_command_uses_exact_target_without_password_or_shell(self):
        self.assertTrue(hasattr(recovery, 'SystemAdapter'), 'system adapter is not implemented')
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs)); return SimpleNamespace(returncode=0, stdout=b'')
        adapter = recovery.SystemAdapter('@f-Young', 'http://10.11.2.3/', run=run)
        self.assertTrue(adapter.join('@f-Young'))
        self.assertEqual(calls[0][0], ['/usr/sbin/networksetup', '-setairportnetwork', 'en0', '@f-Young'])
        self.assertNotIn('shell', calls[0][1])
        self.assertFalse(adapter.join('some-other-network'))
        self.assertEqual(len(calls), 1)

    def test_guard_is_checked_again_after_recording_attempt(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False)
        saved = []
        c = recovery.RecoveryController(a, '@f-Young', {}, persist=lambda s: saved.append(s),
                                        guard=lambda: False)
        status = c.step(START, permitted=True)
        self.assertEqual(status['state'], 'action_guard_failed')
        self.assertEqual(a.actions, [])
        self.assertEqual(saved[0]['attempts'], 1)

    def test_cutoff_crossed_during_guard_prevents_action(self):
        a = FixtureAdapter(radio='off', association='disconnected', ipv4=False)
        instants = iter((START + timedelta(minutes=1, seconds=29),
                         START + timedelta(minutes=1, seconds=29),
                         START + timedelta(minutes=1, seconds=31)))
        c = recovery.RecoveryController(a, '@f-Young', {}, clock=lambda: next(instants))
        status = c.step(START + timedelta(minutes=1, seconds=29), permitted=True)
        self.assertEqual(a.actions, [])
        self.assertEqual(status['state'], 'action_guard_failed')

    def test_private_state_persists_attempts_and_rejects_symlinks(self):
        self.assertTrue(hasattr(recovery, 'PrivateState'), 'private state is not implemented')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve() / 'state'; base.mkdir(mode=0o700)
            files = recovery.PrivateState(base)
            state = {'day': '2026-10-28', 'attempts': 1, 'last_action': START.timestamp(),
                     'disconnected_since': None, 'last_wall': START.timestamp()}
            files.write('state.json', state)
            self.assertEqual(files.read('state.json'), state)
            self.assertEqual((base / 'state.json').stat().st_mode & 0o777, 0o600)
            (base / 'status.json').symlink_to(base / 'state.json')
            with self.assertRaises((ValueError, OSError)):
                files.write('status.json', {'state': 'observed'})
            self.assertEqual(files.read('state.json'), state)

    def test_guard_requires_fresh_running_backend_and_enabled_config(self):
        self.assertTrue(hasattr(recovery, 'PowerGuard'), 'power guard is not implemented')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve() / 'power'; base.mkdir(mode=0o700)
            config = {'revision': 1, 'enabled': True, 'paused': False, 'allow_live': True}
            backend = {'at': START.timestamp(), 'elapsed': 100.0, 'pid': os.getpid(),
                       'scheduler_running': True, 'clock_ok': True, 'allow_live': True}
            (base / 'config.plist').write_bytes(plistlib.dumps(config))
            (base / 'backend.json').write_text(json.dumps(backend))
            (base / 'config.plist').chmod(0o600); (base / 'backend.json').chmod(0o600)
            guard = recovery.PowerGuard(base, wall=lambda: START.timestamp(), elapsed=lambda: 100.0)
            self.assertTrue(guard())
            backend['at'] -= 30
            (base / 'backend.json').write_text(json.dumps(backend))
            self.assertFalse(guard())
            config['paused'] = True
            (base / 'config.plist').write_bytes(plistlib.dumps(config))
            backend['at'] = START.timestamp()
            (base / 'backend.json').write_text(json.dumps(backend))
            self.assertFalse(guard())

    def test_retry_state_lock_rejects_second_worker(self):
        self.assertTrue(hasattr(recovery, 'PrivateState'), 'private state is not implemented')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve() / 'state'; base.mkdir(mode=0o700)
            files = recovery.PrivateState(base)
            with files.lock():
                with self.assertRaises(BlockingIOError):
                    with files.lock():
                        pass

    def test_worker_uses_guard_and_keeps_status_without_credentials(self):
        self.assertTrue(hasattr(recovery, 'run_window'), 'scheduled worker is not implemented')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve() / 'state'; base.mkdir(mode=0o700)
            files = recovery.PrivateState(base)
            adapter = FixtureAdapter(association='disconnected', ipv4=False, portal='unavailable')
            now = [START]
            def sleep(seconds):
                now[0] += timedelta(seconds=seconds)
            recovery.run_window(files, adapter, lambda: True, now=lambda: now[0], sleep=sleep)
            self.assertEqual(len(adapter.actions), 3)
            self.assertEqual(files.read('state.json')['attempts'], 3)
            status = files.read('status.json')
            self.assertIn('checked_at', status)
            self.assertNotIn('password', json.dumps(status).lower())
            self.assertLess(datetime.fromisoformat(status['checked_at']).time(), recovery.CHECK_END)

    def test_worker_outside_window_does_not_probe_or_create_files(self):
        self.assertTrue(hasattr(recovery, 'run_window'), 'scheduled worker is not implemented')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve() / 'state'; base.mkdir(mode=0o700)
            adapter = FixtureAdapter()
            recovery.run_window(recovery.PrivateState(base), adapter, lambda: True,
                                now=lambda: START - timedelta(seconds=1), sleep=lambda _: None)
            self.assertEqual(adapter.reads, 0)
            self.assertEqual(list(base.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
