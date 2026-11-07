"""Claim once, then execute bounded same-day segments without replay or rollback."""
import base64
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
import threading
import time
import uuid

from pymongo.errors import DuplicateKeyError

from .manual_windows import record_windows, require_free_window
from .models import AppError, RunResult, RunSpec, ZONE, stamp
from .recovery_policy import clear_busy_failure


class TodayBatchService:
    MAX_PENDING = 8
    PURPOSE = 'today_batch_v1'

    def __init__(self, today):
        self.today = today
        self.accounts, self.reservations, self.store = today.accounts, today.reservations, today.store
        self.batches = self.store.db.campus_today_batches
        self._stopping = threading.Event()
        self._dispatch = threading.Lock()
        self._slots = threading.BoundedSemaphore(self.MAX_PENDING)
        self._cleanup_lock = threading.Lock()
        self._cleanup_needed = threading.Event()
        self._cleanup_pending = {}
        self._signing_key = hmac.new(self.accounts.vault.key, b'today-batches-v1', hashlib.sha256).digest()
        self._recover()
        self.batches.create_index([('account_id', 1), ('created_at', -1), ('_id', -1)])
        self.batches.create_index('account_id', unique=True, partialFilterExpression={'state': 'running'},
                                  name='one_running_today_batch')
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='today-batch')
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, name='today-batch-cleanup', daemon=True)
        self._cleanup_thread.start()

    def _now(self):
        return self.reservations.wall_clock()

    def _verified_result(self, batch, segment):
        """Project a later receipt only from this exact segment's durable manual run."""
        if segment['status'] != 'uncertain':
            return None
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, batch['_id'] + ':' + str(segment['index'])).hex
        key = f"{batch['account_id']}:{batch['target_date']}:manual:{operation_id}"
        if segment.get('operation_id') != operation_id or segment.get('run_id') != key:
            return None
        run = self.store.get_run(key)
        if (not run or run.get('_id') != key or run.get('status') != 'success'
                or run.get('account_id') != batch['account_id'] or run.get('target_date') != batch['target_date']):
            return None
        spec, result = run.get('spec'), run.get('result')
        expected = {'kind': 'manual', 'operation_id': operation_id, 'account_id': batch['account_id'],
                    'revision': batch['revision'], 'target_date': batch['target_date'],
                    'begin': segment['begin'], 'end': segment['end']}
        if (not isinstance(spec, dict) or type(spec.get('revision')) is not int
                or any(spec.get(key) != value for key, value in expected.items())
                or not isinstance(spec.get('seat_ids'), (list, tuple))
                or list(spec['seat_ids']) != segment['seat_ids']
                or not isinstance(result, dict) or result.get('status') != 'success'
                or not isinstance(result.get('records'), list) or not result['records']
                or not isinstance(result.get('verified_at'), str) or not result['verified_at']):
            return None
        return {key: result.get(key) for key in ('status', 'message', 'records', 'verified_at')}

    def _view(self, batch):
        fields = ('index', 'begin', 'end', 'seat_ids', 'seats', 'status', 'message', 'run_id', 'result')
        segments, updated = [], False
        for segment in batch['segments']:
            view = {key: segment.get(key) for key in fields}
            result = self._verified_result(batch, segment)
            if result:
                view.update(status='success', message=result['message'], result=result)
                updated = True
            segments.append(view)
        message = ('学校回执已更新；批次保持停止，剩余时段未提交，不会自动重交'
                   if updated and batch['state'] == 'stopped' else batch.get('message', ''))
        return {'batch_id': batch['_id'], 'date': batch['target_date'], 'state': batch['state'],
                'message': message, 'segments': segments}

    def get(self, account_id, batch_id):
        batch = self.batches.find_one({'_id': batch_id, 'account_id': account_id})
        if not batch:
            raise AppError('not_found', '当天批次不存在', 404)
        return self._view(batch)

    def latest(self, account_id):
        batch = self.batches.find_one({'account_id': account_id}, sort=[('created_at', -1), ('_id', -1)])
        return {'batch': self._view(batch) if batch else None}

    def active(self, account_id):
        return bool(self.batches.find_one({'account_id': account_id, 'state': 'running'}, {'_id': 1}))

    def _quiet(self):
        # Match FormImportService's protected preparation window for every enabled plan.
        rows = list(self.store.accounts.find({'plan.enabled': True}, {'plan.trigger': 1}).limit(5001).max_time_ms(1000))
        if len(rows) > 5000:
            raise AppError('batch_limit', '开启的计划过多，暂不能执行当天批次', 409)
        now = datetime.fromtimestamp(self._now(), ZONE)
        triggers = {'07:00:00'} | {row['plan'].get('trigger') for row in rows}
        for trigger in triggers:
            for delta in (-1, 0, 1):
                try:
                    due = datetime.fromisoformat(f'{now.date() + timedelta(days=delta)}T{trigger}').replace(tzinfo=ZONE)
                except (ValueError, TypeError):
                    continue
                if -183 <= (now - due).total_seconds() <= 40:
                    raise AppError('release_window', '临近定时预约准备或执行窗口，已停止当天批次，请稍后重新预检', 409)

    def _normalize(self, account_id, data):
        segments = data.get('segments')
        if not isinstance(segments, list) or not 1 <= len(segments) <= 7:
            raise AppError('invalid_segments', '请选择1–7个当天预约时段')
        normalized, starts = [], []
        for segment in segments:
            if not isinstance(segment, dict) or set(segment) != {'begin', 'end', 'seat_ids'}:
                raise AppError('invalid_segments', '每段须填写开始、结束时间及座位')
            row, day, plan, start = self.today._validate(account_id, {
                'expected_revision': data.get('expected_revision'), 'date': data.get('date'), **segment})
            normalized.append({'begin': plan.begin, 'end': plan.end, 'seat_ids': list(plan.seat_ids)})
            starts.append(start)
        normalized.sort(key=lambda item: (item['begin'], item['end']))
        if any(left['end'] > right['begin'] for left, right in zip(normalized, normalized[1:])):
            raise AppError('batch_overlap', '各段预约时间不能重叠，相邻时段可以连续', 400)
        return row, day, normalized, min(starts)

    def _pending(self, account_id, day):
        if account_id in self.accounts.busy:
            raise AppError('account_busy', '此账号正在执行预约，请稍后再试', 409)
        if self.store.runs.find_one({'account_id': account_id, 'target_date': day,
                                    'status': {'$in': ['preparing', 'submitting', 'uncertain']}}):
            raise AppError('booking_uncertain', '该账号当天有待确认任务，请先核对学校记录', 409)

    def _school(self, row, day, segments):
        self._pending(row['_id'], day)
        client = self.accounts.client(row['_id'])
        prepare = getattr(client, 'prepare_booking', None)
        if prepare:
            prepare()
        query_day = datetime.fromisoformat(day).date()
        records = client.query(query_day, query_day + timedelta(days=3))
        windows = record_windows(records, row['student_id'], getattr(client, 'active_statuses', frozenset()))
        self.store.save_snapshot(row['_id'], row['revision'], records, stamp())
        for segment in segments:
            require_free_window(windows, day, segment['begin'], segment['end'])

    def _encode(self, claims):
        encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')
        signature = hmac.new(self._signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + '.' + signature

    def _decode(self, token):
        try:
            if not isinstance(token, str) or len(token) > 16384:
                raise ValueError()
            encoded, signature = token.split('.')
            expected = hmac.new(self._signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
            if (not isinstance(claims, dict) or set(claims) != {'purpose', 'batch_id', 'account_id',
                    'revision', 'target_date', 'segments', 'expires_at'} or claims['purpose'] != self.PURPOSE
                    or uuid.UUID(claims['batch_id']).hex != claims['batch_id']
                    or not isinstance(claims['account_id'], str) or type(claims['revision']) is not int
                    or type(claims['expires_at']) not in (int, float) or not math.isfinite(claims['expires_at'])):
                raise ValueError()
            return claims
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise AppError('invalid_preview', '当天批次预检已失效，请重新预检', 400) from None

    def preview(self, account_id, data):
        self._quiet()
        if self._stopping.is_set():
            raise AppError('execution_paused', '后台正在停止，请稍后重新预检', 503)
        lock = self.accounts.account_lock(account_id)
        if not lock.acquire(blocking=False):
            raise AppError('account_busy', '此账号正在使用，请稍后预检', 409)
        try:
            if self.active(account_id):
                raise AppError('account_busy', '此账号有正在执行的当天批次，请先查看结果', 409)
            row, day, segments, _ = self._normalize(account_id, data)
            self._school(row, day, segments)
            # Slow reads cannot produce a preview whose first interval has already begun.
            row, day, segments, start = self._normalize(account_id, data)
            self._quiet()
            expiry = min(self._now() + 120, start - 1)
            if expiry <= self._now():
                raise AppError('invalid_time', '开始时间过近，请选择下一时段')
            batch_id = uuid.uuid4().hex
            claims = {'purpose': self.PURPOSE, 'batch_id': batch_id, 'account_id': account_id,
                      'revision': row['revision'], 'target_date': day, 'segments': segments, 'expires_at': expiry}
            return {'batch_id': batch_id, 'date': day,
                    'segments': [{'index': index, 'begin': segment['begin'], 'end': segment['end'],
                                  'seats': [asdict(self.accounts.catalog[key]) for key in segment['seat_ids']]}
                                 for index, segment in enumerate(segments)],
                    'expires_at': datetime.fromtimestamp(expiry, ZONE).isoformat(), 'preview_token': self._encode(claims)}
        finally:
            lock.release()

    def _check(self, batch, segment=None):
        if self._stopping.is_set():
            raise AppError('execution_paused', '后台正在停止，剩余时段未提交', 503)
        if not self.reservations.allow_live:
            raise AppError('live_disabled', '真实预约执行未开启，剩余时段未提交', 403)
        if not self.reservations.guard():
            raise AppError('execution_paused', '后台时间校验或执行状态异常，剩余时段未提交', 503)
        if self._now() >= batch['expires_at']:
            raise AppError('preview_expired', '预检已过期，剩余时段未提交，请重新预检', 409)
        row = self.store.get_account(batch['account_id'])
        if row['revision'] != batch['revision']:
            raise AppError('conflict', '账号配置已变化，剩余时段未提交', 409)
        if not self.accounts.is_allowed(row['student_id']):
            raise AppError('access_denied', '此学号的预约权限已停用，剩余时段未提交', 403)
        for item in [segment] if segment else batch['segments']:
            self.today._validate(batch['account_id'], {'expected_revision': batch['revision'],
                'date': batch['target_date'], 'begin': item['begin'], 'end': item['end'], 'seat_ids': item['seat_ids']})
        self._quiet()
        return row

    def _submission_clear(self):
        try:
            self._quiet()
            return not self._stopping.is_set()
        except Exception:
            return False

    def submit(self, account_id, token):
        claims = self._decode(token)
        if claims['account_id'] != account_id:
            raise AppError('invalid_preview', '预检账号不匹配，请重新预检', 403)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._dispatch:
            old = self.batches.find_one({'_id': claims['batch_id']})
            if old:
                if old['account_id'] != account_id or old['claim_digest'] != digest:
                    raise AppError('invalid_preview', '批次预检内容不匹配，请重新预检', 409)
                return self._view(old)
            with self._cleanup_lock:
                cleanup_pending = claims['batch_id'] in self._cleanup_pending
            if cleanup_pending:
                # A missing read during ambiguous claim cleanup is not a new authorization.
                raise AppError('batch_interrupted', '原批次仍在核对未提交状态，请稍后刷新进度，勿重复提交', 503)
            self._check(claims)
            if not self._slots.acquire(blocking=False):
                raise AppError('batch_busy', '当天批次队列已满，请稍后重新预检', 409)
            batch = {'_id': claims['batch_id'], 'account_id': account_id, 'revision': claims['revision'],
                     'target_date': claims['target_date'], 'expires_at': claims['expires_at'],
                     'claim_digest': digest, 'claim_owner': uuid.uuid4().hex,
                     'created_at': time.time(), 'updated_at': stamp(),
                     'state': 'running', 'message': '批次已确认，按时间顺序逐段检查并提交', 'segments': []}
            for index, segment in enumerate(claims['segments']):
                batch['segments'].append(segment | {'index': index,
                    'operation_id': uuid.uuid5(uuid.NAMESPACE_URL, batch['_id'] + ':' + str(index)).hex,
                    'seats': [asdict(self.accounts.catalog[key]) for key in segment['seat_ids']],
                    'status': 'pending', 'message': '等待本段检查', 'run_id': None, 'result': None})
            try:
                self.batches.insert_one(batch)
            except DuplicateKeyError:
                self._slots.release()
                old = self.batches.find_one({'_id': batch['_id'], 'account_id': account_id, 'claim_digest': digest})
                if old:
                    return self._view(old)
                raise AppError('account_busy', '此账号已有正在执行的当天批次，请先查看结果', 409) from None
            except Exception:
                self._slots.release()
                # The insert may have committed despite a lost acknowledgement.
                # Cleanup owns this exact claim even when its write is not yet visible.
                self._queue_cleanup(batch['_id'], batch)
                raise
            dispatch = Future()
            try:
                self.pool.submit(self._dispatch_work, batch['_id'], dispatch)
            except Exception:
                # submit() can enqueue work before thread startup raises. The gate
                # prevents that wrapper from sending or releasing the slot again.
                dispatch.set_result(False)
                self._slots.release()
                self._queue_cleanup(batch['_id'], batch)
                raise AppError('batch_interrupted', '后台未能确认批次启动，正在核对未提交状态；请查看原批次，勿重复提交', 503) from None
            dispatch.set_result(True)
            return self.get(account_id, batch['_id'])

    def _segment(self, batch_id, index, fields):
        values = {f'segments.{index}.{key}': value for key, value in fields.items()}
        values['updated_at'] = stamp()
        if not self.batches.update_one({'_id': batch_id, 'state': 'running'}, {'$set': values}).matched_count:
            raise AppError('batch_interrupted', '批次状态已变化，已停止后续提交', 409)

    def _stop(self, batch_id, message):
        batch = self.batches.find_one({'_id': batch_id, 'state': 'running'})
        if not batch:
            return
        for segment in batch['segments']:
            if segment['status'] == 'pending':
                segment.update(status='not_submitted', message='批次已停止，本时段未提交')
        self.batches.update_one({'_id': batch_id, 'state': 'running'}, {'$set': {
            'state': 'stopped', 'message': message, 'segments': batch['segments'], 'updated_at': stamp()}})

    def _recover_segment(self, batch, segment):
        if segment['status'] != 'running':
            return
        run = self.store.get_run(segment.get('run_id')) if segment.get('run_id') else None
        if run and run['status'] in ('success', 'failed', 'skipped', 'uncertain'):
            fields = {'status': run['status'], 'message': run.get('message', '请核对学校记录'), 'result': run.get('result')}
        elif run and run['status'] == 'submitting':
            fields = {'status': 'uncertain', 'message': '提交中断，请核对学校记录；不会自动重交'}
        elif run and run['status'] in ('queued', 'preparing'):
            operation_id = uuid.uuid5(uuid.NAMESPACE_URL, batch['_id'] + ':' + str(segment['index'])).hex
            spec = run.get('spec') or {}
            bound = (spec.get('kind') == 'manual' and spec.get('operation_id') == operation_id
                and spec.get('account_id') == batch['account_id'] and spec.get('revision') == batch['revision']
                and spec.get('target_date') == batch['target_date']
                and run['_id'] == f"{batch['account_id']}:{batch['target_date']}:manual:{operation_id}")
            unsent = run.get('attempted_seat_id') is None and not run.get('attempts') and not run.get('submitted_at')
            result = RunResult('skipped', '批次执行中断，本段未提交；不会自动重交', [])
            if bound and unsent and self.store.transition_run(run['_id'], ('queued', 'preparing'), 'skipped',
                    {'message': result.message, 'result': asdict(result)}, revision=batch['revision']):
                fields = {'status': 'not_submitted', 'message': result.message, 'result': asdict(result)}
            else:
                fields = {'status': 'uncertain', 'message': '手动运行认领或提交证据无法确认，请核对学校记录；不会自动重交'}
        else:
            fields = {'status': 'not_submitted', 'message': '执行中断，本时段未提交'}
        self._segment(batch['_id'], segment['index'], fields)

    def _recover(self):
        for batch in self.batches.find({'state': 'running'}):
            self._interrupt(batch['_id'], '服务重启中断，剩余时段未提交；请核对已处理结果，不会自动重交')

    def _interrupt(self, batch_id, message):
        batch = self.batches.find_one({'_id': batch_id, 'state': 'running'})
        if batch:
            for segment in batch['segments']:
                self._recover_segment(batch, segment)
            self._stop(batch_id, message)

    def _work(self, batch_id):
        try:
            self._execute(batch_id)
        except Exception:
            try:
                self._interrupt(batch_id, '批次执行中断，剩余时段未提交；请核对已处理结果，不会自动重交')
            except Exception:
                # Only terminalization is retried after a database outage; never dispatch again.
                self._queue_cleanup(batch_id)
        finally:
            self._slots.release()

    def _dispatch_work(self, batch_id, dispatch):
        if dispatch.result():
            self._work(batch_id)

    def _queue_cleanup(self, batch_id, claim=None):
        with self._cleanup_lock:
            self._cleanup_pending.setdefault(batch_id, claim)
            self._cleanup_needed.set()

    def _stop_unstarted_claim(self, claim):
        """Fence a failed dispatch without altering another owner or execution evidence."""
        fields = ('_id', 'account_id', 'claim_digest', 'claim_owner', 'revision', 'target_date')
        binding = {key: claim[key] for key in fields}
        current = self.batches.find_one({'_id': claim['_id']})
        if current and (any(current.get(key) != value for key, value in binding.items())
                or current.get('state') != 'running' or current.get('segments') != claim['segments']):
            return True
        message = '批次启动未完成，所有时段均未提交；不会自动重试，请重新预检'
        segments = [segment | {'status': 'not_submitted', 'message': '批次未启动，本时段未提交'}
                    for segment in claim['segments']]
        terminal = {'state': 'stopped', 'message': message, 'segments': segments, 'updated_at': stamp()}
        if current:
            # Include the unchanged pending segments to avoid overwriting later evidence.
            self.batches.update_one(binding | {'state': 'running', 'segments': claim['segments']},
                                    {'$set': terminal})
        else:
            # An absence read cannot prove the original insert will never commit.
            # A terminal document with the same ID fences that late write atomically.
            try:
                self.batches.insert_one(claim | terminal)
            except DuplicateKeyError:
                return False
        return True

    def _cleanup_loop(self):
        while not self._stopping.is_set():
            self._cleanup_needed.wait()
            if self._stopping.is_set():
                break
            with self._cleanup_lock:
                pending = tuple(self._cleanup_pending.items())
            for batch_id, claim in pending:
                if self._stopping.is_set():
                    break
                try:
                    if claim is not None:
                        if not self._stop_unstarted_claim(claim):
                            continue
                    else:
                        self._interrupt(batch_id, '批次执行中断，剩余时段未提交；请核对已处理结果，不会自动重交')
                except Exception:
                    continue
                with self._cleanup_lock:
                    self._cleanup_pending.pop(batch_id, None)
            with self._cleanup_lock:
                if not self._cleanup_pending:
                    self._cleanup_needed.clear()
            self._stopping.wait(.5)

    def _clear_failure(self, run_key):
        """Replay the shared seat policy; only known, exhausted rejections may continue."""
        run = self.store.get_run(run_key)
        return bool(run and (run.get('spec') or {}).get('kind') == 'manual' and clear_busy_failure(run))

    def _execute(self, batch_id):
        batch = self.batches.find_one({'_id': batch_id, 'state': 'running'})
        if not batch:
            return
        for segment in batch['segments']:
            try:
                self._check(batch, segment)
                lock = self.accounts.account_lock(batch['account_id'])
                if not lock.acquire(blocking=False):
                    raise AppError('account_busy', '账号正被其他操作使用，剩余时段未提交', 409)
                try:
                    row = self._check(batch, segment)
                    self._school(row, batch['target_date'], [segment])
                    self._check(batch, segment)
                    run = RunSpec(batch['account_id'], batch['revision'], batch['target_date'],
                        segment['begin'], segment['end'], tuple(segment['seat_ids']), self._now(),
                        kind='manual', operation_id=segment['operation_id'], expires_at=batch['expires_at'])
                    self._segment(batch_id, segment['index'], {'status': 'running', 'run_id': run.key, 'message': '本段正在执行'})
                    self.accounts.busy.add(batch['account_id'])
                    try:
                        result = self.reservations.execute(run, manual_guard=self._submission_clear)
                    finally:
                        self.accounts.busy.discard(batch['account_id'])
                finally:
                    lock.release()
            except AppError as exc:
                self._interrupt(batch_id, str(exc))
                return
            status = result.status if result.status in ('success', 'failed', 'uncertain', 'skipped') else 'uncertain'
            self._segment(batch_id, segment['index'], {'status': status, 'message': result.message, 'result': asdict(result)})
            if status == 'failed' and not self._clear_failure(run.key):
                self._stop(batch_id, '学校拒绝或失败原因不属于已确认的普通座位忙碌，剩余时段未提交；请核对本段结果')
                return
            if status not in ('success', 'failed'):
                self._stop(batch_id, '本段结果待核对或执行校验已失效，剩余时段未提交；不会自动重交')
                return
        self.batches.update_one({'_id': batch_id, 'state': 'running'}, {'$set': {
            'state': 'completed', 'message': '批次已完成，请查看各段预约结果；已成功的预约保持有效', 'updated_at': stamp()}})

    def close(self):
        with self._dispatch:
            self._stopping.set()
            self._cleanup_needed.set()
        # Queued work drains through the stop check; running work retains its receipt.
        self.pool.shutdown(wait=True, cancel_futures=False)
        self._cleanup_thread.join()
