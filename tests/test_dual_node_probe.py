"""Isolated standalone-probe tests; school network is always mocked."""
import argparse
import copy
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'diagnostics/dual_node_probe.py'
SPEC = importlib.util.spec_from_file_location('dual_node_probe', MODULE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def epoch(text):
    return datetime.fromisoformat('2026-11-05T' + text + '+08:00').timestamp()


def response(value=None, status=200, timings='0.001,0.004,0.010,0.020,0.030'):
    if value is None:
        value = {'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': '0'}]}
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return body + probe.MARKER + str(status).encode() + b',' + timings.encode()


def evidence(node, total=100, count=3):
    start = epoch('14:00:00')
    samples = []
    for index in range(count):
        sample = probe.decode_response(response(timings='0.001,0.004,0.010,0.020,' + str(total / 1000)))
        sample.update(at=start + index * 10, finished_at=start + index * 10 + .2)
        samples.append(sample)
    return {**probe.base('sample', node), 'endpoint': probe.ENDPOINT,
            'started_at': start, 'finished_at': start + (count - 1) * 10 + .2,
            'clock_jump_detected': False, 'samples': samples,
            'parameters': {'sample_count': count, 'interval_seconds': 10,
                           'connect_timeout_seconds': 1, 'request_timeout_seconds': 3,
                           'quiet_before_seconds': 300, 'quiet_after_seconds': 300,
                           'release_times': ['07:00:00']}}


class Clock:
    def __init__(self, wall):
        self.wall, self.elapsed = wall, 100.

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds


