"""Offline selection contracts; every report and account identifier is synthetic."""
import copy
from datetime import datetime
import unittest
from unittest.mock import patch

from experimental.dual_node_rehearsal.contracts import RehearsalError, intent_identity
from experimental.dual_node_rehearsal import selection


RELEASE = datetime.fromisoformat('2026-11-06T07:00:00+08:00').timestamp()
NOW = RELEASE - 600


def intent():
    return {'schema': 1, 'account_ref': 'fixture-account', 'day': '2026-11-07',
            'kind': 'S', 'operation': '-', 'plan_version': 1,
            'start_minute': 480, 'end_minute': 600, 'seat_ref': 'fixture-seat',
            'release_at': RELEASE}


def report(node, total=100, count=3, now=NOW):
    start = now - count * 10
    samples = [{'at': start + index * 10,
                'finished_at': start + index * 10 + total / 1000 + .02,
                'public_response_valid': True, 'http_status': 200,
                'public_captcha_flag': '0',
                'metrics_ms': {'dns': 1., 'tcp': 3., 'tls': 6., 'ttfb': 20., 'total': total}}
               for index in range(count)]
    return {'schema': 1, 'probe_version': '1', 'kind': 'sample', 'node': node,
            'endpoint': selection.probe.ENDPOINT, 'measurement': 'fresh_curl_public_get',
            'authentication': 'not_checked', 'booking_readiness': 'not_checked',
            'automatic_selection_authorized': False, 'clock_accuracy': 'not_verified',
            'clock_jump_detected': False, 'observed_at': start - .001,
            'started_at': start, 'finished_at': samples[-1]['finished_at'], 'samples': samples,
            'parameters': {'sample_count': count, 'interval_seconds': 10,
                           'connect_timeout_seconds': 1, 'request_timeout_seconds': 3,
                           'quiet_before_seconds': 300, 'quiet_after_seconds': 300,
                           'release_times': ['07:00:00']}}


def shift(value, seconds):
    for key in ('started_at', 'finished_at', 'observed_at'):
        value[key] += seconds
    for sample in value['samples']:
        sample['at'] += seconds
        sample['finished_at'] += seconds


