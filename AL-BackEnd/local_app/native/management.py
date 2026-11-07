"""Bind authoritative Mongo management writes to acknowledged C++ gates."""
from contextlib import nullcontext
import json
import threading

from .outbox import DurableOutbox
from .platform_policy import validate_native_policy


class AuthorityCollection:
    SAFE_ACCOUNT = frozenset({'snapshot','snapshot_at','query_error','query_error_at','login_error','verified_at',
                              'seat_preferences','seat_preferences_revision'})
    SAFE_ACCESS = frozenset({'last_login','note','updated_at'})
    WRITES = frozenset({'insert_one','update_one','find_one_and_update'})
    UNSUPPORTED = frozenset({'insert_many','update_many','replace_one','bulk_write','delete_one','delete_many','drop','rename',
                             'find_one_and_delete','find_one_and_replace'})

    def __init__(self, bridge, collection, account):
        self.bridge, self.raw, self.account = bridge, collection, account

    def __getattr__(self, name):
        if name in self.UNSUPPORTED:
            def deny(*args, **kwargs):
                raise ValueError('unsupported authority mutation: '+name)
            return deny
        target = getattr(self.raw, name)
        if name not in self.WRITES:
            return target

        def write(*args, **kwargs):
            update = args[1] if len(args)>1 else kwargs.get('update')
            safe = self.SAFE_ACCOUNT if self.account else self.SAFE_ACCESS
            # Only a narrow, known metadata update may bypass the authority gate.
            if name != 'insert_one' and isinstance(update, dict) and update and all(
                    op in {'$set','$inc','$unset','$setOnInsert'} and isinstance(fields,dict)
                    and all(field.split('.')[0] in safe for field in fields) for op,fields in update.items()):
                return target(*args, **kwargs)
            with self.bridge.prevent_pilot_probe(),self.bridge.outbox.lock:
                query = args[0] if args else kwargs.get('filter',{})
                if name == 'insert_one':
                    identities = [query.get('_id')]
                else:
                    identities = [row['_id'] for row in self.raw.find(query, {'_id':1})]
                    if not identities and isinstance(query.get('_id'),str):
                        identities = [query['_id']]
                if self.account:
                    keys = [key for key in identities if isinstance(key,str)]
                else:
                    keys = [row['_id'] for row in self.bridge.raw.campus_accounts.find({'student_id':{'$in':identities}}, {'_id':1})]
                # No account exists for a new student: there is no existing send identity to fence.
                return self.bridge.outbox.mutate(keys, self.raw.name+'.'+name,
                    lambda: target(*args, **kwargs), self.bridge.project)
        return write


class AuthorityDatabase:
    def __init__(self, bridge):
        self.bridge = bridge
        self.collections = {'campus_accounts':AuthorityCollection(bridge, bridge.raw.campus_accounts, True),
                            'campus_access':AuthorityCollection(bridge, bridge.raw.campus_access, False)}

    def __getitem__(self, name):
        return self.collections.get(name) if name in self.collections else self.bridge.raw[name]

    def __getattr__(self, name):
        return self.collections[name] if name in self.collections else getattr(self.bridge.raw,name)


class ManagementBridge:
    def __init__(self, directory, database, native, *, initialize=False,managed_gate=None):
        config = json.loads(native.rpc('CONFIG').removeprefix('CONFIG '))
        validate_native_policy(config)
        self.raw, self.native = database, native
        self.pilot_probe_guard = None
        self.managed_gate = managed_gate
        self.outbox = DurableOutbox(directory, native, initialize=initialize)
        self.database = AuthorityDatabase(self)

    def project(self, key):
        if key == 'global':
            control = json.loads(self.outbox.meta('control'))
            return {'enabled':control['live'],'plan':control['clock_ok'],'credential':0}, control
        row = self.raw.campus_accounts.find_one({'_id':key})
        if not row:
            return {'enabled':False,'plan':False,'credential':0}, {'missing':True,'account_id':key}
        access = self.raw.campus_access.find_one({'_id':row['student_id']}) or {}
        binding = {field:row.get(field) for field in ['student_id','identity','revision','credential_revision','plan','date_exceptions']}
        binding['access'] = {field:access.get(field) for field in ['enabled','revision']}
        identity_ok = (row.get('identity') or {}).get('student_id') == row['student_id']
        desired = {'enabled':bool(access.get('enabled') and identity_ok),
                   'plan':bool((row.get('plan') or {}).get('enabled') and identity_ok),
                   'credential':row['credential_revision']}
        gate=getattr(self,'managed_gate',None)
        return gate.project(key,desired,binding) if gate is not None else (desired,binding)

    def recover(self):
        with self.prevent_pilot_probe(),self.outbox.lock:
            self.outbox.failed=True
            try:
                return self.outbox.recover(self.project,lambda:[row['_id'] for row in self.raw.campus_accounts.find({}, {'_id':1})])
            except Exception:
                self.outbox.failed=True
                self.native.close_control()
                raise

    def set_control(self, **values):
        with self.prevent_pilot_probe():return self.outbox.set_control(**values)

    def prepare_dual_account(self, account_id, *, timeout=1.):
        """Stage permanent legacy exclusion; never activate a coordinator.

        STATE ACK follows C++'s exclusive account gate, which waits for the
        entire earlier curl operation. Unclassified or uncertain old outcomes
        still refuse staging; only proven unclaimed work can be terminalized.
        The gate then drains Python POSTs with its separate cross-process lock.
        """
        from local_app.dual_node.fence import NativeFenceAck
        from local_app.models import AppError
        gate=getattr(self,'managed_gate',None)
        owner=getattr(self.native,'dispatch_owner',None)
        if gate is None or owner is None:
            raise AppError('dual_native_fence_unconfirmed','原执行器撤权入口尚未就绪，未激活双机管理',409)

        def disable():
            with owner.lifecycle_lock,self.outbox.lock:
                self.outbox.mutate([account_id],'dual.permanent_legacy_fence',lambda:None,self.project)
                applied=self.outbox.applied(account_id)
                if not applied or applied['enabled'] or applied['plan']:
                    raise AppError('dual_native_fence_unconfirmed','原执行器撤权未取得确认，未激活',409)
                rows=self.raw.campus_runs.find({'account_id':account_id,
                    'status':{'$in':['queued','preparing','submitting','uncertain']}})
                for row in rows:
                    if row['status'] in {'submitting','uncertain'}:
                        raise AppError('dual_native_results_pending','原任务结果仍待核对，双机管理保持待启用',409)
                    binding=row.get('native_binding')
                    if binding:
                        fields=self.native.job_status(binding['job']).split()
                        if (len(fields)!=6 or fields[:2]!=['JOB',binding['job']] or fields[4]!='0'):
                            raise AppError('dual_native_results_pending','原生资格或结果尚未确认，双机管理保持待启用',409)
                    self.raw.campus_runs.update_one({'_id':row['_id'],'status':row['status']},
                        {'$set':{'status':'skipped','message':'账号已永久停用原发送路径，本任务未重新派发'}})
                return NativeFenceAck(account_id,self.native.identity,applied['version'],False,False,True)

        return gate.stage(account_id,native_disable=disable,timeout=timeout)

    def prevent_pilot_probe(self):
        callback = self.pilot_probe_guard
        return callback() if callback else nullcontext()

    def close(self):
        with self.prevent_pilot_probe():self.outbox.close()
