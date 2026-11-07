"""Dedicated collections and conditional writes for a standalone local MongoDB."""
from dataclasses import asdict
import base64
from datetime import date
import json
import time
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from .models import AccountView, AppError, RunPage, STATES, stamp
from .timing_diagnostics import safe_timings


class LocalStore:
    def __init__(self, database):
        self.db = database
        self.accounts, self.runs = database.campus_accounts, database.campus_runs
        self.accounts.create_index('student_id', unique=True)
        self.runs.create_index([('created', -1), ('_id', -1)])

    @staticmethod
    def view(row):
        return AccountView(row['_id'], row['student_id'], row['alias'], row['revision'],
                           row['credential_revision'], row['verified_at'], row.get('plan'),
                           row.get('date_exceptions', {}), row.get('seat_preferences_revision', 0),
                           row.get('seat_preferences', {'favorites': [], 'groups': []}))

    @staticmethod
    def preference_view(row):
        preferences = row.get('seat_preferences', {'favorites': [], 'groups': []})
        return {'account_id': row['_id'], 'revision': row.get('seat_preferences_revision', 0),
                'favorites': preferences['favorites'], 'groups': preferences['groups']}

    def save_seat_preferences(self, account_id, preferences, expected_revision):
        query = {'_id': account_id, 'seat_preferences_revision': expected_revision}
        if expected_revision == 0:
            query = {'_id': account_id, '$or': [{'seat_preferences_revision': 0},
                                             {'seat_preferences_revision': {'$exists': False}}]}
        row = self.accounts.find_one_and_update(query,
            {'$set': {'seat_preferences': preferences}, '$inc': {'seat_preferences_revision': 1}},
            return_document=ReturnDocument.AFTER)
        if not row:
            raise AppError('preferences_conflict', '收藏与分组已在另一处修改，请重新读取后操作', 409)
        return self.preference_view(row)

    def get_account(self, account_id):
        row = self.accounts.find_one({'_id': account_id})
        if not row: raise AppError('not_found', '账号不存在', 404)
        return row

    def find_account_by_student(self, student_id): return self.accounts.find_one({'student_id': student_id})
    def list_accounts(self): return [self.view(x) for x in self.accounts.find().sort('student_id', 1)]

    def save_verified_account(self, account_id, identity, alias, envelope, expected_revision, *, credential_changed=True):
        values = {'student_id': identity.student_id, 'alias': alias or identity.display_name or identity.student_id,
                  'identity': asdict(identity), 'secret': envelope, 'verified_at': stamp(), 'login_error': None}
        if expected_revision is None:
            try: self.accounts.insert_one({'_id': account_id, **values, 'revision': 1, 'credential_revision': 1, 'plan': None})
            except DuplicateKeyError: raise AppError('conflict', '账号已存在，请切换到该账号并重新载入', 409) from None
        else:
            # Verification/metadata edits still advance the account CAS revision;
            # unchanged credentials must keep their existing node enrollment.
            increments = {'revision': 1}
            if credential_changed: increments['credential_revision'] = 1
            row = self.accounts.find_one_and_update({'_id': account_id, 'revision': expected_revision},
                {'$set': values, '$inc': increments}, return_document=ReturnDocument.AFTER)
            if not row: raise AppError('conflict', '配置已变化，请重新载入后操作', 409)
        return self.view(self.get_account(account_id))

    def save_plan(self, account_id, plan, expected_revision):
        row = self.accounts.find_one_and_update({'_id': account_id, 'revision': expected_revision},
            {'$set': {'plan': asdict(plan)}, '$inc': {'revision': 1}}, return_document=ReturnDocument.AFTER)
        if not row: raise AppError('conflict', '配置已变化，请重新载入后保存', 409)
        return self.view(row)

    def disable_plan(self, account_id, expected_revision):
        """Stopping access must work even if an old plan fails today's validation."""
        row = self.accounts.find_one_and_update({'_id':account_id,'revision':expected_revision},
            {'$set':{'plan.enabled':False},'$inc':{'revision':1}},return_document=ReturnDocument.AFTER)
        if not row:raise AppError('conflict','计划已变化，请刷新后停用',409)
        return self.view(row)

    def save_date_exceptions(self, account_id, exceptions, expected_revision):
        row = self.accounts.find_one_and_update({'_id': account_id, 'revision': expected_revision},
            {'$set': {'date_exceptions': exceptions}, '$inc': {'revision': 1}},
            return_document=ReturnDocument.AFTER)
        if not row:
            raise AppError('conflict', '配置已变化，请重新载入后调整日期', 409)
        return self.view(row)

    def save_snapshot(self, account_id, expected_revision, records, checked_at):
        self.accounts.update_one({'_id': account_id, 'revision': expected_revision}, {'$set': {
            'snapshot': [asdict(r) if hasattr(r, '__dataclass_fields__') else r for r in records],
            'snapshot_at': checked_at, 'query_error': None, 'login_error': None}})

    def query_failed(self, account_id, expected_revision, code):
        self.accounts.update_one({'_id': account_id, 'revision': expected_revision}, {'$set': {'query_error': code, 'query_error_at': stamp()}})

    def ensure_run(self, run):
        # Already prepared tasks need one read, not a failed insert plus update.
        existing = self.runs.find_one({'_id': run.key, 'spec': asdict(run)})
        if existing is not None:return existing
        try:
            self.runs.insert_one({'_id': run.key, 'spec': asdict(run), 'account_id': run.account_id,
                                 'target_date': run.target_date, 'status': 'queued', 'created': time.time(),
                                 'updated_at': stamp(), 'message': '等待放号时间', 'attempted_seat_id': None})
        except DuplicateKeyError: pass
        self.runs.update_one({'_id': run.key, 'spec': {'$ne': asdict(run)},
            'status': {'$in': ['queued','preparing','skipped']}, 'attempted_seat_id': None},
            {'$set': {'spec': asdict(run), 'status': 'queued', 'message': '计划已更新，等待执行', 'updated_at': stamp()},
             '$unset': {'scheduler_timings': ''}})
        return self.get_run(run.key)

    def save_scheduler_timings(self, run, phases):
        safe = safe_timings(phases)
        query = {'_id': run.key, 'spec': asdict(run)}
        if 'stop' in safe:query['status'] = {'$in':['failed','skipped']}
        return bool(self.runs.update_one(query,
            {'$set': {'scheduler_timings': safe}}).matched_count)

    def get_run(self, run_key): return self.runs.find_one({'_id': run_key})

    def transition_run(self, run_key, from_states, state, fields, *, revision=None):
        if state not in STATES: raise ValueError('Invalid run state')
        safe = {k: v for k, v in fields.items() if k in ('message', 'result', 'attempted_seat_id', 'submitted_at', 'reconcile_count', 'attempts', 'account_pacing')}
        query = {'_id': run_key, 'status': {'$in': list(from_states)}}
        if revision is not None: query['spec.revision'] = revision
        return bool(self.runs.update_one(query,
                    {'$set': safe | {'status': state, 'updated_at': stamp()}}).matched_count)

    def recover_interrupted(self):
        gate=getattr(self,'managed_gate',None)
        if gate is not None:
            for row in self.runs.find({'status':{'$in':['queued','preparing']}}):
                if gate.is_managed(row['account_id']):
                    self.runs.update_one({'_id':row['_id'],'status':row['status']},
                        {'$set':{'status':'uncertain','message':'双机管理账号保持原路径停用；恢复不重新排队'}})
        self.runs.update_many({'native_binding':{'$exists':True},'status':{'$in':['queued','preparing','submitting']}},
            {'$set':{'status':'uncertain','message':'原生任务执行中断，禁止切换发送方式；请查询学校记录核对'}})
        count = self.runs.update_many({'status': 'submitting'}, {'$set': {'status': 'uncertain', 'message': '上次提交中断，请查询学校记录核对'}}).modified_count
        self.runs.update_many({'status': 'preparing','native_binding':{'$exists':False}}, {'$set': {'status': 'queued', 'message': '后台恢复，等待重新准备'}})
        return count

    @staticmethod
    def run_view(row):
        return {key: row.get(key) for key in ('account_id', 'target_date', 'status', 'spec',
            'message', 'updated_at', 'result', 'attempts', 'scheduler_timings')} | {'id': row['_id']}

    def list_runs(self, account_id, target_date, limit=100, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100: raise AppError('invalid_limit', '每页条数须为 1–100')
        query = {}
        if account_id: query['account_id'] = account_id
        if target_date:
            try:
                if date.fromisoformat(target_date).isoformat() != target_date: raise ValueError()
            except (ValueError, TypeError): raise AppError('invalid_date', '日期须为 YYYY-MM-DD') from None
            query['target_date'] = target_date
        counts = {'total': self.runs.count_documents(query)}
        for item in self.runs.aggregate([{'$match': query}, {'$group': {'_id': '$status', 'n': {'$sum': 1}}}]): counts[item['_id']] = item['n']
        page_query = dict(query)
        if cursor:
            try:
                when, key = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if type(when) not in (float, int) or not isinstance(key, str): raise ValueError()
            except (ValueError, TypeError, UnicodeError): raise AppError('invalid_cursor', '分页位置无效') from None
            page_query['$or'] = [{'created': {'$lt': when}}, {'created': when, '_id': {'$lt': key}}]
        rows = list(self.runs.find(page_query).sort([('created', -1), ('_id', -1)]).limit(limit+1))
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last['created'], last['_id']]).encode()).decode()
        items = [self.run_view(row) for row in rows[:limit]]
        return RunPage(items, next_cursor, counts)
