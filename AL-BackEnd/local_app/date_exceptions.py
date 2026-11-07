"""Account-owned use-date exceptions and bounded effective schedule search."""
from datetime import date, datetime, timedelta

from .models import AppError, ZONE, resolve_runs, validate_plan

MAX_EXCEPTION_DAYS = 366


def guard_date_edit(now, trigger, first_day=None, *, day_offset=1, affected_dates=None):
    # Local execution binds the whole account revision. Dual execution preserves
    # unchanged tasks, so only edits to a protected task's use date are blocked.
    # Its window matches the dual authentication fence, including commit budget.
    release_days = []
    if affected_dates is not None:
        for target in affected_dates:
            try:
                release_days.append(target - timedelta(days=day_offset))
            except OverflowError:
                continue  # A removal at date.min cannot affect a current task.
        before, after = 183, 40
    else:
        # The runtime bounds a Mongo operation to two seconds. Reserve three
        # seconds before release for its commit, covering both sides of midnight.
        before, after = 3, 30
        for delta in (-1, 0, 1):
            try:
                release_days.append(now.date() + timedelta(days=delta))
            except OverflowError:
                continue
    for release_day in release_days:
        due = datetime.fromisoformat(f'{release_day}T{trigger}').replace(tzinfo=ZONE)
        if -before <= (now - due).total_seconds() <= after:
            raise AppError('account_busy', '临近放号或正在执行，请稍后调整日期', 409)
    if first_day is not None:
        if first_day <= now.date():
            raise AppError('exception_released', '所选日期已经放号，请调整尚未放号的日期；已有预约保留', 409)
        release = datetime.fromisoformat(f'{first_day - timedelta(days=day_offset)}T{trigger}').replace(tzinfo=ZONE)
        if release <= now:
            raise AppError('exception_released', '所选日期已经放号，请调整尚未放号的日期；已有预约保留', 409)


def exception_date(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise AppError('invalid_exception_date', '日期须为 YYYY-MM-DD') from None


def validate_date_exceptions(data, catalog):
    if not isinstance(data, dict) or len(data) > MAX_EXCEPTION_DAYS:
        raise AppError('invalid_exceptions', '日期例外最多保留366天，请先移除不再需要的调整')
    normalized = {}
    for text, rule in data.items():
        day = exception_date(text)
        if not isinstance(rule, dict):
            raise AppError('invalid_exception', '请选择跳过日期或单日调整')
        if rule.get('action') == 'skip' and set(rule) == {'action'}:
            normalized[text] = {'action': 'skip'}
        elif rule.get('action') == 'override' and set(rule) == {'action', 'segments'}:
            segments = rule['segments']
            if (not isinstance(segments, list) or not 1 <= len(segments) <= 7
                    or any(not isinstance(value, dict)
                           or set(value) != {'id', 'begin', 'end', 'seat_ids'} for value in segments)):
                raise AppError('invalid_segments', '单日调整须包含1–7段，每段保留时段标识、时间和座位')
            plan = validate_plan({'segments': segments, 'trigger': '07:00:00', 'enabled': True},
                                 catalog, single_date=day)
            normalized[text] = {'action': 'override', 'segments': plan.segments}
        elif rule.get('action') == 'override' and set(rule) == {'action', 'begin', 'end', 'seat_ids'}:
            plan = validate_plan({'seat_ids': rule['seat_ids'], 'begin': rule['begin'], 'end': rule['end'],
                                  'trigger': '07:00:00', 'enabled': True}, catalog, single_date=day)
            normalized[text] = {'action': 'override', 'begin': plan.begin, 'end': plan.end,
                                'seat_ids': list(plan.seat_ids)}
        else:
            raise AppError('invalid_exception', '日期例外内容无效，请重新选择')
    return normalized


def next_scheduled_run(account_id, revision, plan, now, date_exceptions=None):
    runs = next_scheduled_runs(account_id, revision, plan, now, date_exceptions)
    return runs[0] if runs else None


def next_scheduled_runs(account_id, revision, plan, now, date_exceptions=None):
    if not plan.enabled or not plan.trigger:
        return ()
    exceptions = date_exceptions or {}
    now = now.astimezone(ZONE)
    # Each exception can suppress at most one eligible date; a valid weekly
    # schedule has an eligible date every seven days. No scan to a distant key.
    for delta in range((len(exceptions) + 1) * 7 + 1):
        try:
            release = now.date() + timedelta(days=delta)
            runs = resolve_runs(account_id, revision, plan, release, date_exceptions=exceptions)
        except OverflowError:
            break
        if runs and runs[0].due_at > now.timestamp():
            return runs
    return ()