class SelectionTests(unittest.TestCase):
    def choose(self, rows=None, **kwargs):
        return selection.propose(rows if rows is not None else [report('mac'), report('ubuntu', 200)],
                                 kwargs.pop('intent', intent()),
                                 eligible_nodes=kwargs.pop('eligible_nodes', ['mac', 'ubuntu']),
                                 incumbent=kwargs.pop('incumbent', 'mac'), now=kwargs.pop('now', NOW), **kwargs)

    def test_valid_reports_recompute_preference_and_preserve_inputs(self):
        rows = [report('mac'), report('ubuntu', 200)]
        original = copy.deepcopy(rows)
        with patch.object(selection.probe, 'compare_reports', wraps=selection.probe.compare_reports) as compare:
            result = self.choose(rows)
        self.assertEqual(result['status'], 'proposed')
        self.assertEqual(result['selected_node'], 'mac')
        self.assertEqual(result['reason'], 'lower_observed_latency')
        self.assertEqual((result['task_id'], result['binding_sha256']), intent_identity(intent()))
        self.assertEqual(result['release_at'], RELEASE)
        self.assertEqual(result['expires_at'], RELEASE + 30)
        self.assertEqual(set(result['evidence_sha256']), {'mac', 'ubuntu'})
        self.assertTrue(all(len(h) == 64 for h in result['evidence_sha256'].values()))
        self.assertEqual(result['evidence_trust'], 'operator_imported_unsigned')
        self.assertEqual(result['booking_readiness'], 'not_checked')
        self.assertEqual(result['clock_accuracy'], 'not_verified')
        self.assertTrue(result['rehearsal_only']);self.assertFalse(result['live_execution_authorized'])
        self.assertEqual(rows, original)
        compare.assert_called_once()

    def test_tie_retains_explicit_eligible_incumbent(self):
        result = self.choose([report('mac', 100), report('ubuntu', 105)], incumbent='ubuntu')
        self.assertEqual(result['selected_node'], 'ubuntu')
        self.assertEqual(result['reason'], 'retain_incumbent')

    def test_explicit_single_eligible_node_precedes_network_rank(self):
        result = self.choose(eligible_nodes=['ubuntu'])
        self.assertEqual(result['selected_node'], 'ubuntu')
        self.assertEqual(result['reason'], 'single_eligible_node')

    def test_invalid_or_missing_eligibility_and_incumbent_block(self):
        for value in ([], ['mac', 'mac'], ['other'], 'mac', [True], None):
            with self.subTest(eligible=value):
                self.assertEqual(self.choose(eligible_nodes=value)['status'], 'blocked')
        self.assertEqual(self.choose(incumbent='other')['status'], 'blocked')

    def test_saved_or_forged_summary_is_never_authoritative(self):
        rows = [report('mac'), report('ubuntu', 200)]
        for row in rows:
            row['summaries'] = [{'node': 'ubuntu', 'median_ms': .001}]
            row['advisory_candidate'] = 'ubuntu'
        self.assertEqual(self.choose(rows)['selected_node'], 'mac')
        self.assertEqual(self.choose([{'kind': 'compare', 'advisory_candidate': 'mac'}] * 2)['status'], 'blocked')

    def test_wrong_node_count_labels_and_raw_shapes_block_without_exception(self):
        for rows in (None, {}, 'reports', [], [report('mac')], [report('mac'), report('mac')],
                     [report('mac'), None], [[], {}]):
            with self.subTest(rows=str(rows)[:40]):
                self.assertEqual(selection.propose(rows, intent(), eligible_nodes=['mac', 'ubuntu'],
                                                    incumbent='mac', now=NOW)['status'], 'blocked')

    def test_exact_types_measurement_and_disclaimers_are_required(self):
        for key, value in [('schema', True), ('kind', 'compare'), ('probe_version', '2'),
                           ('endpoint', 'https://other.invalid'), ('measurement', 'other_transport'),
                           ('clock_jump_detected', 0), ('automatic_selection_authorized', 0),
                           ('booking_readiness', 'ready'), ('authentication', 'verified')]:
            rows = [report('mac'), report('ubuntu', 200)];rows[0][key] = value
            with self.subTest(key=key):self.assertEqual(self.choose(rows)['status'], 'blocked')
        for key in ('sample_count', 'interval_seconds', 'connect_timeout_seconds', 'request_timeout_seconds',
                    'quiet_before_seconds', 'quiet_after_seconds'):
            rows = [report('mac'), report('ubuntu', 200)]
            for row in rows:row['parameters'][key] = True
            with self.subTest(parameter=key):self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_stale_future_and_nonfinite_evidence_block(self):
        for seconds in (-181, 30):
            rows = [report('mac'), report('ubuntu', 200)]
            for row in rows:shift(row, seconds)
            with self.subTest(shift=seconds):self.assertEqual(self.choose(rows)['reason'], 'stale_or_future_evidence')
        for value in (True, float('nan'), float('inf'), 10 ** 400):
            rows = [report('mac'), report('ubuntu', 200)];rows[0]['samples'][0]['at'] = value
            with self.subTest(value=str(value)[:30]):self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_retagged_header_cannot_refresh_stale_actual_samples(self):
        rows = [report('mac', now=NOW - 3600), report('ubuntu', 200, now=NOW - 3600)]
        for row in rows:row['finished_at'] = NOW
        result = self.choose(rows)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'stale_or_future_evidence')
        self.assertFalse(result['live_execution_authorized'])

    def test_actual_last_sample_freshness_boundaries(self):
        # Earlier successful samples must also fit within the freshness window.
        for age, proposed in ((160, True), (180, False), (180.001, False), (-5, True), (-5.001, False)):
            rows = [report('mac'), report('ubuntu', 200)]
            for row in rows:
                shift(row, NOW - age - row['samples'][-1]['finished_at'])
                row['finished_at'] += selection.probe.GUARD_BUDGET if age >= 0 else 0
            result = self.choose(rows)
            with self.subTest(age=age):
                self.assertEqual(result['status'] == 'proposed', proposed)
                if not proposed:self.assertEqual(result['reason'], 'stale_or_future_evidence')

    def test_report_completion_gap_is_bounded_by_the_guard_budget(self):
        budget = selection.probe.GUARD_BUDGET
        for gap, proposed in ((0, True), (budget, True), (budget + .001, False)):
            rows = [report('mac'), report('ubuntu', 200)]
            for row in rows:row['finished_at'] = row['samples'][-1]['finished_at'] + gap
            result = self.choose(rows)
            with self.subTest(gap=gap):
                self.assertEqual(result['status'] == 'proposed', proposed)
                if not proposed:self.assertEqual(result['reason'], 'invalid_evidence')

    def test_fresh_failed_tail_cannot_refresh_expired_successful_measurements(self):
        rows = [report('mac', count=4), report('ubuntu', 200, count=4)]
        for row in rows:
            shift(row, NOW - 181 - row['samples'][-2]['finished_at'])
            row['samples'][-1].update(public_response_valid=False, error='curl_failed', curl_exit_code=28)
        result = self.choose(rows)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'stale_or_future_evidence')

    def test_six_samples_at_ten_second_intervals_remain_accepted(self):
        rows = [report('mac', count=6), report('ubuntu', 200, count=6)]
        for row in rows:row['finished_at'] += 2
        result = self.choose(rows)
        self.assertEqual(result['status'], 'proposed')
        self.assertEqual(result['selected_node'], 'mac')
        self.assertFalse(result['live_execution_authorized'])

    def test_recent_success_cannot_refresh_old_successful_latency_samples(self):
        rows = [report('mac', count=6), report('ubuntu', 200, count=6)]
        for row in rows:
            for sample in row['samples'][:-1]:
                sample['at'] -= 3600
                sample['finished_at'] -= 3600
            row['started_at'] = row['samples'][0]['at']
            row['observed_at'] = row['started_at'] - .001
        result = self.choose(rows)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'stale_or_future_evidence')
        self.assertFalse(result['live_execution_authorized'])

    def test_all_successful_sample_freshness_boundaries(self):
        for age, proposed in ((180, True), (180.001, False)):
            rows = [report('mac'), report('ubuntu', 200)]
            for row in rows:
                first = row['samples'][0]
                elapsed = first['finished_at'] - first['at']
                first['finished_at'] = NOW - age
                first['at'] = first['finished_at'] - elapsed
                row['started_at'] = first['at']
                row['observed_at'] = row['started_at'] - .001
            result = self.choose(rows)
            with self.subTest(age=age):
                self.assertEqual(result['status'] == 'proposed', proposed)
                if not proposed:self.assertEqual(result['reason'], 'stale_or_future_evidence')

    def test_ten_samples_at_sixty_second_intervals_require_fresh_successes(self):
        rows = [report('mac', count=10), report('ubuntu', 200, count=10)]
        for row in rows:
            row['parameters']['interval_seconds'] = 60
            last_end = row['samples'][-1]['finished_at']
            for index, sample in enumerate(row['samples']):
                elapsed = sample['finished_at'] - sample['at']
                sample['finished_at'] = last_end - (len(row['samples']) - index - 1) * 60
                sample['at'] = sample['finished_at'] - elapsed
            row['started_at'] = row['samples'][0]['at']
            row['observed_at'] = row['started_at'] - .001
        result = self.choose(rows)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'stale_or_future_evidence')

    def test_bad_metrics_shapes_and_elapsed_consistency_block(self):
        for metrics in (['dns', 'tcp', 'tls', 'ttfb', 'total'], None, {},
                        {'dns': 1, 'tcp': 3, 'tls': 6, 'ttfb': 20, 'total': True},
                        {'dns': 1, 'tcp': 3, 'tls': 60, 'ttfb': 20, 'total': 100},
                        {'dns': 1, 'tcp': 3, 'tls': 6, 'ttfb': 200, 'total': 100}):
            rows = [report('mac'), report('ubuntu', 200)];rows[0]['samples'][0]['metrics_ms'] = metrics
            with self.subTest(metrics=metrics):self.assertEqual(self.choose(rows)['status'], 'blocked')
        rows = [report('mac'), report('ubuntu', 200)]
        rows[0]['samples'][0]['finished_at'] = rows[0]['samples'][0]['at'] + .001
        self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_bad_sample_boolean_http_and_failure_records_block(self):
        for edit in (lambda s:s.update(public_response_valid=1), lambda s:s.update(http_status=True),
                     lambda s:s.update(public_response_valid=False),
                     lambda s:s.update(public_response_valid=False,error='arbitrary_error'),
                     lambda s:s.update(error='curl_failed')):
            rows = [report('mac'), report('ubuntu', 200)];edit(rows[0]['samples'][0])
            self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_captcha_required_or_inconsistent_blocks(self):
        for flags in (('1', '1'), ('2', '2'), ('0', '1'), ('0', '2'), ('0', True)):
            rows = [report('mac'), report('ubuntu', 200)]
            for row, flag in zip(rows, flags):
                for sample in row['samples']:sample['public_captcha_flag'] = flag
            with self.subTest(flags=flags):self.assertEqual(self.choose(rows)['status'], 'blocked')
        rows = [report('mac', count=4), report('ubuntu', 200, count=4)]
        rows[1]['samples'][0].update(public_response_valid=False, error='curl_failed',
                                    curl_exit_code=28, public_captcha_flag='1')
        self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_failed_samples_require_three_successes_and_seventy_five_percent(self):
        rows = [report('mac', 100, count=4), report('ubuntu', 200, count=4)]
        rows[1]['samples'][0].update(public_response_valid=False,error='curl_failed',curl_exit_code=28)
        self.assertEqual(self.choose(rows)['status'], 'proposed')
        rows = [report('mac', count=5), report('ubuntu', 200, count=5)]
        for sample in rows[1]['samples'][:2]:
            sample.update(public_response_valid=False,error='curl_failed',curl_exit_code=28)
        self.assertEqual(self.choose(rows)['reason'], 'low_success_fraction')
        rows = [report('mac'), report('ubuntu', 200)]
        rows[1]['samples'][0].update(public_response_valid=False,error='curl_failed',curl_exit_code=28)
        self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_report_parameters_spacing_and_window_corruption_block(self):
        for edit in (lambda r:r['parameters'].update(interval_seconds=11),
                     lambda r:r['parameters'].update(release_times=['07:00:00', '07:00:00']),
                     lambda r:r['parameters'].update(release_times=['07:60:00']),
                     lambda r:r['samples'][1].update(at=r['samples'][0]['at'] + 1),
                     lambda r:r.update(started_at=r['finished_at'] + 1),
                     lambda r:r['samples'][0].update(finished_at=r['finished_at'] + 1)):
            rows = [report('mac'), report('ubuntu', 200)];edit(rows[0])
            self.assertEqual(self.choose(rows)['status'], 'blocked')

    def test_release_schedule_must_contain_declared_clock_time(self):
        changed = intent();changed['release_at'] += 3600
        result = self.choose(intent=changed)
        self.assertEqual(result['reason'], 'release_schedule_mismatch')

    def test_nonoverlap_start_skew_and_comparison_parameters_block(self):
        rows = [report('mac', now=NOW - 50), report('ubuntu', 200)]
        self.assertEqual(self.choose(rows)['reason'], 'nonoverlapping_observations')
        rows = [report('mac', count=6), report('ubuntu', 200, count=6)]
        shift(rows[1], -11)
        self.assertEqual(self.choose(rows)['reason'], 'observation_start_skew')
        rows = [report('mac', count=3), report('ubuntu', 200, count=4)]
        self.assertEqual(self.choose(rows)['reason'], 'parameters_mismatch')

    def test_midnight_release_is_matched_in_beijing_time(self):
        release = datetime.fromisoformat('2026-11-07T00:02:00+08:00').timestamp()
        changed = intent();changed['release_at'] = release
        now = release - 600
        rows = [report('mac', now=now), report('ubuntu', 200, now=now)]
        for row in rows:row['parameters']['release_times'] = ['00:02:00', '07:00:00']
        self.assertEqual(self.choose(rows, intent=changed, now=now)['status'], 'proposed')

    def test_freeze_window_and_quiet_guard_are_preserved(self):
        for lead, proposed in ((901, False), (900, True), (306, True), (305, False), (304, False)):
            now = RELEASE - lead
            result = self.choose([report('mac', now=now), report('ubuntu', 200, now=now)], now=now)
            with self.subTest(lead=lead):self.assertEqual(result['status'] == 'proposed', proposed)

    def test_quiet_samples_are_rejected_even_without_error_marker(self):
        now = RELEASE - 306
        rows = [report('mac', now=now), report('ubuntu', 200, now=now)]
        for row in rows:row['parameters']['release_times'] = ['06:50:00', '07:00:00']
        self.assertEqual(self.choose(rows, now=now)['status'], 'blocked')

    def test_invalid_intent_and_now_raise_contract_errors(self):
        changed = intent();changed['account_ref'] = 'real-account'
        with self.assertRaises(RehearsalError):self.choose(intent=changed)
        with self.assertRaises(RehearsalError):self.choose(now=True)


if __name__ == '__main__':
    unittest.main()
