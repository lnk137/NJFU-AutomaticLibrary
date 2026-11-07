"""Optional durable configuration sync; never submits a school reservation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
import requests
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash
from .models import AppError, ZONE, validate_plan, stamp
from .policy import school_policy
from .limits import PARALLEL_ACCOUNT_LIMIT
from .inbox_transport import configure_session,read_route

AUTH_REJECTIONS = frozenset({'login_failed', 'verification_required', 'login_incomplete', 'identity_mismatch'})
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024 - 65536


def _snapshot_size(value):
    # Match requests' default JSON encoding, including escaped non-ASCII text.
    return len(json.dumps(value, allow_nan=False).encode('utf-8'))


def _bounded_snapshot(packet):
    """Bound optional caches; never omit an account, authorization or saved plan."""
    if _snapshot_size(packet) <= SNAPSHOT_MAX_BYTES: return packet
    rows, histories = [], []
    for source in packet['accounts']:
        row = dict(source)
        history = dict(source.get('runs') or {})
        histories.append(history.get('items') or [])
        row['runs'] = history | {'items': []}
        rows.append(row)
    result = packet | {'accounts': rows}
    sizes = [_snapshot_size(row) for row in rows]
    size = _snapshot_size(packet | {'accounts': []}) + sum(sizes) + 2 * max(0, len(rows)-1)
    # A receipt cache is all-or-nothing: a truncated list must never look like
    # a successful school query with missing reservations.
    for index in sorted(range(len(rows)), key=lambda i: _snapshot_size(rows[i].get('records')), reverse=True):
        if size <= SNAPSHOT_MAX_BYTES: break
        old = rows[index].get('records') or {}
        rows[index]['records'] = {**old, 'records': [], 'checked_at': None,
                                  'error': 'cache_capacity', 'cache_truncated': True}
        new_size = _snapshot_size(rows[index])
        size += new_size-sizes[index]; sizes[index] = new_size
    if size > SNAPSHOT_MAX_BYTES:
        raise AppError('inbox_snapshot_capacity', '账号配置超出同步容量，请管理员核对；未发布部分账号快照', 503)
    # Fairly retain a contiguous newest prefix for each account. Persisted
    # counts continue to expose truncation through the existing cache contract.
    blocked = set()
    for rank in range(max((len(items) for items in histories), default=0)):
        for index, items in enumerate(histories):
            if index in blocked or rank >= len(items): continue
            cached = rows[index]['runs']['items']
            cost = _snapshot_size(items[rank]) + (2 if cached else 0)
            if size+cost > SNAPSHOT_MAX_BYTES:
                blocked.add(index); continue
            cached.append(items[rank]); size += cost
    if _snapshot_size(result) > SNAPSHOT_MAX_BYTES:
        raise AppError('inbox_snapshot_capacity', '同步缓存超出容量，请管理员核对', 503)
    return result


class SyncWindow(Exception):
    """No new sync I/O is started during booking preparation."""


def _near_release(now, before, triggers=()):
    day = datetime.fromtimestamp(now, ZONE)
    for trigger in set(('07:00:00', *triggers)):
        try:
            due = datetime.fromisoformat(day.date().isoformat()+'T'+trigger).replace(tzinfo=ZONE).timestamp()
            if -before <= now-due <= 40: return True
        except (ValueError, TypeError): continue
    return False


def apply_quiet(now, triggers=()): return _near_release(now, 20, triggers)
def io_quiet(now, triggers=()): return _near_release(now, 15, triggers)


def _digest(job):
    return hashlib.sha256(json.dumps({k:job[k] for k in ('student_id','version','generation','expected_revision','plan')},
                         sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class InboxEngine:
    def __init__(self, accounts, store, auth, *, clock=time.time):
        self.accounts, self.store, self.auth, self.clock = accounts, store, auth, clock
        self.verifiers = store.db.campus_inbox_auth
        self.receipts = store.db.campus_inbox_receipts
        self.state = store.db.campus_inbox_state
        self.triggers = ()

    def note_auth(self, account_id, code):
        if code is not None and code not in AUTH_REJECTIONS: return
        old = self.verifiers.find_one({'_id': account_id})
        if code is not None and old and old.get('blocked') == code: return
        values={'blocked':code,'observed_at':stamp()}
        if code is None:values['success_id']=uuid.uuid4().hex
        update={'$set':values}
        if not old or old.get('blocked') != code:update['$inc']={'epoch':1}
        self.verifiers.update_one({'_id':account_id},update,upsert=True)

    @staticmethod
    def generation(row, access, verifier):
        return f"{row['_id']}:{row['credential_revision']}:{access['revision']}:{verifier.get('epoch', 1)}"

    def verifier(self, row, access):
        if (not access or not access.get('enabled') or not row.get('secret') or not row.get('verified_at')
                or (row.get('identity') or {}).get('student_id') != row['student_id']): return None
        cached = self.verifiers.find_one({'_id': row['_id']})
        if not cached or cached.get('credential_revision') != row['credential_revision']:
            lock = self.accounts.account_lock(row['_id'])
            if not lock.acquire(blocking=False): return None
            try:
                current = self.store.get_account(row['_id'])
                if current['credential_revision'] != row['credential_revision']: return None
                password = self.accounts.vault.unseal(row['_id'], row['secret'])
                verifier = generate_password_hash(password, method='scrypt:32768:8:1'); password = None
                known_rejection=None
                if not cached:
                    for field in ('login_error','query_error'):
                        if row.get(field) not in AUTH_REJECTIONS:continue
                        try:newer=datetime.fromisoformat(row[field+'_at'])>=datetime.fromisoformat(row['verified_at'])
                        except (KeyError,TypeError,ValueError):newer=True
                        if newer:known_rejection=row[field]
                self.verifiers.update_one({'_id': row['_id']}, {'$set': {'password_hash': verifier,
                    'credential_revision': row['credential_revision'],'success_id':(cached or {}).get('success_id') or uuid.uuid4().hex}, '$setOnInsert': {'epoch': 1, 'blocked': known_rejection}}, upsert=True)
                cached = self.verifiers.find_one({'_id': row['_id']})
            finally: lock.release()
        return {'password_hash': cached['password_hash'], 'generation': self.generation(row, access, cached),
                'blocked': cached.get('blocked'),'success_id':cached['success_id']}

    def snapshot(self, runtime, *, stop_for_release=False):
        access = {r['_id']: r for r in self.auth.access.find()}
        rows = []; triggers = []
        for row in self.store.accounts.find().sort('student_id', 1):
            if stop_for_release and io_quiet(self.clock(), self.triggers): raise SyncWindow()
            allowed = access.get(row['student_id'])
            plan = row.get('plan')
            if isinstance(plan,dict) and plan.get('enabled'): triggers.append(plan.get('trigger'))
            item = {'student_id':row['student_id'],'enabled':bool(allowed and allowed.get('enabled')),
                    'account':None,'auth':None,'sync_error':None,'applied_version':0,
                    'records':{'records':[],'checked_at':None},'runs':{'items':[],'counts':{},'next_cursor':None}}
            try:
                item['account'] = asdict(self.store.view(row))
                gate = self.accounts.managed_gate
                managed = gate is not None
                if gate is not None:
                    try: managed = gate.is_managed(row['_id']) is not False
                    except Exception: managed = True
                item['account']['capabilities'] = {
                    'execution_mode': 'dual_node' if managed else 'local',
                    'date_override_segments': not managed,
                    # This cache has no live node-enrollment readback.
                    'credential_sync_state': 'unknown' if managed else 'not_required'}
                item['auth'] = self.verifier(row,allowed)
                item['records'] = self.accounts.snapshot(row['_id'])
                item['runs'] = asdict(self.store.list_runs(row['_id'],None,100,None))
                item['applied_version'] = (row.get('last_inbox') or {}).get('version',0)
                json.dumps(item,allow_nan=False)
            except Exception:
                item.update(auth=None,sync_error='账号快照读取失败，请核对该账号；其他账号继续同步')
                item['records']={'records':[],'checked_at':None}
                item['runs']={'items':[],'counts':{},'next_cursor':None}
                try:json.dumps(item['account'],allow_nan=False)
                except (TypeError,ValueError):item['account']=None
            rows.append(item)
        self.triggers = tuple(t for t in triggers if t)
        if stop_for_release and io_quiet(self.clock(), self.triggers): raise SyncWindow()
        sequence = self.state.find_one_and_update({'_id': 'sequence'}, {'$inc': {'value': 1}}, upsert=True,
                                                 return_document=ReturnDocument.AFTER)['value']
        safe_runtime = {k:runtime.get(k) for k in ('allow_live','scheduler_running','clock_ok','ready_to_submit',
                                                'last_error','scheduler_warning','next_due','capacity','execution_message')}
        return _bounded_snapshot({'generation': sequence, 'accounts': rows, 'policy': school_policy(), 'runtime': safe_runtime,
                                  'seats': [asdict(s) for s in self.accounts.catalog.values()]})

    def _remember(self, request_id, digest, result):
        self.receipts.update_one({'_id': request_id}, {'$setOnInsert': {'digest': digest, 'result': result,
                                  'at': stamp()}}, upsert=True)
        return result

    def apply(self, job):
        """Authenticate + CAS one account; marker and plan commit in one Mongo write."""
        request_id = job.get('request_id'); digest = _digest(job)
        old = self.receipts.find_one({'_id': request_id})
        if old:
            if old['digest'] != digest: return {'state':'failed','code':'inbox_replay','message':'提交编号与原配置不一致'}
            return old['result']
        if apply_quiet(self.clock(), self.triggers):
            return {'state':'deferred','code':'release_window','message':'已进入放号准备阶段，配置将在本轮结束后同步，不补交本轮预约'}
        student_lock = self.accounts.account_lock('student:'+job['student_id'])
        if not student_lock.acquire(blocking=False): return {'state':'deferred','code':'account_busy','message':'此账号正在使用，稍后同步'}
        lock = None
        row = None
        try:
            row = self.store.find_account_by_student(job['student_id'])
            if not row: raise AppError('offline_unavailable','此账号未在本机保存')
            lock = self.accounts.account_lock(row['_id'])
            if not lock.acquire(blocking=False):
                lock = None
                return {'state':'deferred','code':'account_busy','message':'此账号正在使用，稍后同步'}
            row = self.store.get_account(row['_id'])
            marker = row.get('last_inbox') or {}
            if marker.get('request_id') == request_id:
                if marker.get('digest') != digest: raise AppError('inbox_replay','提交编号与原配置不一致')
                return self._remember(request_id, digest, {'state':'applied','revision':marker['revision'],
                                                          'message':'配置已在执行电脑落库生效'})
            if job.get('reconcile_only'):
                raise AppError(job.get('reconcile_code') or 'auth_changed','未找到该旧配置的本机落库凭据，未执行或覆盖现有计划')
            if row['_id'] in self.accounts.busy: return {'state':'deferred','code':'account_busy','message':'此账号正在准备预约，稍后同步'}
            if job['expires_at'] <= self.clock() or job['created_at'] > self.clock()+5:
                raise AppError('inbox_expired','配置已过期或时钟异常，请重新提交')
            access = self.auth.access.find_one({'_id': row['student_id'], 'enabled': True})
            verifier = self.verifiers.find_one({'_id': row['_id']})
            if not access or not verifier or verifier.get('blocked') or self.generation(row, access, verifier) != job['generation']:
                raise AppError('auth_changed','授权或学校认证状态已变化，请重新登录')
            if (type(job['expected_revision']) is not int or row['revision'] != job['expected_revision']
                    or type(job['version']) is not int or job['version'] <= marker.get('version', 0)):
                raise AppError('conflict','本机配置已有更新，未覆盖；请重新载入后提交',409)
            plan = self.accounts._validated_plan_update(row, job['plan'])
            self.accounts.client(row['_id'], force=True)
            if not self.auth.is_allowed(row['student_id']): raise AppError('access_denied','该账号的使用权限已停用')
            if apply_quiet(self.clock(), self.triggers) or self.clock() >= job['expires_at']:
                return {'state':'deferred','code':'release_window','message':'临近放号或配置过期，未改动现有计划'}
            # Authentication can cross a preparation boundary. Recheck the same
            # online admission without splitting the plan/receipt-marker CAS.
            plan = self.accounts._validated_plan_update(row, job['plan'])
            revision = row['revision'] + 1
            committed = self.store.accounts.find_one_and_update({'_id':row['_id'],'revision':row['revision']},
                {'$set': {'plan':asdict(plan), 'last_inbox':{'request_id':request_id,'version':job['version'],
                    'digest':digest,'revision':revision,'at':stamp()}}, '$inc':{'revision':1}}, return_document=ReturnDocument.AFTER)
            if not committed or committed['revision'] != revision:
                raise AppError('conflict','配置版本已变化，未覆盖本机计划',409)
            return self._remember(request_id,digest,{'state':'applied','revision':revision,'message':'配置已在执行电脑落库生效'})
        except AppError as exc:
            if exc.code in AUTH_REJECTIONS and row: self.note_auth(row['_id'], exc.code)
            retry = exc.code in ('network_error','school_http','account_busy','native_sync_pending','native_unavailable','native_recovery_busy') and job.get('attempt',1) < 4
            result = {'state':'deferred' if retry else 'failed','code':exc.code,'message':str(exc)}
            return result if retry else self._remember(request_id,digest,result)
        except PyMongoError:
            return {'state':'deferred','code':'database_unavailable','message':'本机数据库暂不可用，未确认生效，稍后重试'}
        except Exception:
            return {'state':'failed','code':'inbox_internal','message':'配置同步中断，请管理员核对该账号'}
        finally:
            if lock: lock.release()
            student_lock.release()


class InboxWorker:
    def __init__(self, settings, accounts, store, auth, scheduler, config):
        self.engine = InboxEngine(accounts, store, auth)
        self.settings, self.config, self.scheduler = settings, config, scheduler
        self.stop_event = threading.Event(); self.sync_requested=threading.Event(); self.thread = None
        self.pool = ThreadPoolExecutor(max_workers=PARALLEL_ACCOUNT_LIMIT, thread_name_prefix='njfu-inbox-account')
        self.ack_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='njfu-inbox-ack')
        self.snapshot_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='njfu-inbox-export')
        self.acks, self.ack_retry, self.snapshot_future = {}, {}, None
        self.jobs = {}; self.next_publish = 0; self.next_poll = 0
        self._lock = threading.Lock()
        self._status = {'configured': True, 'connected':False,'last_sync_at':None,'last_version_check_at':None,
                        'last_error':None,'pending':0,'failed':0,'accounts':[]}
        accounts.auth_observer = self.engine.note_auth

    def request(self, path, data, *, budget=3):
        session = requests.Session(); session.trust_env = False
        try:
            configure_session(session,self.config['origin'],self.config.get('connect_route'))
            response = session.post(self.config['origin']+'/_inbox/'+path, json=data,
                headers={'Authorization':'Bearer '+self.config['token']}, timeout=(budget/3,budget*2/3), allow_redirects=False)
            if response.status_code != 200:
                error = AppError('inbox_connection','公网收件同步暂不可用',503)
                error.inbox_http_status = response.status_code
                raise error
            return response.json()
        finally: session.close()

    def _record_failure(self, stage, error):
        stage = stage if stage in {'snapshot','poll','ack','export','cycle'} else 'cycle'
        if isinstance(error,requests.exceptions.Timeout): code = 'timeout'
        elif isinstance(error,requests.exceptions.SSLError): code = 'tls_error'
        elif isinstance(error,requests.exceptions.ConnectionError): code = 'connection_error'
        elif isinstance(error,requests.exceptions.JSONDecodeError): code = 'response_invalid'
        elif isinstance(error,requests.exceptions.RequestException): code = 'request_error'
        elif isinstance(error,AppError): code = 'http_status' if error.code == 'inbox_connection' else 'application_error'
        else: code = 'internal_error'
        diagnostic = {'stage':stage,'code':code,'at':time.time()}
        status = getattr(error,'inbox_http_status',None)
        if code == 'http_status' and type(status) is int and 100 <= status <= 599:
            diagnostic['http_status'] = status
        with self._lock: self._status['last_failure'] = diagnostic

    def remote(self, path, data, *, cycle_started=None):
        now = time.time()
        if io_quiet(now,self.engine.triggers): raise SyncWindow()
        budget = 3.
        day = datetime.fromtimestamp(now,ZONE).date().isoformat()
        for trigger in set(('07:00:00',*self.engine.triggers)):
            try:
                start = datetime.fromisoformat(day+'T'+trigger).replace(tzinfo=ZONE).timestamp()-15
                if start > now: budget = min(budget,start-now-.05)
            except (TypeError,ValueError):continue
        if cycle_started is not None:budget=min(budget,4-(now-cycle_started))
        if budget < .5:raise SyncWindow()
        return self.request(path,data,budget=budget)

    def status(self, student=None):
        with self._lock:
            result = {k:v for k,v in self._status.items() if k != 'local_accounts'}
            result['accounts'] = [dict(a) for a in result['accounts'] if student is None or a['student_id'] == student]
        if student is not None:
            result['pending'] = sum(a.get('state') in ('pending','leased') for a in result['accounts'])
            result['failed'] = sum(a.get('state') == 'failed' for a in result['accounts'])
        age=time.time()-(result.get('last_version_check_at') or 0)
        result['connected']=bool(result.get('connected') and (0<=age<15 or io_quiet(time.time(),self.engine.triggers) and 0<=age<90))
        return result

    def request_sync(self):self.sync_requested.set()

    def tick(self):
        self._sync_stage = 'cycle'
        if self.sync_requested.is_set():
            self.next_poll=self.next_publish=0;self.sync_requested.clear()
        now = time.time()
        if io_quiet(now, self.engine.triggers): return
        if not self.scheduler.status().get('clock_ok'):
            with self._lock: self._status['last_error']='本机时钟尚未恢复，暂缓配置同步'
            return
        for request_id, (job, future) in list(self.jobs.items()):
            if not future.done(): continue
            try: result = future.result()
            except Exception: result = {'state':'failed','code':'inbox_internal','message':'该账号同步异常，请管理员核对'}
            acknowledgement=self.acks.get(request_id)
            if acknowledgement and acknowledgement.done():
                del self.acks[request_id]
                try:
                    acknowledgement.result();del self.jobs[request_id];self.next_publish=0
                    continue
                except Exception as error:
                    self._record_failure('ack',error)
                    self.ack_retry[request_id]=time.time()+2
                    with self._lock:self._status['last_error']='部分收件确认待重试，未重复修改本机计划'
            if request_id not in self.acks and time.time() >= self.ack_retry.get(request_id,0):
                self.acks[request_id]=self.ack_pool.submit(self.remote,'ack',
                    {'request_id':request_id,'lease':job['lease'],'result':result})
        if self.snapshot_future and self.snapshot_future.done() and time.time()>=self.next_publish:
            try:packet=self.snapshot_future.result()
            except SyncWindow:self.snapshot_future=None;raise
            except Exception as error:
                self._record_failure('export',error)
                self.snapshot_future=None;self.next_publish=time.time()+3
                with self._lock:self._status['export_error']='账号快照发布待重试；现有计划继续运行'
                return
            try: self.remote('snapshot',packet,cycle_started=now)
            except SyncWindow: raise
            except Exception as error:
                self._record_failure('snapshot',error)
                self.next_publish=time.time()+3
                if getattr(error, 'inbox_http_status', None) == 413:
                    # The server definitely rejected this body. Re-export fresh
                    # state instead of permanently retrying an unsendable packet.
                    self.snapshot_future=None;self.next_publish=time.time()+10
                with self._lock:self._status['export_error']='账号快照发布待重试；现有计划继续运行'
                # Keep the same generation for idempotent retry, while allowing
                # independent polling within the existing cycle/quiet budget.
            else:
                self.snapshot_future=None;self.next_publish=time.time()+10
                local = {}
                for row in packet['accounts']:
                    auth = row.get('auth') or {}; account = row.get('account') or {}
                    try: validate_plan(account.get('plan'), self.engine.accounts.catalog); ready = bool((account.get('plan') or {}).get('enabled') and row['enabled'] and not auth.get('blocked') and not row.get('sync_error'))
                    except Exception: ready = False
                    local[row['student_id']] = {'student_id':row['student_id'],'account_id':account.get('id'),
                        'name':account.get('alias') or row['student_id'],'applied_version':row.get('applied_version',0),
                        'auth_error':auth.get('blocked'),'offline_eligible':bool(auth and not auth.get('blocked')),
                        'sync_error':row.get('sync_error'),'plan_ready':ready}
                with self._lock:
                    previous={a['student_id']:a for a in self._status['accounts']}
                    self._status.update(last_sync_at=time.time(), connected=True, last_error=None, export_error=None,
                        local_accounts=local,accounts=[{**previous.get(k,{}),**v} for k,v in local.items()])
        if self.snapshot_future is None and time.time() >= self.next_publish and not io_quiet(time.time(),self.engine.triggers):
            self.snapshot_future=self.snapshot_pool.submit(self.engine.snapshot,self.scheduler.status(),stop_for_release=True)
        if time.time() < self.next_poll: return
        self._sync_stage = 'poll'
        due = self.scheduler.status().get('next_due')
        response = self.remote('poll', {'quiet_until':due+40 if due and -40 < due-time.time() < 60 else None},cycle_started=now)
        summary = response['summary']
        with self._lock:
            local = self._status.get('local_accounts', {})
            latest = {a['student_id']:a for a in summary['accounts']}
            self._status.update({k:summary[k] for k in ('pending','failed','heartbeat_at','quiet_until')})
            self._status.update(connected=True,last_error=None,last_version_check_at=now,
                accounts=[{**value, **latest.get(student,{})} for student,value in local.items()])
        for job in response['jobs']:
            if job['request_id'] in self.jobs:
                self.jobs[job['request_id']][0]['lease'] = job['lease']
            else: self.jobs[job['request_id']] = (job, self.pool.submit(self.engine.apply, job))
        due = self.scheduler.status().get('next_due')
        self.next_poll = now + (.5 if due and 5 < due-now < 180 else 2)

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try: self.tick()
                except SyncWindow:pass
                except Exception as error:
                    self._record_failure(getattr(self,'_sync_stage','cycle'),error)
                    with self._lock: self._status.update(connected=False,last_error='公网收件同步暂不可用，现有本机计划继续运行')
                    self.next_poll = time.time()+3
                    self.next_publish = time.time()+3
                self.stop_event.wait(.5)
        self.thread = threading.Thread(target=loop,name='njfu-inbox-sync',daemon=True); self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread: self.thread.join()
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.ack_pool.shutdown(wait=True,cancel_futures=True)
        self.snapshot_pool.shutdown(wait=True,cancel_futures=True)


class UnavailableInbox:
    def status(self, student=None):
        return {'configured':True,'connected':False,'last_error':'收件配置无效；本机现有计划继续运行',
                'pending':None,'failed':None,'accounts':[]}
    def start(self):pass
    def close(self):pass


def configured_worker(settings, accounts, store, auth, scheduler):
    path = Path(settings.data_dir)/'inbox/config.json'
    if not path.exists(): return None
    def private(file):
        if file.is_symlink() or file.stat().st_uid != os.getuid() or file.stat().st_mode & 0o077:
            raise AppError('unsafe_inbox_config','收件配置必须为当前用户的0600文件',503)
        return file.read_text()
    try:
        config = json.loads(private(path))
        if set(config) != {'origin','token_file'} or config['origin'] != 'https://njfuautolib.duckdns.org:45581': raise ValueError()
        config['token'] = private(Path(config.pop('token_file'))).strip()
        if len(config['token']) != 64 or any(c not in '0123456789abcdef' for c in config['token']): raise ValueError()
        config['connect_route']=read_route(settings.data_dir,config['origin'])
    except (ValueError, KeyError, OSError,AppError): return UnavailableInbox()
    return InboxWorker(settings,accounts,store,auth,scheduler,config)
