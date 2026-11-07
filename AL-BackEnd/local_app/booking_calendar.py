"""Bounded account-owned calendar built only from saved plans and durable evidence."""
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta
import re
import math
import time
import uuid

from . import models
from .date_exceptions import validate_date_exceptions
from .models import AppError, RunSpec, ZONE, validate_plan
from .dual_node.receipt_view import is_dual_row, receipt_view, progress_view

MAX_DAYS = 42
MAX_RUNS = 1000
MAX_BATCHES = 100
MAX_RECORDS = 2000
MAX_CANCELLATIONS = 1000
ACTIVE_STATUS = 1027
_TIME = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d')


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise AppError('invalid_calendar_date', '日历日期须为 YYYY-MM-DD') from None


def _timestamp(value):
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', value):
        raise ValueError('Missing evidence timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=ZONE) if parsed.tzinfo is None else parsed.astimezone(ZONE)


def _record(value, student):
    if (not isinstance(value, dict) or value.get('student_id') != student
            or not isinstance(value.get('id'), str) or not value['id']
            or not isinstance(value.get('seat_id'), str) or not value['seat_id']):
        raise ValueError('Unbound school record')
    begin, end = _timestamp(value.get('begin')), _timestamp(value.get('end'))
    if begin >= end or begin.date() != end.date() or begin.second or end.second or begin.microsecond or end.microsecond:
        raise ValueError('Invalid school record interval')
    accepted = type(value.get('status')) is int and value['status'] == ACTIVE_STATUS
    return {'date': begin.date().isoformat(), 'begin': begin.strftime('%H:%M'), 'end': end.strftime('%H:%M'),
            'seat_ids': [value['seat_id']], 'receipt_id': value['id'], 'accepted': accepted,
            'fingerprint': (student, value['id'], value['seat_id'], begin.isoformat(), end.isoformat(),
                            value.get('status') if type(value.get('status')) in (int, str) else 'unknown')}


def _spec(row, account_id):
    value = row.get('spec')
    if (not isinstance(value, dict) or value.get('account_id') != account_id
            or row.get('account_id') != account_id or row.get('target_date') != value.get('target_date')
            or type(value.get('revision')) is not int or value['revision'] < 1
            or value.get('kind', 'scheduled') not in ('scheduled', 'manual')):
        raise ValueError('Unbound run')
    _date(value.get('target_date'))
    if (any(not isinstance(value.get(k), str) or not _TIME.fullmatch(value[k]) for k in ('begin', 'end'))
            or value['begin'] >= value['end'] or not isinstance(value.get('seat_ids'), (list, tuple))
            or not 1 <= len(value['seat_ids']) <= 5
            or any(not isinstance(s, str) or not s for s in value['seat_ids'])
            or len(set(value['seat_ids'])) != len(value['seat_ids'])):
        raise ValueError('Invalid run interval')
    try:
        values = {f.name: value[f.name] for f in fields(RunSpec) if f.name in value}
        values['seat_ids'] = tuple(value['seat_ids'])
        spec = RunSpec(**values)
        if spec.key != row.get('_id') or (spec.kind == 'manual' and not spec.operation_id):
            raise ValueError('Run key mismatch')
        return spec
    except (TypeError, KeyError):
        raise ValueError('Invalid run spec') from None


class BookingCalendarService:
    def __init__(self, store, catalog, *, wall_clock=None):
        self.store, self.catalog = store, catalog
        self.wall_clock = wall_clock or time.time

    def get(self, account_id, start, end):
        first, last = _date(start), _date(end)
        length = (last - first).days + 1
        if not 1 <= length <= MAX_DAYS:
            raise AppError('invalid_calendar_range', '日历每次须读取连续1–42天')
        account = self.store.get_account(account_id)
        now = datetime.fromtimestamp(self.wall_clock(), ZONE)
        days = {(first + timedelta(days=i)).isoformat(): [] for i in range(length)}
        warnings, warning_keys = [], set()

        def warn(code, message):
            if code not in warning_keys:
                warning_keys.add(code)
                warnings.append({'code': code, 'message': message})

        def add(day, entry):
            if isinstance(day, str) and day in days:
                days[day].append(entry)

        def bounded(collection, maximum, label):
            rows = list(collection.find({'account_id': account_id, 'target_date': {'$gte': start, '$lte': end}})
                        .limit(maximum + 1))
            if len(rows) > maximum:
                raise AppError('calendar_limit', f'{label}超过日历读取上限，请缩小日期范围', 409)
            return rows

        runs = bounded(self.store.runs, MAX_RUNS, '执行记录')
        batches = bounded(self.store.db.campus_today_batches, MAX_BATCHES, '当天批次')
        snapshots = account.get('snapshot', [])
        if not isinstance(snapshots, list):
            snapshots = []
            warn('invalid_snapshot', '保存的学校回执格式异常，请到学校记录页核对')
        if len(snapshots) > MAX_RECORDS:
            raise AppError('calendar_limit', '学校回执超过日历读取上限，请在学校记录页核对', 409)
        try:
            checked = _timestamp(account.get('snapshot_at'))
            snapshot_stale = not 0 <= (now - checked).total_seconds() <= 86400
        except (ValueError, TypeError, OverflowError):
            snapshot_stale = True
        if snapshots and snapshot_stale:
            warn('stale_snapshot', '学校回执来自较早或时间不明的缓存，当前状态请到学校记录页核对')
        if account.get('query_error'):
            warn('snapshot_query_failed', '最近一次学校查询未成功，仍显示先前保存的回执')

        receipt_entries = {}
        # A newer cached record with a non-active status must not be replaced by an older run receipt.
        snapshot_ids, snapshot_bindings = set(), {}
        for index, raw in enumerate(snapshots):
            try:
                record = _record(raw, account['student_id'])
            except (ValueError, TypeError, OverflowError):
                warn('invalid_school_record', '部分学校回执身份或时段异常，未将其认定为成功')
                continue
            snapshot_ids.add(record['receipt_id'])
            if record['date'] not in days:
                continue
            if record['fingerprint'] in receipt_entries:
                continue
            entry = {'id': 'receipt:' + str(index), 'source': 'school_snapshot',
                     'state': 'confirmed' if record['accepted'] else 'uncertain',
                     'begin': record['begin'], 'end': record['end'], 'seat_ids': record['seat_ids'],
                     'receipt_id': record['receipt_id'], 'stale': snapshot_stale,
                     'verified_at': account.get('snapshot_at'), 'run_ids': [],
                     'message': '已保存的学校有效回执' if record['accepted'] else '学校记录状态未确认，请核对'}
            receipt_entries[record['fingerprint']] = entry
            add(record['date'], entry)
            previous = snapshot_bindings.get(record['receipt_id'])
            if previous and previous[0] != record['fingerprint']:
                previous[1].update(state='uncertain', message='同一学校记录存在冲突证据，请核对')
                entry.update(state='uncertain', message='同一学校记录存在冲突证据，请核对')
                warn('conflicting_school_record', '同一学校记录存在不同状态或时段，未将其认定为成功')
            snapshot_bindings[record['receipt_id']] = (record['fingerprint'], entry)
            if not record['accepted']:
                warn('unknown_school_status', '存在非有效或未知状态的学校记录，未将其认定为成功')

        valid_runs, run_entries, dual_entries = {}, {}, {}
        for row in runs:
            try:
                spec = _spec(row, account_id)
            except (ValueError, TypeError, AppError):
                warn('invalid_run', '部分执行记录身份或配置异常，未与计划或成功回执合并')
                day = row.get('target_date')
                add(day, {'id': 'invalid-run:' + str(row.get('_id')), 'source': 'run', 'state': 'uncertain',
                          'begin': '', 'end': '', 'seat_ids': [], 'message': '执行记录异常，请查看执行详情'})
                continue
            valid_runs[spec.key] = spec
            status = row.get('status')
            state = {'queued': 'planned', 'preparing': 'planned', 'failed': 'failed', 'skipped': 'skipped'}.get(status, 'uncertain') if isinstance(status, str) else 'uncertain'
            entry = {'id': 'run:' + spec.key, 'source': 'run', 'state': state, 'run_id': spec.key,
                     'begin': spec.begin, 'end': spec.end, 'seat_ids': list(spec.seat_ids),
                     'kind': spec.kind, 'status': status, 'message': row.get('message', ''),
                     'updated_at': row.get('updated_at'), 'stale_plan': spec.revision != account.get('revision')}
            dual = receipt_view(account, row) if is_dual_row(row) else None
            if dual is not None:
                if dual.phase == 'completed':
                    actual, conflict = {}, dual.snapshot_conflict(account)
                    for raw in snapshots:
                        if dual.commits_record_id(account, raw):
                            try:
                                parsed = _record(raw, account['student_id'])
                            except (ValueError, TypeError, OverflowError):
                                continue
                            if parsed['fingerprint'] in receipt_entries:
                                actual[parsed['fingerprint']] = receipt_entries[parsed['fingerprint']]
                    if len(actual) == 1:
                        entry = next(iter(actual.values()))
                        entry['run_ids'].append(spec.key)
                        if conflict:
                            entry.update(state='uncertain', message='保存的学校记录已变化，双机旧回执摘要不再证明当前有效，请核对')
                    elif conflict:
                        entry.update(state='uncertain', message='保存的学校记录已变化，双机旧回执摘要不再证明当前有效，请核对')
                        add(spec.target_date, entry)
                    else:
                        entry.update(state='confirmed', source='dual_local_verified' if dual.verification_source == 'local_query' else 'dual_node_verified', seat_ids=[dual.seat_id],
                            receipt_ref=dual.receipt_ref, owner=dual.owner,
                            verified_at=datetime.fromtimestamp(dual.result_at, ZONE).isoformat(),
                            message=(f'本机查询已核实学校有效回执；回执摘要：{dual.receipt_ref}' if dual.verification_source == 'local_query' else f'{dual.owner_label}执行节点已查询核验有效回执；回执摘要：{dual.receipt_ref}'))
                        add(spec.target_date, entry)
                else:
                    entry.update(source='dual_node_reported', state=dual.status, owner=dual.owner,
                        message=f'{dual.owner_label}执行节点报告预约失败，请核对学校记录' if dual.phase == 'failed'
                        else f'{dual.owner_label}执行节点报告预约结果待确认，请核对学校记录')
                    add(spec.target_date, entry)
                run_entries[spec.key] = entry
                dual_entries[spec.key] = (dual, entry)
                continue
            progress = progress_view(account, row) if is_dual_row(row) else None
            if progress is not None:
                labels = {'collecting':'双机任务等待选机', 'selected':'双机已选定执行电脑',
                          'ready':'双机执行电脑准备就绪', 'authorized':'双机已授权，等待学校结果',
                          'cancel_requested':'双机执行中止结果待核对', 'cancelled':'双机任务已取消，未发送',
                          'expired_unissued':'双机任务已过期，未签发授权', 'not_sent':'双机任务未发送'}
                entry.update(source='dual_node_reported', owner=progress.owner,
                    state={'queued':'planned','preparing':'planned','skipped':'skipped'}.get(progress.status,'uncertain'),
                    message=labels[progress.phase])
                add(spec.target_date, entry)
                run_entries[spec.key] = entry
                dual_entries[spec.key] = (progress, entry)
                continue
            if is_dual_row(row):
                entry.update(state='uncertain', message='双机结果证据不足，请核对学校记录')
            result = row.get('result')
            matched = None
            if not is_dual_row(row) and status == 'success' and isinstance(result, dict) and result.get('status') == 'success':
                records = result.get('records')
                if isinstance(records, list) and len(records) <= MAX_RECORDS:
                    try:
                        _timestamp(result.get('verified_at'))
                        candidates = {row.get('attempted_seat_id')} | {a.get('seat_id') for a in row.get('attempts', []) if isinstance(a, dict)}
                        for raw in records:
                            record = _record(raw, account['student_id'])
                            if (record['accepted'] and record['date'] == spec.target_date
                                    and record['begin'] == spec.begin and record['end'] == spec.end
                                    and record['seat_ids'][0] in spec.seat_ids
                                    and record['seat_ids'][0] in candidates):
                                matched = record
                                break
                    except (ValueError, TypeError, OverflowError):
                        pass
            if matched:
                existing = receipt_entries.get(matched['fingerprint'])
                if existing:
                    existing['run_ids'].append(spec.key)
                    entry = existing
                elif matched['receipt_id'] not in snapshot_ids:
                    entry.update(state='confirmed', receipt_id=matched['receipt_id'], seat_ids=matched['seat_ids'],
                                 verified_at=result['verified_at'], message='学校有效预约回执已核实', run_ids=[spec.key])
                    receipt_entries[matched['fingerprint']] = entry
                    add(spec.target_date, entry)
                else:
                    entry.update(state='uncertain', message='保存的学校记录已变化，请核对当前状态')
                    add(spec.target_date, entry)
            else:
                if status == 'success':
                    warn('unverified_success', '部分成功记录缺少严格匹配的有效学校回执，显示为待核对')
                    entry['message'] = '执行显示成功，学校有效回执证据不足，请核对'
                add(spec.target_date, entry)
            run_entries[spec.key] = entry

        for batch in batches:
            segments = batch.get('segments')
            if not isinstance(segments, list) or len(segments) > 7:
                warn('invalid_batch', '部分当天批次格式异常，请查看当天预约详情')
                add(batch.get('target_date'), {'id': 'invalid-batch:' + str(batch.get('_id')), 'source': 'today_batch',
                    'state': 'uncertain', 'begin': '', 'end': '', 'seat_ids': [], 'message': '当天批次记录异常，请核对'})
                continue
            for index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    warn('invalid_batch', '部分当天批次时段异常，请查看当天预约详情')
                    continue
                op = uuid.uuid5(uuid.NAMESPACE_URL, str(batch.get('_id')) + ':' + str(index)).hex
                key = f"{account_id}:{batch.get('target_date')}:manual:{op}"
                run = valid_runs.get(key)
                bound = (segment.get('index') == index and segment.get('operation_id') == op
                         and segment.get('run_id') == key and run and run.kind == 'manual'
                         and run.revision == batch.get('revision') and run.begin == segment.get('begin')
                         and run.end == segment.get('end') and list(run.seat_ids) == segment.get('seat_ids'))
                if bound:
                    run_entries[key]['batch_id'] = str(batch['_id'])
                    continue
                status = segment.get('status')
                state = {'pending': 'planned', 'not_submitted': 'skipped', 'skipped': 'skipped', 'failed': 'failed'}.get(status, 'uncertain') if isinstance(status, str) else 'uncertain'
                begin, finish, seats = segment.get('begin'), segment.get('end'), segment.get('seat_ids')
                valid = (isinstance(begin, str) and bool(_TIME.fullmatch(begin)) and isinstance(finish, str)
                         and bool(_TIME.fullmatch(finish)) and begin < finish and isinstance(seats, list)
                         and 1 <= len(seats) <= 5 and all(isinstance(s, str) and s for s in seats))
                if not valid:
                    state, begin, finish, seats = 'uncertain', '', '', []
                    warn('invalid_batch', '部分当天批次时段异常，请查看当天预约详情')
                add(batch.get('target_date'), {'id': f"batch:{batch.get('_id')}:{index}", 'source': 'today_batch',
                    'state': state, 'batch_id': str(batch.get('_id')), 'kind': 'manual', 'begin': begin, 'end': finish,
                    'seat_ids': seats, 'status': status, 'message': segment.get('message', '') if valid else '当天批次时段异常，请核对'})

        cancellations = list(self.store.db.campus_cancellations.find({'account_id': account_id,
            'record.begin': {'$gte': start, '$lte': end + '\uffff'}}).limit(MAX_CANCELLATIONS + 1))
        if len(cancellations) > MAX_CANCELLATIONS:
            raise AppError('calendar_limit', '取消记录超过日历读取上限，请缩小日期范围', 409)
        cancellations.sort(key=lambda row: row.get('requested_at') if type(row.get('requested_at')) in (int, float) else 0)
        for cancellation in cancellations:
            try:
                record = _record(cancellation.get('record'), account['student_id'])
                if not record['accepted']:
                    raise ValueError('Unbound cancellation')
            except (ValueError, TypeError, OverflowError):
                warn('invalid_cancellation', '部分取消记录身份或时段异常，未将其认定为已取消')
                continue
            matches = [entry for fingerprint, entry in receipt_entries.items()
                       if fingerprint[:5] == record['fingerprint'][:5]]
            for dual, entry in dual_entries.values():
                if dual.matches_record(account, cancellation.get('record')) and all(entry is not item for item in matches):
                    matches.append(entry)
            finished = cancellation.get('completed_at')
            accepted = (cancellation.get('state') == 'success' and cancellation.get('ack') is True
                        and type(finished) in (int, float) and math.isfinite(finished))
            if accepted:
                if not matches:
                    entry = {'id': 'cancellation:' + str(cancellation.get('_id')), 'source': 'cancellation',
                             'begin': record['begin'], 'end': record['end'], 'seat_ids': record['seat_ids'],
                             'receipt_id': record['receipt_id']}
                    add(record['date'], entry)
                    matches = [entry]
                for entry in matches:
                    entry.update(previous_state=entry.get('state'), state='cancelled',
                                 cancellation_id=str(cancellation.get('_id')), cancelled_at=finished,
                                 message='学校已确认取消此条预约')
            elif cancellation.get('state') in ('submitting', 'uncertain'):
                for entry in matches:
                    entry.update(cancellation_state=cancellation['state'], cancellation_message='取消结果待核对')
                warn('cancellation_unverified', '存在结果待核对的取消操作，未将其认定为已取消')

        plan_valid = True
        try:
            exceptions = validate_date_exceptions(account.get('date_exceptions', {}), self.catalog)
            plan = validate_plan(account['plan'], self.catalog) if account.get('plan') is not None else None
            for day in days:
                rule = exceptions.get(day)
                if rule and rule['action'] == 'skip':
                    add(day, {'id': 'skip:' + day, 'source': 'exception', 'state': 'skipped', 'begin': '', 'end': '',
                              'seat_ids': [], 'exception': 'skip', 'message': '已保存的临时跳过' + ('（计划已暂停）' if plan and not plan.enabled else '')})
                    continue
                if plan:
                    target = _date(day)
                    if target < now.date():
                        continue
                    release = target - timedelta(days=plan.day_offset)
                    resolved = models.resolve_runs(account_id, account['revision'], plan, release, date_exceptions=exceptions)
                    for spec in resolved:
                        durable = valid_runs.get(spec.key)
                        if durable and asdict(durable) == asdict(spec):
                            continue
                        matches = [(dual, entry) for dual, entry in dual_entries.values() if dual.matches_plan(spec, account.get('credential_revision'))]
                        if matches:
                            for _, entry in matches: entry['stale_plan'] = False
                            continue
                        overdue = spec.due_at < now.timestamp()
                        add(day, {'id': 'plan:' + spec.key, 'source': 'plan', 'state': 'planned',
                            'begin': spec.begin, 'end': spec.end, 'seat_ids': list(spec.seat_ids), 'release_at': datetime.fromtimestamp(spec.due_at, ZONE).isoformat(),
                            'exception': rule['action'] if rule else None, 'overdue': overdue,
                            'message': '当前计划；放号已过，请核对执行记录' if overdue else '已保存的计划，尚未获得学校确认'})
        except (AppError, ValueError, TypeError, OverflowError, KeyError) as exc:
            plan_valid = False
            warn('invalid_plan', '保存的计划或日期调整无效，日历未展开计划：' + str(exc))

        for entries in days.values():
            entries.sort(key=lambda entry: (entry['begin'] or '99:99', entry['end'], entry['id']))
        return {'account_id': account_id, 'start': start, 'end': end, 'timezone': 'Asia/Shanghai',
                'generated_at': now.isoformat(), 'snapshot_at': account.get('snapshot_at'), 'plan_valid': plan_valid,
                'has_saved_plan': account.get('plan') is not None, 'warnings': warnings,
                'days': [{'date': day, 'entries': entries} for day, entries in days.items()]}
