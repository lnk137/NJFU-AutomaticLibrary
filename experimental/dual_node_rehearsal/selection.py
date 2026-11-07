"""Offline, unsigned public-probe advice for a synthetic one-owner rehearsal."""
import argparse
from datetime import datetime
import math
import re
from typing import Any

from diagnostics import dual_node_probe as probe

from .contracts import (NODE_IDS, RehearsalError, canonical_hash, envelope,
                        intent_identity, wall_time)


_FAILURES = frozenset(('curl_failed', 'unexpected_http_status', 'invalid_metrics',
                       'invalid_public_response', 'command_timeout', 'output_limit'))
_COMPARABLE = frozenset(('higher_observed_success_fraction', 'lower_observed_latency',
                         'no_clear_network_preference'))


def _reject(code: str = 'invalid_evidence') -> None:
    raise RehearsalError(code)


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _timestamp(value: Any) -> float:
    if not _number(value) or not 0 <= value <= 4102444800:
        _reject()
    return float(value)


def _parameters(value: Any) -> list[str]:
    if type(value) is not dict:
        _reject()
    for name, lower, upper in (('sample_count', 3, 10), ('interval_seconds', 10, 60)):
        if type(value.get(name)) is not int or not lower <= value[name] <= upper:
            _reject('insufficient_samples' if name == 'sample_count' else 'invalid_evidence')
    for name, expected in (('connect_timeout_seconds', 1), ('request_timeout_seconds', 3),
                           ('quiet_before_seconds', 300), ('quiet_after_seconds', 300)):
        if type(value.get(name)) is not int or value[name] != expected:
            _reject()
    releases = value.get('release_times')
    if (type(releases) is not list or not releases or len(releases) > 1440
            or any(type(item) is not str or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]', item)
                   for item in releases)
            or releases != sorted(set(releases)) or '07:00:00' not in releases):
        _reject()
    return releases


def _metrics(value: Any, elapsed: float) -> None:
    if type(value) is not dict or set(value) != {'dns', 'tcp', 'tls', 'ttfb', 'total'}:
        _reject()
    if not all(_number(number) and number >= 0 for number in value.values()):
        _reject()
    if (not 0 < value['total'] <= (probe.REQUEST_SECONDS + 1) * 1000
            or value['dns'] + value['tcp'] + value['tls'] > value['ttfb'] + .003
            or value['ttfb'] > value['total']
            or value['total'] > elapsed * 1000 + 3):
        _reject()


def _report(value: Any, now: float) -> tuple[str, list[str]]:
    if type(value) is not dict or type(value.get('node')) is not str or value['node'] not in NODE_IDS:
        _reject()
    if (type(value.get('schema')) is not int or value['schema'] != 1
            or value.get('kind') != 'sample' or value.get('probe_version') != probe.VERSION
            or value.get('endpoint') != probe.ENDPOINT
            or value.get('measurement') != 'fresh_curl_public_get'
            or value.get('authentication') != 'not_checked'
            or value.get('booking_readiness') != 'not_checked'
            or value.get('automatic_selection_authorized') is not False
            or value.get('clock_jump_detected') is not False):
        _reject()
    if 'clock_accuracy' in value and value['clock_accuracy'] != 'not_verified':
        _reject()
    started, finished = _timestamp(value.get('started_at')), _timestamp(value.get('finished_at'))
    if started > finished:
        _reject()
    # Preserve the probe comparison's bounded five-second host-clock tolerance.
    if not -5 <= now - finished <= 180:
        _reject('stale_or_future_evidence')
    releases = _parameters(value.get('parameters'))
    samples = value.get('samples')
    if type(samples) is not list or len(samples) != value['parameters']['sample_count']:
        _reject('insufficient_samples')
    previous = None
    successes, captcha = 0, []
    latest_success_end = None
    for sample in samples:
        if type(sample) is not dict or type(sample.get('public_response_valid')) is not bool:
            _reject()
        at, end = _timestamp(sample.get('at')), _timestamp(sample.get('finished_at'))
        elapsed = end - at
        if not started <= at <= end <= finished or elapsed > probe.GUARD_BUDGET:
            _reject()
        if previous is not None and at - previous < value['parameters']['interval_seconds'] - .05:
            _reject()
        previous = at
        if probe.guarded(at, releases):
            _reject('release_window')
        if 'metrics_ms' in sample:
            _metrics(sample['metrics_ms'], elapsed)
        if 'http_status' in sample and (type(sample['http_status']) is not int
                                        or not 100 <= sample['http_status'] <= 599):
            _reject()
        if 'public_captcha_flag' in sample:
            if type(sample['public_captcha_flag']) is not str or sample['public_captcha_flag'] not in ('0', '1', '2'):
                _reject()
            captcha.append(sample['public_captcha_flag'])
        if sample['public_response_valid']:
            if ('error' in sample or 'curl_exit_code' in sample
                    or sample.get('http_status') != 200 or 'metrics_ms' not in sample
                    or type(sample.get('public_captcha_flag')) is not str
                    or sample['public_captcha_flag'] not in ('0', '1', '2')):
                _reject()
            if not -5 <= now - end <= 180:
                _reject('stale_or_future_evidence')
            successes += 1
            latest_success_end = end
        else:
            error = sample.get('error')
            if type(error) is not str or error not in _FAILURES:
                _reject()
            if error == 'curl_failed' and (type(sample.get('curl_exit_code')) is not int
                                           or not -255 <= sample['curl_exit_code'] <= 255
                                           or sample['curl_exit_code'] == 0):
                _reject()
            if error != 'curl_failed' and 'curl_exit_code' in sample:
                _reject()
    if successes < 3:
        _reject('insufficient_valid_samples')
    if successes / len(samples) < .75:
        _reject('low_success_fraction')
    # A fresh report header or failed tail cannot refresh old successful measurements.
    if (not -5 <= now - end <= 180 or latest_success_end is None
            or not -5 <= now - latest_success_end <= 180):
        _reject('stale_or_future_evidence')
    if finished - end > probe.GUARD_BUDGET:
        _reject()
    return value['node'], captcha


