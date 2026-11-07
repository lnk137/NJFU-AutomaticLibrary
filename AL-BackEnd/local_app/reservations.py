"""Durable bounded booking attempts with school-record confirmation."""
from dataclasses import asdict
from datetime import date, datetime
import math
import time
from .models import AppError, RunResult, RunSpec, stamp, ZONE
from .policy import validate_window
from .recovery_policy import BUSY_RETRY_RESPONSE_COOLDOWN, busy_retry_spacing, candidate_attempts, remaining_candidate_attempts
from .elapsed_clock import elapsed_time
from .manual_windows import record_windows, require_free_window
from .dual_node.fence import require_legacy,legacy_allowed


def records_match(run, attempted_seat_id, records, identity, *, active_statuses=frozenset()):
    return any(r.student_id == identity.student_id and r.seat_id == attempted_seat_id
        and r.begin == f'{run.target_date} {run.begin}:00' and r.end == f'{run.target_date} {run.end}:00'
        and type(r.status) is int and r.status in active_statuses for r in records)


def matching_attempt(run, row, records, identity, active_statuses):
    seats = [row.get('attempted_seat_id')] + [a.get('seat_id') for a in row.get('attempts', [])]
    return next((seat for seat in seats if seat in run.seat_ids
                 and records_match(run, seat, records, identity, active_statuses=active_statuses)), None)