class ProbeTests(unittest.TestCase):
    def test_guard_default_budget_boundaries(self):
        for text, expected in [('06:54:54', False), ('06:54:55', True),
                               ('07:05:00', True), ('07:05:01', False)]:
            with self.subTest(text=text):
                self.assertEqual(probe.guarded(epoch(text), []), expected)

    def test_guard_custom_midnight_and_invalid_config(self):
        self.assertTrue(probe.guarded(epoch('23:59:59'), ['00:02:00']))
        self.assertTrue(probe.guarded(epoch('00:01:00'), ['23:59:00']))
        for invalid in ('7:00:00', '24:00:00', '07:60:00', '../x', ''):
            with self.subTest(invalid=invalid), self.assertRaises(argparse.ArgumentTypeError):
                probe.guarded(epoch('14:00:00'), [invalid])

    def test_curl_exact_readonly_transport(self):
        argv = probe.curl_command('/usr/bin/curl')
        self.assertEqual(argv[1], '--disable')
        self.assertEqual(argv[-1], probe.ENDPOINT)
        for key, value in [('--request', 'GET'), ('--proxy', ''), ('--noproxy', '*'),
                           ('--proto', '=https'), ('--http1.1', '--max-redirs'),
                           ('--max-redirs', '0'), ('--max-time', '3')]:
            self.assertEqual(argv[argv.index(key) + 1], value)
        for forbidden in ('--insecure', '-k', '--location', '-L', '--retry', '--cookie',
                          '--cookie-jar', '--user', '--data', '--data-raw', '--config'):
            self.assertNotIn(forbidden, argv)

    def test_response_valid_and_captcha_not_readiness(self):
        for flag in ('0', 0, '1', 2):
            row = probe.decode_response(response({'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': flag}]}))
            self.assertTrue(row['public_response_valid'])
            self.assertEqual(row['metrics_ms'], {'dns': 1., 'tcp': 3., 'tls': 6., 'ttfb': 20., 'total': 30.})
        self.assertFalse(probe.DISCLAIMER['automatic_selection_authorized'])
        self.assertEqual(probe.DISCLAIMER['booking_readiness'], 'not_checked')

    def test_response_rejects_portal_http_schema_and_large_body(self):
        bad = [b'<html>portal</html>', {'code': False, 'data': []}, {'code': 0, 'data': []},
               {'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': True}]},
               {'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': 'unknown'}]},
               {'code': 0, 'data': [{'sysKey': 'resvCode', 'sysValue': 0}] * 2},
               b'x' * (probe.BODY_LIMIT + 1)]
        for value in bad:
            with self.subTest(value=str(value)[:40]):
                self.assertFalse(probe.decode_response(response(value))['public_response_valid'])
        for code in (302, 401, 429, 500):
            self.assertEqual(probe.decode_response(response(status=code))['error'], 'unexpected_http_status')

    def test_bad_metrics_cannot_be_success(self):
        for timings in ('nan,0,0,0,1', '0,0,0,1,0', '0,0,-1,1,2', '0,0,0,0,0'):
            with self.subTest(timings=timings):
                self.assertEqual(probe.decode_response(response(timings=timings))['error'], 'invalid_metrics')
        self.assertEqual(probe.decode_response(b'x')['error'], 'invalid_metrics')

    def run_sample(self, clock, runner, count=3, sleep=None):
        with patch.object(probe.shutil, 'which', return_value='/usr/bin/curl'), \
                patch.object(probe, 'command_info', return_value={'version': 'fixture'}), \
                patch.object(probe, 'bounded_run', side_effect=runner) as call, \
                patch.object(probe.time, 'time', side_effect=lambda: clock.wall), \
                patch.object(probe.time, 'monotonic', side_effect=lambda: clock.elapsed), \
                patch.object(probe.time, 'sleep', side_effect=sleep or clock.advance):
            return probe.sample_node('mac', count), call

    def test_quiet_window_makes_no_network_calls(self):
        report, call = self.run_sample(Clock(epoch('06:54:55')), lambda *_a, **_k: (0, response()))
        call.assert_not_called()
        self.assertEqual(report['samples'][0]['error'], 'release_window')

    def test_sleep_rechecks_guard_before_next_request(self):
        clock = Clock(epoch('06:54:50'))
        report, call = self.run_sample(clock, lambda *_a, **_k: (0, response()))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(report['samples'][-1]['error'], 'release_window')

    def test_stalled_schedule_preserves_actual_start_spacing(self):
        clock, starts, sleeps = Clock(epoch('14:00:00')), [], []
        def runner(*_args, **_kwargs):
            starts.append(clock.elapsed)
            clock.advance(.1)
            return 0, response()
        def sleeping(seconds):
            sleeps.append(seconds)
            clock.advance(seconds + (25 if len(sleeps) == 1 else 0))
        report, _ = self.run_sample(clock, runner, sleep=sleeping)
        self.assertEqual(len(report['samples']), 3)
        self.assertGreaterEqual(starts[2] - starts[1], 10)

    def test_final_request_clock_jump_invalidates_sample(self):
        clock = Clock(epoch('14:00:00'))
        def runner(*_args, **_kwargs):
            clock.wall += 5
            return 0, response()
        report, _ = self.run_sample(clock, runner, count=1)
        self.assertTrue(report['clock_jump_detected'])
        self.assertEqual(report['samples'][0]['error'], 'clock_jump')
        self.assertFalse(report['samples'][0]['public_response_valid'])

    def test_curl_failures_timeout_and_limit_are_redacted(self):
        for failure, expected in [(TimeoutError('secret'), 'command_timeout'),
                                  (ValueError('secret'), 'output_limit'),
                                  (OSError('secret'), 'curl_unavailable')]:
            def runner(*_args, **_kwargs):
                raise failure
            report, _ = self.run_sample(Clock(epoch('14:00:00')), runner, count=1)
            self.assertEqual(report['samples'][0]['error'], expected)
            self.assertNotIn('secret', json.dumps(report))
        report, _ = self.run_sample(Clock(epoch('14:00:00')), lambda *_a, **_k: (60, b'secret'), count=1)
        self.assertEqual(report['samples'][0]['curl_exit_code'], 60)
        self.assertNotIn('secret', json.dumps(report))