def _blocked(reason: str) -> dict:
    return envelope(status='blocked', selected_node=None, reason=reason)


def propose(reports: Any, intent: Any, *, eligible_nodes: Any, incumbent: Any, now: Any) -> dict:
    """Recompute public advice; this never creates native execution authority.

    Both original sample reports are required even when one node is ineligible.
    Additional report metadata (including saved summaries) is not interpreted.
    Only structural/integrity checks are possible for these unsigned imports.
    """
    now = wall_time(now)
    task_id, binding = intent_identity(intent)
    if (type(eligible_nodes) not in (list, tuple) or not eligible_nodes
            or any(type(node) is not str or node not in NODE_IDS for node in eligible_nodes)
            or len(set(eligible_nodes)) != len(eligible_nodes)):
        return _blocked('invalid_eligibility')
    if type(incumbent) is not str or incumbent not in NODE_IDS:
        return _blocked('invalid_incumbent')
    eligible = [node for node in NODE_IDS if node in eligible_nodes]
    try:
        if type(reports) not in (list, tuple) or len(reports) != 2:
            _reject()
        nodes, captcha = set(), []
        for value in reports:
            node, flags = _report(value, now)
            nodes.add(node);captcha.extend(flags)
        if nodes != set(NODE_IDS):
            _reject()
        if len(set(captcha)) != 1:
            _reject('captcha_inconsistent')
        if captcha[0] != '0':
            _reject('captcha_required')
        clock = datetime.fromtimestamp(intent['release_at'], probe.ZONE).strftime('%H:%M:%S')
        if any(clock not in value['parameters']['release_times'] for value in reports):
            _reject('release_schedule_mismatch')
        lead = intent['release_at'] - now
        if not 305 <= lead <= 900:
            _reject('outside_selection_window')
        if any(probe.guarded(now, value['parameters']['release_times']) for value in reports):
            _reject('release_window')
        comparison = probe.compare_reports(reports, now=now)
        if type(comparison) is not dict or comparison.get('reason') not in _COMPARABLE:
            reason = comparison.get('reason') if type(comparison) is dict else 'invalid_evidence'
            return _blocked(reason if reason in ('parameters_mismatch', 'nonoverlapping_observations',
                                                  'observation_start_skew') else 'invalid_evidence')
        evidence_hashes = {value['node']: canonical_hash(value) for value in reports}
        comparison['observed_at'] = now
        preferred = comparison.get('advisory_candidate')
        if len(eligible) == 1:
            selected, reason = eligible[0], 'single_eligible_node'
        elif preferred in eligible:
            selected, reason = preferred, comparison['reason']
        else:
            selected, reason = incumbent, 'retain_incumbent'
        return envelope(status='proposed', selected_node=selected, reason=reason,
                        task_id=task_id, binding_sha256=binding, evidence_sha256=evidence_hashes,
                        evaluated_at=now, release_at=intent['release_at'],
                        expires_at=intent['release_at'] + 30, comparison=comparison,
                        eligible_nodes=eligible, incumbent=incumbent,
                        evidence_trust='operator_imported_unsigned', booking_readiness='not_checked',
                        clock_accuracy='not_verified')
    except RehearsalError as exc:
        allowed = {'invalid_evidence', 'stale_or_future_evidence', 'insufficient_samples',
                   'insufficient_valid_samples', 'low_success_fraction', 'release_window',
                   'captcha_inconsistent', 'captcha_required', 'release_schedule_mismatch',
                   'outside_selection_window'}
        return _blocked(exc.code if exc.code in allowed else 'invalid_evidence')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError,
            RecursionError, argparse.ArgumentTypeError):
        return _blocked('invalid_evidence')
