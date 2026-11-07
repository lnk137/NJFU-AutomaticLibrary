"""Keep local-root diagnostics readable without enabling another sender."""
import threading

from ..models import AppError
from ..reservations import ReservationService
from .management import AuthorityDatabase


class BlockedOutbox:
    failed=True
    def __init__(self):self.lock=threading.RLock()
    def mutate(self,*args,**kwargs):raise AppError('native_unavailable','执行器未就绪，暂不能修改执行配置',503)


class BlockedBridge:
    def __init__(self,raw):self.raw=raw;self.outbox=BlockedOutbox()
    def project(self,key):raise AppError('native_unavailable','执行器未就绪',503)


class DormantScheduler:
    clock_ok=False
    def status(self):
        return {'executor':'cpp','executor_state':'blocked','executor_message':'执行器未就绪，请管理员核对安装与执行记录',
            'allow_live':False,'scheduler_running':False,'clock_ok':False,'ready_to_submit':False,
            'capacity':24,'last_error':'执行器未就绪，预约已暂停；账号、计划和历史保留',
            'scheduler_warning':None,'execution_message':'自动预约已暂停','queued':0,'preparing':0,'next_due':None}
    def start(self):pass
    def stop(self):pass


class UnavailableEngine:
    def __init__(self,database):
        self.bridge=BlockedBridge(database);self.database=AuthorityDatabase(self.bridge)
        self.process=None;self.scheduler=DormantScheduler()
    def attach(self,accounts,store):return ReservationService(accounts,store,False),self.scheduler
    def start(self):pass
    def quiesce(self):pass
    def close(self):pass
    def recover_once(self,**kwargs):raise AppError('native_unavailable','请先修复执行器安装或记录，再重启后台；未重置任何记录',503)
    def pause(self):raise AppError('native_unavailable','执行器已停止发送，请先检查安装与执行记录',503)
