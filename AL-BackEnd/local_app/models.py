"""Typed contracts and pure date/plan validation."""
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo
from .policy import daily_window, validate_window, closing_end

ZONE = ZoneInfo('Asia/Shanghai')
STATES = {'queued', 'preparing', 'submitting', 'uncertain', 'success', 'failed', 'skipped'}


class AppError(Exception):
    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code, self.http_status = code, http_status


@dataclass(frozen=True)
class Identity:
    student_id: str
    display_name: str
    acc_no: str | int


@dataclass(frozen=True)
class Seat:
    id: str
    name: str
    location: str


@dataclass(frozen=True)
class Plan:
    seat_ids: tuple[str, ...]
    day_offset: int
    mode: str
    begin: str
    end: str
    week_times: dict
    trigger: str
    enabled: bool
    time_mode: str = 'custom'


@dataclass(frozen=True)
class SegmentedPlan(Plan):
    """Explicit opt-in keeps legacy Plan/asdict contracts byte-for-byte shaped."""
    segments: list = field(default_factory=list)
    week_segments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AccountView:
    id: str
    student_id: str
    alias: str
    revision: int
    credential_revision: int
    verified_at: str
    plan: dict | None
    date_exceptions: dict = field(default_factory=dict)
    seat_preferences_revision: int = 0
    seat_preferences: dict = field(default_factory=lambda: {'favorites': [], 'groups': []})


@dataclass(frozen=True)
class RunSpec:
    account_id: str
    revision: int
    target_date: str
    begin: str
    end: str
    seat_ids: tuple[str, ...]
    due_at: float
    kind: str = 'scheduled'
    operation_id: str | None = None
    expires_at: float | None = None

    @property
    def key(self):
        base = self.account_id + ':' + self.target_date
        if self.kind == 'scheduled' and isinstance(self.operation_id, str) and re.fullmatch(r'dual:[0-9a-f]{64}', self.operation_id):
            return base + ':' + self.operation_id
        if self.operation_id and self.kind in ('manual', 'scheduled'):
            return base + (':manual:' if self.kind == 'manual' else ':segment:') + self.operation_id
        return base


@dataclass(frozen=True)
class SchoolRecord:
    id: str
    student_id: str
    seat_id: str
    begin: str
    end: str
    status: int | str


@dataclass(frozen=True)
class Submission:
    outcome: str
    retryable: bool = False
    code: int | None = None
    message: str = ''
    sent: bool | None = None
    timings: dict | None = None


@dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    records: list
    verified_at: str | None = None


@dataclass(frozen=True)
class RunPage:
    items: list
    next_cursor: str | None
    counts: dict


def stamp(): return datetime.now(ZONE).isoformat(timespec='seconds')


def credentials(student, password):
    if not isinstance(student, str) or not re.fullmatch(r'[A-Za-z0-9_-]{2,64}', student.strip()):
        raise AppError('invalid_student', '学号格式无效')
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise AppError('invalid_password', '请填写学校统一认证密码，最多 1024 字符')
    return student.strip(), password


