"""Root-reviewed file actions, independent from ordinary user scheduling."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import time
import uuid

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .form_files import parse_form
from .models import AppError, ZONE, stamp, validate_plan
from .import_today import execute_import_today


class FormImportService:
    def __init__(self, accounts, store, auth, today, *, clock=time.time):
        self.accounts,self.store,self.auth,self.today,self.clock=accounts,store,auth,today,clock
        self.batches=store.db.campus_form_imports
        self.batches.create_index('purge_at',expireAfterSeconds=0)
        self.batches.update_many({'state':{'$in':['validating','confirming']}},
            {'$set':{'state':'interrupted','message':'服务重启中断，请核对已处理结果；不会自动重交'},'$unset':{'payload':''}})
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='form-import')

    @contextmanager
    def _lock(self,key):
        lock=self.accounts.account_lock(key)
        if not lock.acquire(blocking=False):raise AppError('account_busy','账号正在使用，请稍后重新导入',409)
        try:yield
        finally:lock.release()

    def _quiet(self,extra='07:00:00'):
        now=datetime.fromtimestamp(self.clock(),ZONE)
        rows=list(self.store.accounts.find({'plan.enabled':True},{'plan.trigger':1}).limit(5001).max_time_ms(1000))
        if len(rows)>5000:raise AppError('import_limit','开启的计划过多，暂不能导入',409)
        triggers={'07:00:00',extra}|{row['plan'].get('trigger') for row in rows}
        for trigger in triggers:
            for delta in (-1,0,1):
                try:due=datetime.fromisoformat(f'{now.date()+timedelta(days=delta)}T{trigger}').replace(tzinfo=ZONE)
                except (ValueError,TypeError):continue
                if -183 <= (now-due).total_seconds() <= 40:
                    raise AppError('release_window','临近放号或正在执行，请稍后重新导入；现有计划保持原样',409)

    @staticmethod
    def _safe_row(row):
        keys=('line','alias','student_id','action','plan','today','account_id','account_saved','status','message','run_id','result')
        return {key:row[key] for key in keys if key in row}

    def _view(self,batch):
        rows=[self._safe_row(row) for row in batch.get('rows',[])]
        return {'id':batch['_id'],'filename':batch['filename'],'state':batch['state'],'created_at':batch['created_at'],
            'expires_at':batch.get('expires_at'),'message':batch.get('message',''),'rows':rows,
            'ready_count':sum(row['status']=='ready' for row in rows)}

    def get(self,key):
        batch=self.batches.find_one({'_id':key})
        if not batch:raise AppError('not_found','导入记录不存在或已过期',404)
        return self._view(batch)

    def recent(self):return [self._view(row) for row in self.batches.find().sort('created_at',-1).limit(5)]

    def create(self,filename,content,request_id):
        try:key=uuid.UUID(request_id).hex
        except (ValueError,TypeError,AttributeError):raise AppError('invalid_import_id','请重新选择文件') from None
        if not isinstance(filename,str) or not 1 <= len(filename) <= 120:raise AppError('invalid_form','文件名无效')
        digest=hmac.new(self.accounts.vault.key,content,hashlib.sha256).hexdigest()
        old=self.batches.find_one({'_id':key})
        if old:
            if old['digest']!=digest:raise AppError('import_conflict','文件编号已用于其他内容，请重新选择文件',409)
            return self._view(old)
        rows=parse_form(filename,content,self.accounts.catalog,datetime.fromtimestamp(self.clock(),ZONE).date())
        now=self.clock()
        batch={'_id':key,'digest':digest,'filename':filename,'created_at':now,'expires_at':now+900,
            'purge_at':datetime.fromtimestamp(now+86400,timezone.utc),'state':'validating','rows':[],
            'payload':self.accounts.vault.seal(key,json.dumps(rows,ensure_ascii=False))}
        try:self.batches.insert_one(batch)
        except DuplicateKeyError:return self.create(filename,content,request_id)
        self.pool.submit(self._validate,key)
        return self._view(batch)

    def _validate_row(self,row):
        if row.get('error'):raise AppError('invalid_form',row['error'])
        student=row['student_id'];trigger=(row.get('plan') or {}).get('trigger','07:00:00')
        self._quiet(trigger)
        old=self.store.find_account_by_student(student)
        access=self.auth.access.find_one({'_id':student})
        if access and access.get('enabled') is not True:raise AppError('access_denied','此学号已停用，请先在用户管理重新授权')
        if old and (not access or not access.get('enabled')):raise AppError('access_denied','此账号尚未授权，请先在用户管理核对')
        if old and old['_id'] in self.accounts.busy:raise AppError('account_busy','此账号正在预约，请稍后导入')
        client=self.accounts.client_factory(student,row['password'],allow_live=False)
        try:
            identity=client.login()
            if identity.student_id!=student:raise AppError('identity_mismatch','学校返回的身份不一致')
            self._quiet(trigger)
            with self._lock('student:'+student):
                latest_access=self.auth.access.find_one({'_id':student})
                if latest_access and latest_access.get('enabled') is not True:
                    raise AppError('access_denied','此学号的权限已停用，未保存或更新账号')
                current=self.store.find_account_by_student(student)
                if current!=old:raise AppError('conflict','账号已在另一处更新，请重新导入',409)
                if not old:
                    account_id=uuid.uuid4().hex
                    saved=self.store.save_verified_account(account_id,identity,row['alias'],self.accounts.vault.seal(account_id,row['password']),None)
                    row['account_saved']=True
                    if not access:self.auth.set_access(student,{'enabled':True,'note':row['alias'],'expected_revision':None})
                    old=self.store.get_account(saved.id)
                row.update(account_id=old['_id'],expected_revision=old['revision'],identity=asdict(identity),
                    credential=self.accounts.vault.seal(old['_id'],row['password']))
            if row['action']=='today':
                self.today._validate(row['account_id'],row['today']|{'expected_revision':row['expected_revision']})
                pending=self.store.runs.find_one({'account_id':row['account_id'],'target_date':row['today']['date'],
                    'status':{'$in':['preparing','submitting','uncertain']}})
                if pending:raise AppError('booking_uncertain','当天有待确认任务，请先核对学校记录')
                day=datetime.fromisoformat(row['today']['date']).date()
                records=client.query(day,day+timedelta(days=3))
                begin,end=f"{day} {row['today']['begin']}:00",f"{day} {row['today']['end']}:00"
                for record in records:
                    if record.student_id!=student:raise AppError('identity_mismatch','学校查询身份不一致')
                    if record.begin<end and record.end>begin:raise AppError('booking_overlap','学校记录已有重叠预约，请先核对')
        finally:client.close()

    @staticmethod
    def _message(exc):
        if isinstance(exc,AppError):
            if exc.code in ('login_failed','verification_required','login_incomplete','identity_mismatch','network_error','school_http'):
                return '学校验证未通过，请核对账号密码、人工验证或稍后重试'
            return str(exc)
        return '本机处理未完成，请核对账号和执行记录后重试'

    def _validate(self,key):
        try:self._validate_batch(key)
        except Exception:
            self.batches.update_one({'_id':key,'state':'validating'},
                {'$set':{'state':'failed','message':'表单验证中断，请重新导入；现有计划保持原样'},'$unset':{'payload':''}})

    def _validate_batch(self,key):
        batch=self.batches.find_one({'_id':key});rows=json.loads(self.accounts.vault.unseal(key,batch['payload']))
        for row in rows:
            try:self._validate_row(row);row.update(status='ready',message='验证通过，等待整份确认')
            except Exception as exc:row.update(status='error',message=self._message(exc))
            row.pop('password',None);row.pop('error',None)
        self.batches.update_one({'_id':key,'state':'validating'}, {'$set':{'state':'ready','rows':[self._safe_row(row) for row in rows],
            'expires_at':self.clock()+900,'payload':self.accounts.vault.seal(key,json.dumps(rows,ensure_ascii=False))}})

    def confirm(self,key):
        batch=self.batches.find_one({'_id':key})
        if not batch:raise AppError('not_found','导入记录不存在',404)
        if batch['state']!='ready':return self._view(batch)
        if batch['expires_at']<=self.clock():raise AppError('import_expired','表单审阅已过期，请重新导入验证',409)
        self._quiet()
        claimed=self.batches.find_one_and_update({'_id':key,'state':'ready','expires_at':{'$gt':self.clock()}},
            {'$set':{'state':'confirming'}},return_document=ReturnDocument.AFTER)
        if claimed:self.pool.submit(self._execute,key)
        return self.get(key)

    def _commit(self,key,row,old):
        changed=self.accounts.vault.unseal(old['_id'],old['secret'])!=self.accounts.vault.unseal(old['_id'],row['credential'])
        values={'last_form_import':{'batch_id':key,'line':row['line'],'at':stamp()},'alias':row['alias'] or old['alias'],
            'identity':row['identity'],'verified_at':stamp(),'login_error':None}
        increment={}
        if changed:values['secret']=row['credential'];increment['credential_revision']=1
        if row['action']=='scheduled':values['plan']=row['plan']
        if changed or row['action']=='scheduled':increment['revision']=1
        update={'$set':values}
        if increment:update['$inc']=increment
        current=self.store.accounts.find_one_and_update({'_id':old['_id'],'revision':old['revision']},update,return_document=ReturnDocument.AFTER)
        if not current:raise AppError('conflict','账号已更新，未覆盖，请重新导入',409)
        return current

    def _apply(self,key,row,expires_at):
        if row['action']=='today':
            result=execute_import_today(self,key,row,expires_at)
            return {'status':'done' if result['result']['status']=='success' else 'error','run_id':result['run_id'],
                'message':result['result']['message'],'result':result['result']['status']}
        with self._lock('student:'+row['student_id']),self._lock(row['account_id']):
            old=self.store.get_account(row['account_id'])
            if old['revision']!=row['expected_revision']:raise AppError('conflict','账号或计划已更新，未覆盖；请重新导入',409)
            if old['_id'] in self.accounts.busy:raise AppError('account_busy','账号正在执行，请稍后处理')
            if not self.auth.is_allowed(row['student_id']):raise AppError('access_denied','此学号的使用权限已停用')
            if row['action']=='scheduled':row['plan']=asdict(self.accounts._validated_plan_update(old,row['plan']))
            self._quiet((row.get('plan') or {}).get('trigger','07:00:00'))
            if self.clock()>=expires_at:raise AppError('import_expired','整份审阅已过期，本行未执行')
            self._commit(key,row,old)
        self.accounts._observe_auth(row['account_id'])
        return {'message':'定时计划已保存','status':'done'}

    def _execute(self,key):
        try:self._execute_batch(key)
        except Exception:
            self.batches.update_one({'_id':key,'state':'confirming'},
                {'$set':{'state':'interrupted','message':'执行记录待核对，请先查看账号和预约记录；不会自动重交'},'$unset':{'payload':''}})

    def _execute_batch(self,key):
        batch=self.batches.find_one({'_id':key});rows=json.loads(self.accounts.vault.unseal(key,batch['payload']))
        for row in rows:
            if row['status']=='ready':
                try:
                    if self.clock()>=batch['expires_at']:raise AppError('import_expired','审阅已过期，本行未执行')
                    row.update(self._apply(key,row,batch['expires_at']))
                except Exception as exc:row.update(status='error',message=self._message(exc))
            self.batches.update_one({'_id':key,'state':'confirming'},{'$set':{'rows':[self._safe_row(item) for item in rows]}})
        self.batches.update_one({'_id':key,'state':'confirming'}, {'$set':{'state':'done'},'$unset':{'payload':''}})

    def close(self):self.pool.shutdown(wait=True,cancel_futures=False)
