"""Credential-free health projection and bounded local administrator alerts."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

STATES = {'waiting','probing','eligible','selected','ready','authorized','completed',
          'failed','cancelled','not_sent','uncertain','missed','blocked','paused','idle','unavailable'}
CODES = {'probe_not_due','probe_in_progress','probe_eligible','owner_selected','owner_prepared',
         'authorization_issued','completed','failed','cancelled','not_sent','execution_uncertain',
         'release_without_authorization','probe_report_missing','nodes_unavailable',
         'owner_unavailable','preparation_not_ready','operator_paused','account_unavailable','no_tasks',
         'monitor_unavailable','clock_untrusted'}
ALERT_CODES = {'release_without_authorization','probe_report_missing','nodes_unavailable',
               'owner_unavailable','preparation_not_ready','execution_uncertain','clock_untrusted'}
MESSAGES = {'clock_untrusted':'协调器可信时间未通过校验，执行已阻断',
            'release_without_authorization':'双机任务到期仍未授权，未补交，请核查执行链路',
            'probe_report_missing':'双机任务缺少有效探测报告，请核查节点诊断',
            'nodes_unavailable':'双机执行节点均离线或心跳过期',
            'owner_unavailable':'选定执行节点已离线或重新启动',
            'preparation_not_ready':'临近放号，选定节点尚未完成准备',
            'execution_uncertain':'双机执行结果待核实，禁止重放'}
LIMIT = 128
MAX_BYTES = 65536


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_snapshot(value):
    if (type(value) is not dict or value.get('schema') != 1 or not finite(value.get('checked_at'))
            or type(value.get('operator_paused')) is not bool or type(value.get('nodes')) is not list
            or len(value['nodes']) > 2 or type(value.get('tasks')) is not list or len(value['tasks']) > 512):
        raise ValueError('monitor_unavailable')
    for node in value['nodes']:
        if (type(node) is not dict or node.get('node') not in ('mac','ubuntu')
                or type(node.get('online')) is not bool):raise ValueError('monitor_unavailable')
    for row in value['tasks']:
        if (type(row) is not dict or type(row.get('task_id')) is not str
                or not re.fullmatch('[0-9a-f]{64}',row['task_id'])
                or type(row.get('account_ref')) is not str or len(row['account_ref']) > 128
                or not finite(row.get('release_at')) or row.get('state') not in STATES
                or row.get('code') not in CODES or type(row.get('ready')) is not bool
                or type(row.get('degraded')) is not bool):raise ValueError('monitor_unavailable')
    return value


def projection(snapshot, registrations):
    """Map opaque refs internally; public consumers apply account scoping."""
    validate_snapshot(snapshot);now=snapshot['checked_at'];by_ref={}
    for local,record in registrations.items():
        if type(record) is dict and type(record.get('account_ref')) is str:
            by_ref.setdefault(record['account_ref'],[]).append(local)
    accounts={}
    for ref,locals_ in by_ref.items():
        if len(locals_)==1:
            accounts[locals_[0]]=summarize([r for r in snapshot['tasks'] if r['account_ref']==ref],now)
    return {'checked_at':now,'accounts':accounts,'summary':summarize(snapshot['tasks'],now),
            'nodes_online':sum(n['online'] for n in snapshot['nodes'])}


def summarize(rows,now):
    active=[r for r in rows if r['release_at']>=now-30 and r['state'] not in ('completed','failed','cancelled','not_sent')]
    if not active:return {'state':'idle','ready':False,'degraded':False,'code':'no_tasks'}
    release=min(r['release_at'] for r in active);batch=[r for r in active if r['release_at']==release]
    order={'unavailable':0,'blocked':1,'paused':2,'missed':3,'uncertain':4,'waiting':5,
           'probing':6,'eligible':7,'selected':8,'authorized':9,'ready':10}
    worst=min(batch,key=lambda r:order.get(r['state'],0))
    return {'state':worst['state'],'code':worst['code'],'ready':all(r['ready'] for r in batch),
            'degraded':any(r['degraded'] for r in batch),'release_at':release}


class AlertJournal:
    """Private atomic file, 128 stable events maximum, no school payloads."""
    def __init__(self,path):
        self.path=Path(path).absolute();self.records=[];self.persistence='available'
        try:self._paths();self.records=self._read()
        except (OSError,ValueError,TypeError,KeyError):self.persistence='unavailable'

    def _paths(self):
        aliases={Path('/var'):Path('/private/var'),Path('/tmp'):Path('/private/tmp')}
        for parent in self.path.parents:
            if parent.is_symlink() and not (parent in aliases and parent.resolve()==aliases[parent]):
                raise ValueError('unsafe_alert_path')
        if not self.path.parent.is_dir():raise ValueError('unsafe_alert_path')
        if self.path.exists() or self.path.is_symlink():
            info=self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
                    or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>MAX_BYTES):raise ValueError('unsafe_alert_path')

    def _read(self):
        if not self.path.exists():return []
        fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'r') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES:raise ValueError('unsafe_alert_path')
            value=json.loads(stream.read(MAX_BYTES+1))
        if type(value) is not dict or set(value)!={'schema','alerts'} or value['schema']!=1:raise ValueError('invalid_alert_file')
        rows=value['alerts']
        if type(rows) is not list or len(rows)>LIMIT:raise ValueError('invalid_alert_file')
        for row in rows:
            if (type(row) is not dict or set(row)!={'id','task_ref','code','first_seen','last_seen','occurrences','active'}
                    or type(row['id']) is not str or not re.fullmatch('[0-9a-f]{64}',row['id'])
                    or type(row['task_ref']) is not str or not re.fullmatch('[0-9a-f]{16}',row['task_ref'])
                    or row['code'] not in ALERT_CODES or not finite(row['first_seen']) or not finite(row['last_seen'])
                    or type(row['occurrences']) is not int or not 1<=row['occurrences']<=2147483647
                    or type(row['active']) is not bool):raise ValueError('invalid_alert_file')
        return rows

    def observe(self,snapshot):
        try:
            self._paths();now=snapshot['checked_at']
            if not finite(now):raise ValueError('invalid_alert_time')
            fd=os.open(self.path.with_suffix('.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'a+b') as stream:
                info=os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
                        or stat.S_IMODE(info.st_mode)!=0o600):raise ValueError('unsafe_alert_lock')
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);self._paths();previous=self._read()
                by_id={r['id']:dict(r) for r in previous};seen=set()
                for row in snapshot['tasks']:
                    code=row.get('code');task_id=row.get('task_id')
                    if code not in ALERT_CODES or type(task_id) is not str or not re.fullmatch('[0-9a-f]{64}',task_id):continue
                    key=hashlib.sha256((task_id+':'+code).encode()).hexdigest();seen.add(key)
                    if key not in by_id:
                        by_id[key]={'id':key,'task_ref':task_id[:16],'code':code,'first_seen':now,
                                    'last_seen':now,'occurrences':0,'active':True}
                    item=by_id[key];item.update(last_seen=now,active=True,occurrences=min(2147483647,item['occurrences']+1))
                for key,item in by_id.items():
                    if key not in seen:item['active']=False
                records=sorted(by_id.values(),key=lambda r:(r['last_seen'],r['id']))[-LIMIT:]
                if records==previous:
                    self.records=records;self.persistence='available';return
                encoded=json.dumps({'schema':1,'alerts':records},sort_keys=True,separators=(',',':')).encode()
                if len(encoded)>MAX_BYTES:raise ValueError('alert_limit')
                temporary=self.path.with_name('.'+self.path.name+'.'+uuid.uuid4().hex)
                try:
                    out=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(out,'wb') as target:target.write(encoded);target.flush();os.fsync(target.fileno())
                    self._paths();os.replace(temporary,self.path)
                    directory=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(directory)
                    finally:os.close(directory)
                finally:
                    if temporary.exists():temporary.unlink()
                self.records=records;self.persistence='available'
        except (OSError,ValueError,TypeError,KeyError):self.persistence='unavailable'

    def status(self):
        return {'persistence':self.persistence,'alerts':[dict(row) for row in reversed(self.records)]}