def validate_plan(data, catalog, *, single_date: date | None = None):
    if not isinstance(data, dict): raise AppError('invalid_plan', '预约计划须为对象')
    multi = 'segments' in data or 'week_segments' in data
    seats = data.get('seat_ids', [])
    if multi and isinstance(seats, tuple): seats = list(seats)
    enabled = data.get('enabled', False)
    offset = data.get('day_offset', 1)
    mode = data.get('mode', 'day')
    time_mode = data.get('time_mode', 'custom')
    if (not isinstance(seats, list) or len(seats) > 5 or any(not isinstance(s, str) or s not in catalog for s in seats)
            or len(set(seats)) != len(seats) or type(enabled) is not bool
            or type(offset) is not int or offset!=1 or mode not in ('day', 'week') or time_mode not in ('custom','full_day')):
        raise AppError('invalid_plan', '请选择目录中的 0–5 个不同座位及有效预约模式')
    def interval(begin, end, weekday=None):
        if any(not isinstance(t, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in (begin, end)) or begin >= end:
            raise AppError('invalid_time', '使用时间须为 HH:MM，结束晚于开始')
        if weekday==5:end=closing_end(end,weekday)
        try:validate_window(begin,end,weekday)
        except ValueError as exc:raise AppError('school_time',str(exc)) from None
        return begin, end
    if multi and time_mode == 'full_day':
        raise AppError('invalid_segments', '多时段计划请分别填写每段使用时间')
    if time_mode=='full_day':
        mode='day';begin,end=daily_window(None)
    else:
        begin, end = interval(data.get('begin', '09:00'), data.get('end', '12:00'),
                              weekday=single_date.isoweekday() if single_date else None)
        if mode == 'day' and single_date is None and not multi:
            try:interval(begin, end, weekday=5)
            except AppError as exc:
                raise AppError(exc.code, '每日计划周五结束时间最多20:00，截短后须至少2小时') from None
    trigger = data.get('trigger', '')
    if not isinstance(trigger, str) or (trigger and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d', trigger)):
        raise AppError('invalid_trigger', '放号提交时间须为 HH:MM:SS')
    week = {} if time_mode=='full_day' else data.get('week_times', {})
    if not isinstance(week, dict) or any(str(k) not in {str(i) for i in range(1, 8)} for k in week):
        raise AppError('invalid_week', '每周计划日期无效')
    normalized = {}
    for key, value in week.items():
        if value in (None, '', []): continue
        if not isinstance(value, (list, tuple)) or len(value) != 2: raise AppError('invalid_week', '每周计划时段无效')
        normalized[str(key)] = list(interval(*value,weekday=int(key)))
    if multi:
        def segment_list(values, weekday=None):
            if not isinstance(values, list) or not 1 <= len(values) <= 7:
                raise AppError('invalid_segments', '每天请选择1–7个预约时段')
            result, ids = [], set()
            for value in values:
                if not isinstance(value, dict) or set(value) - {'id', 'begin', 'end', 'seat_ids'}:
                    raise AppError('invalid_segments', '每段须填写开始、结束时间及座位')
                start, finish = interval(value.get('begin'), value.get('end'), weekday)
                if weekday is None:
                    interval(start, finish, 5)
                chosen = value.get('seat_ids')
                if (not isinstance(chosen, list) or not 1 <= len(chosen) <= 5
                        or any(not isinstance(s, str) or s not in catalog for s in chosen)
                        or len(set(chosen)) != len(chosen)):
                    raise AppError('invalid_segments', '每段请选择1–5个不同座位，并按优先级排序')
                ident = value.get('id', start.replace(':', '') + '-' + finish.replace(':', ''))
                if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', ident)
                        or ident in ids or ident.startswith('override-')):
                    raise AppError('invalid_segments', '时段标识无效或重复，请重新载入编辑')
                ids.add(ident)
                result.append({'id': ident, 'begin': start, 'end': finish, 'seat_ids': chosen.copy()})
            result.sort(key=lambda item: (item['begin'], item['end'], item['id']))
            if any(left['end'] > right['begin'] for left, right in zip(result, result[1:])):
                raise AppError('segment_overlap', '同一天的预约时段不能重叠，可首尾相接')
            return result
        segments, week_segments = [], {}
        if mode == 'day':
            segments = segment_list(data.get('segments'), single_date.isoweekday() if single_date else None)
        else:
            raw_week = data.get('week_segments')
            if (not isinstance(raw_week, dict) or any(str(k) not in {str(i) for i in range(1, 8)} for k in raw_week)):
                raise AppError('invalid_week', '每周计划日期无效')
            for key, values in raw_week.items():
                if values == []: continue
                week_segments[str(key)] = segment_list(values, int(key))
        if enabled and (not trigger or (mode == 'week' and not week_segments)):
            raise AppError('incomplete_plan', '开启定时前请选择放号时间和使用时段')
        return SegmentedPlan(tuple(seats), offset, mode, begin, end, normalized, trigger, enabled,
                             time_mode, segments, week_segments)
    if enabled and (not seats or not trigger or (mode == 'week' and not normalized)):
        raise AppError('incomplete_plan', '开启定时前请选择座位、放号时间和使用时段')
    return Plan(tuple(seats), offset, mode, begin, end, normalized, trigger, enabled, time_mode)


def resolve_runs(account_id, revision, plan, release_date, date_exceptions=None):
    """Resolve actual use-date windows; overrides replace the complete day's plan."""
    if not plan.enabled or not plan.trigger: return ()
    target = release_date + timedelta(days=plan.day_offset)
    exceptions = date_exceptions or {}
    rule = exceptions.get(target.isoformat())
    if target.isoformat() in exceptions and (not isinstance(rule, dict) or rule.get('action') not in ('skip', 'override')):
        raise AppError('invalid_exception', '日期例外配置需要核对')
    if rule and rule['action'] == 'skip':
        return ()
    due = datetime.fromisoformat(f'{release_date}T{plan.trigger}').replace(tzinfo=ZONE).timestamp()
    if rule and 'segments' in rule:
        # A single-date override can opt an ordinary plan into a segmented
        # cohort without rewriting its recurring shape. Reserve the operation
        # namespace so an override cannot collide with a base-plan segment.
        return tuple(RunSpec(account_id, revision, target.isoformat(), value['begin'],
            closing_end(value['end'], target.isoweekday()), tuple(value['seat_ids']), due,
            operation_id='override-' + target.isoformat() + '-' + value['id'])
            for value in rule['segments'])
    if isinstance(plan, SegmentedPlan):
        if rule:
            values = [dict(rule, id='override-' + target.isoformat())]
        else:
            values = plan.segments if plan.mode == 'day' else plan.week_segments.get(str(target.isoweekday()), [])
        return tuple(RunSpec(account_id, revision, target.isoformat(), value['begin'],
            closing_end(value['end'], target.isoweekday()), tuple(value['seat_ids']), due,
            operation_id=value['id']) for value in values)
    begin, end = plan.begin, plan.end
    seats = plan.seat_ids
    if rule:
        begin, end, seats = rule['begin'], rule['end'], tuple(rule['seat_ids'])
    elif plan.time_mode=='full_day':
        begin,end=daily_window(target.isoweekday())
    elif plan.mode == 'week':
        interval = plan.week_times.get(str(target.isoweekday()))
        if not interval: return ()
        begin, end = interval
    end=closing_end(end,target.isoweekday())
    return (RunSpec(account_id, revision, target.isoformat(), begin, end, seats, due),)


def resolve_run(account_id, revision, plan, release_date, date_exceptions=None):
    runs = resolve_runs(account_id, revision, plan, release_date, date_exceptions)
    return runs[0] if runs else None