class ComparisonTests(unittest.TestCase):
    def compare(self, mac=None, ubuntu=None):
        return probe.compare_reports([mac or evidence('mac', 100), ubuntu or evidence('ubuntu', 200)],
                                     now=epoch('14:00:30'))

    def test_advisory_preference_and_close_tie(self):
        result = self.compare()
        self.assertEqual(result['advisory_candidate'], 'mac')
        self.assertFalse(result['automatic_selection_authorized'])
        self.assertIsNone(self.compare(ubuntu=evidence('ubuntu', 105))['advisory_candidate'])

    def test_stale_mismatch_nonoverlap_and_start_skew_refuse(self):
        for field, value in [('endpoint', 'https://other.invalid'), ('probe_version', 'old'),
                             ('finished_at', epoch('13:00:00')), ('schema', True),
                             ('clock_jump_detected', True)]:
            changed = evidence('ubuntu', 200)
            changed[field] = value
            with self.subTest(field=field):
                self.assertIsNone(self.compare(ubuntu=changed)['advisory_candidate'])
        changed = evidence('ubuntu', 200)
        changed['parameters']['interval_seconds'] = 11
        self.assertEqual(self.compare(ubuntu=changed)['reason'], 'parameters_mismatch')
        for shift, count, reason in [(21, 3, 'nonoverlapping_observations'),
                                      (11, 6, 'observation_start_skew')]:
            mac, changed = evidence('mac', 100, count), evidence('ubuntu', 200, count)
            for key in ('started_at', 'finished_at'):
                changed[key] += shift
            for sample in changed['samples']:
                sample['at'] += shift
                sample['finished_at'] += shift
            result = probe.compare_reports([mac, changed], now=epoch('14:01:20'))
            self.assertEqual(result['reason'], reason)

    def test_all_failed_and_too_few_samples_refuse(self):
        changed = evidence('ubuntu', 200)
        for sample in changed['samples']:
            sample.update(public_response_valid=False, error='curl_failed')
        self.assertEqual(self.compare(ubuntu=changed)['reason'], 'insufficient_valid_samples')
        self.assertEqual(self.compare(ubuntu=evidence('ubuntu', 200, count=2))['reason'], 'parameters_mismatch')

    def test_corrupt_shared_parameters_and_success_records_refuse(self):
        for key, value in [('connect_timeout_seconds', 9), ('release_times', ['07:00:99']),
                           ('sample_count', True), ('quiet_before_seconds', 0)]:
            rows = [evidence('mac'), evidence('ubuntu', 200)]
            for row in rows:
                row['parameters'][key] = value
            with self.subTest(key=key):
                self.assertIsNone(probe.compare_reports(rows, epoch('14:00:30'))['advisory_candidate'])
        for modify in (lambda s: s.update(error='curl_failed'),
                       lambda s: s.update(public_captcha_flag='unknown'),
                       lambda s: s['metrics_ms'].update(tls=1000),
                       lambda s: s['metrics_ms'].update(total=float('nan')),
                       lambda s: s.update(at=epoch('14:00:11'))):
            changed = evidence('ubuntu', 200)
            modify(changed['samples'][0])
            self.assertIsNone(self.compare(ubuntu=changed)['advisory_candidate'])

    def test_huge_integer_evidence_refuses_without_exception(self):
        changed = evidence('ubuntu', 200)
        changed['started_at'] = 10 ** 400
        self.assertIsNone(self.compare(ubuntu=changed)['advisory_candidate'])

    def test_success_fraction_precedes_latency(self):
        mac, ubuntu = evidence('mac', 300, count=4), evidence('ubuntu', 100, count=4)
        ubuntu['samples'][0].update(public_response_valid=False, error='curl_failed')
        result = probe.compare_reports([mac, ubuntu], now=epoch('14:00:40'))
        self.assertEqual(result['advisory_candidate'], 'mac')
        self.assertEqual(result['reason'], 'higher_observed_success_fraction')


class FileAndProcessTests(unittest.TestCase):
    def test_exclusive_private_output_preserves_existing_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / 'report.json'
            probe.write_report(target, {'safe': True})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                probe.write_report(target, {'overwrite': True})
            self.assertEqual(probe.read_report(target), {'safe': True})
            link = root / 'alias.json'
            link.symlink_to(target)
            with self.assertRaises(OSError):
                probe.write_report(link, {})
            with self.assertRaises(OSError):
                probe.read_report(link)
            directory = root / 'alias-dir'
            directory.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                probe.write_report(directory / 'new.json', {})

    def test_oversized_report_and_nonregular_input_refuse(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'large.json'
            target.write_bytes(b' ' * (probe.REPORT_LIMIT + 1))
            with self.assertRaises(ValueError):
                probe.read_report(target)
            fifo = Path(folder) / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                probe.read_report(fifo)

    def test_process_output_limit_and_timeout(self):
        with self.assertRaises(ValueError):
            probe.bounded_run([sys.executable, '-B', '-c', 'print("x" * 10000)'], limit=100)
        with self.assertRaises(TimeoutError):
            probe.bounded_run([sys.executable, '-B', '-c', 'import time; time.sleep(2)'], timeout=.05)

    def test_inspect_does_not_use_school_transport_or_conda_commands(self):
        with patch.object(probe, 'command_info', return_value={'available': False}), \
                patch.object(probe.shutil, 'which', return_value=None), \
                patch.object(probe, 'curl_command') as curl, \
                patch.object(probe, 'bounded_run') as runner:
            report = probe.inspect_node('mac')
        curl.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(report['school_network_requests'], 0)
        self.assertEqual(report['native_execution_support'], 'not_verified')
        for forbidden in ('hostname', 'ip_address', 'environment', 'password', 'token'):
            self.assertNotIn(forbidden, report)


if __name__ == '__main__':
    unittest.main()
