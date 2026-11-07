"""Bounded, secret-free scheduler diagnostics; no IO or booking authority."""
import math

NUMBERS = frozenset({'queued_at', 'started_at', 'finished_at', 'queue_ms', 'elapsed_ms',
    'account_lock_wait_ms', 'school_login_ms', 'booking_preflight_ms', 'session_ms',
    'payload_prepare_ms', 'payload_prepare_total_ms', 'transport_ms', 'payload_remaining_ms',
    'execution_started_at', 'attempt_count', 'next_retry_at', 'stopped_at', 'remaining_ms'})
ERRORS = frozenset({'network_error', 'school_http', 'school_format', 'school_protocol',
    'verification_required', 'login_failed', 'login_incomplete', 'identity_mismatch',
    'native_session_changed', 'native_payload_expired', 'native_session_not_prepared',
    'native_capacity', 'native_warm_busy', 'native_state_changed', 'native_paused',
    'access_denied', 'live_disabled', 'redirect_blocked', 'operation_blocked', 'unexpected_error'})
STEPS = frozenset({'prepare', 'session', 'payload', 'transport', 'warm', 'arm', 'unknown'})
ENUMS = {'error_code': ERRORS, 'error_stage': STEPS,
    'error_kind': frozenset({'AppError', 'TimeoutError', 'OSError', 'ProtocolError',
                             'RuntimeError', 'ValueError', 'unexpected_exception'}),
    'outcome': frozenset({'queued', 'running', 'ready', 'needs_refresh', 'failed', 'finished'}),
    'reason': frozenset({'prepare_failed', 'prepare_window_missed', 'capacity', 'plan_changed'}),
    'waiting_stage': STEPS}


def record_error(phase, exc):
    """Keep codes and known categories, never arbitrary exception text."""
    code = getattr(exc, 'code', None)
    kind = type(exc).__name__
    step = phase.get('active_step', 'unknown')
    phase.update(error_code=code if code in ERRORS else 'unexpected_error',
                 error_kind=kind if kind in ENUMS['error_kind'] else 'unexpected_exception',
                 error_stage=step if step in STEPS else 'unknown')


def safe_phase(values, *, history=True):
    if not isinstance(values, dict):return {}
    safe = {}
    for key in NUMBERS:
        value = values.get(key)
        if type(value) in (int, float) and math.isfinite(value):safe[key] = value
    for key, allowed in ENUMS.items():
        value = values.get(key)
        if isinstance(value, str) and value in allowed:safe[key] = value
    for key in ('retryable', 'refresh_needed'):
        if type(values.get(key)) is bool:safe[key] = values[key]
    if history and isinstance(values.get('attempts'), list):
        safe['attempts'] = [safe_phase(item, history=False) for item in values['attempts'][-11:]
                            if isinstance(item, dict)]
    return safe


def finish_attempt(phase):
    history = phase.setdefault('attempts', [])
    history.append(safe_phase(phase, history=False))
    del history[:-11]


def safe_timings(phases):
    if not isinstance(phases, dict):return {}
    return {stage: safe_phase(phases[stage]) for stage in ('prepare', 'warm', 'submit', 'stop')
            if isinstance(phases.get(stage), dict)}