class ReservationService:
    def __init__(self, accounts, store, allow_live, *, wall_clock=None, monotonic=None, wait=None):
        self.accounts, self.store, self.allow_live = accounts, store, allow_live
        self.guard = lambda: True
        self.wall_clock = wall_clock or (lambda: time.time())
        self.monotonic = monotonic or elapsed_time
        self.wait = wait or time.sleep
        # Access is serialized by AccountService.account_lock, including the
        # native scheduler's Python fallback and same-day batch executions.
        self._account_not_before = {}

    def _finish(self, run_key, result, revision=None):
        self.store.transition_run(run_key, ('queued','preparing','submitting','uncertain'), result.status,
            {'message': result.message, 'result': asdict(result)}, revision=revision)
        return result

    def _save_target_snapshot(self, run, records):
        account = self.store.get_account(run.account_id)
        # A target-day query must not erase cached receipts for other dates.
        other_days = [r for r in account.get('snapshot', [])
                      if not r.get('begin', '').startswith(run.target_date+' ')]
        self.store.save_snapshot(run.account_id, run.revision,
                                 other_days + [asdict(r) for r in records], stamp())

    def _busy_retry_window(self, response, attempt_count):
        """Share both retry floors with confirmed form imports, taking the later deadline."""
        received = self.monotonic()
        spacing = busy_retry_spacing(attempt_count)
        duration = (getattr(response, 'timings', None) or {}).get('request_ms')
        # Custom transports without trustworthy duration wait the full floor.
        duration = duration if type(duration) in (int,float) and math.isfinite(duration) and duration >= 0 else 0
        return received + max(BUSY_RETRY_RESPONSE_COOLDOWN, spacing-duration/1000), {
            'state':'waiting','pacing_policy':'dual_interval_v1',
            'spacing_ms':round(spacing*1000,3),
            'response_cooldown_ms':round(BUSY_RETRY_RESPONSE_COOLDOWN*1000,3),
            'previous_request_ms':duration}

    def _wait_account(self, run, allowed, *, checked=False):
        """Carry the last response's pacing across segments, without replay rights."""
        not_before = self._account_not_before.get(run.account_id)
        if not_before is None or self.monotonic() >= not_before:
            # Only the first attempt has no intervening persistence after its
            # checked snapshot. The HTTP boundary still checks current state.
            return True if checked else allowed()
        started = self.monotonic()
        check = {'state': 'waiting', 'pacing_policy': 'account_interval_v1',
                 'spacing_ms': round(busy_retry_spacing(1)*1000, 3),
                 'response_cooldown_ms': round(BUSY_RETRY_RESPONSE_COOLDOWN*1000, 3)}
        if not self.store.transition_run(run.key, ('submitting',), 'submitting',
                {'account_pacing': check}, revision=run.revision):
            raise RuntimeError('Account pacing state unavailable')
        while True:
            # The final check includes permission, pause, revision, identity and
            # both the original submission deadline and manual preview expiry.
            if not allowed():
                check['state'] = 'stopped'
                break
            remaining = not_before-self.monotonic()
            if remaining <= 0:
                check['state'] = 'ready'
                break
            self.wait(min(remaining, .05))
        check['wait_ms'] = round((self.monotonic()-started)*1000, 3)
        if not self.store.transition_run(run.key, ('submitting',), 'submitting',
                {'account_pacing': check}, revision=run.revision):
            raise RuntimeError('Account pacing state unavailable')
        # No blocking persistence after this final execution check.
        return check['state'] == 'ready' and allowed()

    def _wait_retry(self, attempts, not_before, allowed):
        """Wait only the residual spacing, then obtain a fresh self-query."""
        check = attempts[-1]['retry_check']
        started, waited = self.monotonic(), 0.0
        while True:
            if not allowed():
                check['state'] = 'stopped'
                break
            remaining = not_before-self.monotonic()
            if remaining <= 0:
                check['state'] = 'ready'
                break
            before = self.monotonic()
            self.wait(min(remaining, .05))
            waited += self.monotonic()-before
        check.update(wait_ms=round(waited*1000, 3),
                     pacing_elapsed_ms=round((self.monotonic()-started)*1000, 3))
        return check['state'] == 'ready'

    def _retry_readback(self, run, client, attempts):
        """Manual retries may coexist with verified, non-overlapping active records."""
        check = dict(attempts[-1].get('retry_check') or {})
        check.update(checked_at=stamp(), state='error')
        records, matched, failed, manual_clear = [], None, False, False
        try:
            day = date.fromisoformat(run.target_date)
            query_started = time.perf_counter()
            try: records = client.query(day, day)
            finally: check['query_ms'] = round((time.perf_counter()-query_started)*1000, 3)
            if not isinstance(records, list): raise ValueError('Incomplete school query')
            matched = matching_attempt(run, {'attempts': attempts}, records, client.identity,
                                       getattr(client, 'active_statuses', frozenset()))
            if records and not matched and (run.kind == 'manual' or run.operation_id):
                try:
                    windows = record_windows(records, client.identity.student_id, getattr(client, 'active_statuses', frozenset()))
                    require_free_window(windows, run.target_date, run.begin, run.end)
                    if run.kind == 'scheduled':
                        # Only independently verified siblings can coexist during
                        # automatic fallback. Unknown school records still stop it.
                        siblings = list(self.store.runs.find({'account_id': run.account_id,
                            'target_date': run.target_date, 'spec.revision': run.revision,
                            'spec.kind': 'scheduled', 'status': 'success'}))
                        if any(not any(records_match(RunSpec(**sibling['spec']),
                                sibling.get('attempted_seat_id'), [record], client.identity,
                                active_statuses=getattr(client, 'active_statuses', frozenset()))
                                for sibling in siblings) for record in records):
                            raise AppError('school_records_unverified', '已有记录无法绑定已核实的同日时段')
                    manual_clear = True
                except AppError:
                    pass
            if records:self._save_target_snapshot(run, records)
            check.update(state='matched' if matched else 'clear' if manual_clear else 'blocked' if records else 'empty',
                         record_count=len(records))
        except Exception:
            failed = True
        attempts[-1]['retry_check'] = check
        if not self.store.transition_run(run.key, ('submitting',), 'submitting',
                                         {'attempts': attempts}, revision=run.revision):
            return self._finish(run.key, RunResult('uncertain', '重试前查询结果未能保存，已停止重交，请核对学校记录', []), run.revision)
        if failed:
            return self._finish(run.key, RunResult('uncertain', '忙碌后学校记录查询未完成，已停止重交，请核对学校记录', []), run.revision)
        if matched:
            if not self.store.transition_run(run.key, ('submitting',), 'submitting',
                                             {'attempted_seat_id': matched}, revision=run.revision):
                return RunResult('uncertain', '学校回执未取得有效写入状态，已停止重交', records)
            return self._finish(run.key, RunResult('success', '学校有效预约记录已核实（重试前查询）', records, stamp()), run.revision)
        if records and not manual_clear:
            return self._finish(run.key, RunResult('uncertain', '查询发现已有预约或无法确认的记录，已停止重交，请核对学校记录', records), run.revision)
        return None

    def execute(self, run, *, manual_guard=None):
        require_legacy(self.accounts,run)
        if not self.allow_live: return RunResult('skipped','真实预约总开关未开启',[])
        with self.accounts.account_lock(run.account_id):
            manual = run.kind == 'manual'
            if run.kind not in ('scheduled', 'manual') or (manual and not run.operation_id):
                raise AppError('invalid_run', '预约任务类型无效')
            row = self.store.get_account(run.account_id)
            if row['revision'] != run.revision or (not manual and not (row.get('plan') or {}).get('enabled')):
                return self._finish(run.key,RunResult('skipped','计划已更新或关闭',[]),run.revision)
            existing = self.store.ensure_run(run)
            if existing.get('native_binding'):
                return RunResult(existing['status'] if existing['status'] not in {'queued','preparing'} else 'uncertain',
                    '原生任务已有持久资格，禁止切换发送方式；请查询学校记录',[])
            if not self.accounts.is_allowed(row['student_id']):
                return self._finish(run.key,RunResult('skipped','此学号的预约权限已停用',[]),run.revision)
            if existing['status'] not in ('queued','preparing'):
                return RunResult(existing['status'],existing.get('message','任务已处理'),[])
            if self.wall_clock() < run.due_at: return RunResult('queued','尚未到提交时刻',[])
            try:
                if manual:
                    now = datetime.fromtimestamp(self.wall_clock(), ZONE)
                    if run.target_date != now.date().isoformat():
                        raise ValueError('当天预约日期已变化，请重新预检')
                    start = datetime.fromisoformat(f'{run.target_date}T{run.begin}').replace(tzinfo=ZONE).timestamp()
                    if start <= self.wall_clock():
                        raise ValueError('当天预约开始时间已过，请重新预检')
                elif row['plan'].get('day_offset')!=1:
                    raise ValueError('学校常规规则仅支持提前一天预约，请将原计划调整为次日')
                validate_window(run.begin,run.end,date.fromisoformat(run.target_date).isoweekday())
            except ValueError as exc:
                return self._finish(run.key,RunResult('failed',str(exc),[]),run.revision)
            deadline = min(run.due_at + 30, run.expires_at) if run.expires_at is not None else run.due_at + 30
            if manual:
                deadline = min(deadline, start)
            if self.wall_clock() > deadline or not self.guard():
                return self._finish(run.key,RunResult('skipped','已错过放号超过 30 秒，不补交',[]))
            try: client = self.accounts.client(run.account_id, _checked_account=row)
            except AppError as exc: return self._finish(run.key,RunResult('failed',str(exc),[]))
            if self.wall_clock() > deadline or not self.guard():
                return self._finish(run.key,RunResult('skipped','登录完成时已过期或时钟校验失效，不补交',[]),run.revision)
            def allowed():
                if not legacy_allowed(self.accounts,run):return False
                current = self.store.get_account(run.account_id)
                permitted = self.accounts.is_allowed(current['student_id'])
                manual_permitted = not manual or manual_guard is None or manual_guard()
                # No blocking IO after the final time/live/clock checks.
                now = self.wall_clock()
                return (self.allow_live and self.guard() and run.due_at <= now <= deadline
                        and (not manual or now < start)
                        and current['revision'] == run.revision
                        and (manual or (current.get('plan') or {}).get('enabled'))
                        and permitted
                        and manual_permitted
                        and client.identity.student_id == row['student_id'])
            client.allow_live = self.allow_live
            client.submission_guard = allowed
            candidates = candidate_attempts(run.seat_ids)
            if not candidates:
                return self._finish(run.key,RunResult('failed','没有可预约的座位',[]),run.revision)
            # Claim and first attempted seat are one durable write before any POST.
            if not self.guard() or not self.store.transition_run(run.key,('queued','preparing'),'submitting',
                    {'submitted_at':self.wall_clock(),'reconcile_count':0,'attempted_seat_id':candidates[0]},revision=run.revision):
                return RunResult('uncertain','任务已开始，未重复提交',[])
            try:
                attempts = []
                failure_message = '学校未接受所选座位预约，请核对占用或学校规则'
                # Try the configured priority order, then at most one more round.
                while candidates:
                    seat_id = candidates[0]
                    if not allowed():
                        return self._finish(run.key,RunResult('skipped','执行校验已失效，停止后续座位尝试',[]),run.revision)
                    if attempts:
                        if not self._wait_retry(attempts, retry_not_before, allowed):
                            if not self.store.transition_run(run.key, ('submitting',), 'submitting',
                                    {'attempts': attempts}, revision=run.revision):
                                return RunResult('uncertain','等待状态未能保存，已停止重交',[])
                            return self._finish(run.key,RunResult('skipped','重试等待时执行校验失效，已停止后续尝试',[]),run.revision)
                        readback = self._retry_readback(run, client, attempts)
                        if readback is not None: return readback
                        if not allowed():
                            return self._finish(run.key,RunResult('skipped','查询后执行校验已失效，停止后续座位尝试',[]),run.revision)
                    if attempts and not self.store.transition_run(run.key,('submitting',),'submitting',{'attempted_seat_id':seat_id},revision=run.revision):
                        return RunResult('uncertain','尝试记录未取得有效状态，未继续提交',[])
                    if not self._wait_account(run, allowed, checked=not attempts):
                        return self._finish(run.key,RunResult('skipped',
                            '账号提交冷却时执行校验失效，停止后续尝试',[]),run.revision)
                    response = None
                    try:
                        response = client.submit(run,seat_id)
                    finally:
                        # A transport exception can mean a sent request with an
                        # unknown reply. Reserve the conservative full interval;
                        # the existing uncertainty handling still forbids replay.
                        not_before, _ = self._busy_retry_window(response, 1)
                        self._account_not_before[run.account_id] = not_before
                    attempt = asdict(response) | {'seat_id': seat_id}
                    following = remaining_candidate_attempts(run.seat_ids, attempts+[attempt]) if response.retryable else ()
                    retry_pending = bool(following)
                    if retry_pending:
                        retry_not_before,retry_check = self._busy_retry_window(response,len(attempts)+1)
                    attempts.append(attempt)
                    if retry_pending:
                        attempts[-1]['retry_check'] = retry_check
                    if not self.store.transition_run(run.key,('submitting',),'submitting',{'attempts':attempts},revision=run.revision):
                        return RunResult('uncertain','学校回复未取得有效写入状态，已停止后续提交',[])
                    if response.outcome == 'accepted': return self.reconcile(run.key)
                    if response.outcome == 'uncertain': return self._finish(run.key,RunResult('uncertain','提交结果待确认，已停止后续尝试',[]))
                    failure_message = (f'学校拒绝（code={response.code}）：{response.message}'
                                       if response.code is not None else response.message or failure_message)
                    if not retry_pending: break
                    candidates = following
                if attempts and attempts[-1]['retryable']:
                    failure_message += f'；有限尝试已结束（共{len(attempts)}次），未继续提交'
                return self._finish(run.key,RunResult('failed',failure_message,[]))
            except Exception:
                return self._finish(run.key,RunResult('uncertain','提交中断，禁止自动重交；请查询学校记录',[]))

    def reconcile(self, run_key):
        row = self.store.get_run(run_key)
        if not row: return RunResult('uncertain','执行记录不存在',[])
        from .dual_node.receipt_view import is_dual_row
        if is_dual_row(row): return self._reconcile_dual(row)
        if row['status'] not in ('submitting','uncertain'): return RunResult(row['status'],row.get('message',''),[])
        run = RunSpec(**row['spec'])
        with self.accounts.account_lock(run.account_id):
            try:
                client = self.accounts.client(run.account_id)
                records = client.query(date.fromisoformat(run.target_date),date.fromisoformat(run.target_date))
                self._save_target_snapshot(run, records)
                matched = matching_attempt(run, row, records, client.identity,
                                           getattr(client, 'active_statuses', frozenset()))
                if matched and not self.store.transition_run(run.key, ('submitting','uncertain'), row['status'],
                        {'attempted_seat_id': matched}, revision=run.revision):
                    return RunResult('uncertain','学校回执未取得有效写入状态，请核对学校记录',records)
                result = RunResult('success' if matched else 'uncertain',
                    '学校有效预约记录已核实' if matched else '尚未核实有效预约记录，请稍后查询',records,stamp() if matched else None)
            except Exception: result = RunResult('uncertain','提交结果待确认，学校查询暂未完成',[])
            return self._finish(run_key,result)

    def _confirm_dual(self, row, account, records, identity):
        from .dual_node.receipt_view import receipt_view, reconciliation_evidence, reconciliation_basis
        view = receipt_view(account, row)
        if row.get('status') == 'success' and view is not None:
            return RunResult('success', row.get('message', '学校有效预约已核实'), [], stamp())
        pending = RunResult('uncertain', '尚未唯一核实本计划的有效学校回执；原双机结果及授权保持不变', records if type(records) is list else [])
        if (identity.student_id != account.get('student_id') or type(records) is not list
                or reconciliation_basis(account, row) is None): return pending
        matched = []
        for record in records:
            try:
                evidence = reconciliation_evidence(account, row, asdict(record), self.wall_clock())
                matched.append(evidence)
            except (TypeError, ValueError, KeyError):
                continue
        if len(matched) != 1: return pending
        evidence = matched[0]
        message = '学校有效预约已由本机后续查询核实；原双机授权及节点结果保留'
        # CAS every original proof field so concurrent mirror updates cannot be
        # replaced by evidence derived from a different binding or result.
        query = {'_id': row['_id'], 'status': 'uncertain', 'dual_reconciliation': {'$exists': False}}
        for field in ('spec', 'dual_binding', 'dual_task', 'dual_progress', 'dual_result', 'dual_mirror', 'result'):
            query[field] = row.get(field)
        changed = self.store.runs.update_one(query, {'$set': {'status': 'success', 'message': message,
            'dual_reconciliation': evidence, 'updated_at': stamp()}})
        if not changed.acknowledged or changed.matched_count != 1: return pending
        checked = self.store.get_run(row['_id'])
        if receipt_view(account, checked) is None: return pending
        return RunResult('success', message, records, stamp())

    def _reconcile_dual(self, row):
        with self.accounts.account_lock(row['account_id']):
            try:
                row = self.store.get_run(row['_id'])
                account = self.store.get_account(row['account_id'])
                client = self.accounts.client(row['account_id'])
                day = date.fromisoformat(row['spec']['target_date'])
                records = client.query(day, day)
                return self._confirm_dual(row, account, records, client.identity)
            except Exception:
                return RunResult('uncertain', '学校查询未能完成，原双机结果及授权保持不变', [])

    def reconcile_account(self, account_id, records, identity, active_statuses):
        """Use a successful manual self-query to confirm delayed records without POST."""
        for row in self.store.runs.find({'account_id':account_id,'status':'uncertain'}):
            from .dual_node.receipt_view import is_dual_row
            if is_dual_row(row):
                self._confirm_dual(row, self.store.get_account(account_id), records, identity)
                continue
            run=RunSpec(**row['spec'])
            matched = matching_attempt(run, row, records, identity, active_statuses)
            if matched and self.store.transition_run(run.key, ('uncertain',), 'uncertain',
                    {'attempted_seat_id': matched}, revision=run.revision):
                self._finish(run.key,RunResult('success','学校有效预约记录已核实',records,stamp()),run.revision)
