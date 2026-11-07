"""Own one native authority inside the existing Runtime and its data lock."""
from pathlib import Path
from pymongo.errors import ConnectionFailure

from ..models import AppError
from ..limits import PARALLEL_ACCOUNT_LIMIT
from .build import private_directory
from .selection import verify_selection
from .protocol import NativeProcess
from .management import ManagementBridge
from .dispatch import CampusDispatch
from .scheduler import CampusScheduler
from .recovery import EngineRecovery
from .outbox import SyncPending
from .protocol import ProtocolError
from .state import prepare_state,anchor_state
from .pilot_selection import PilotSelection
from .pilot_coordinator import PilotProofCoordinator


class NativeEngine(EngineRecovery):
    def __init__(self,settings,database,*,native_factory=None,client_is_fixture=False,managed_gate=None):
        self.settings=settings;self.process=self.bridge=self.dispatch=self.scheduler=None
        self.pilot_coordinator=None;self.pilot_error=None
        if native_factory is not None:
            if (not client_is_fixture or not getattr(native_factory,'isolated_fixture',False)
                    or not database.name.startswith('njfu_test_')
                    or any(host not in {'127.0.0.1','localhost'} or port in {5003,27019}
                           for host,port in database.client.nodes)):
                raise AppError('native_fixture_denied','测试执行器只能连接独立测试数据库',503)
        try:
            build=verify_selection(settings) if native_factory is None else None
            initialize,identity=prepare_state(settings.data_dir,database)
            directory=private_directory(Path(settings.data_dir)/'native')
            self.process=native_factory(directory) if native_factory else NativeProcess(directory,build.executable,initialize=initialize,
                count=PARALLEL_ACCOUNT_LIMIT,verified_binary_sha256=build.binary_sha256)
            if identity is not None and self.process.identity!=identity:
                raise AppError('native_state_invalid','执行账本身份与独立记录不一致，保持暂停',503)
            outbox=directory/'outbox'
            self.bridge=ManagementBridge(outbox,database,self.process,initialize=initialize,managed_gate=managed_gate)
            if initialize:anchor_state(settings.data_dir,self.process.identity)
            self.database=self.bridge.database
            self.initialize_recovery()
            try:self.bridge.recover()
            except (ConnectionFailure,SyncPending,OSError,ProtocolError):
                self.bridge.outbox.failed=True;self.process.close_control()
                self.recovery_message='启动连接暂未确认，正在自动恢复'
        except Exception:self.close();raise

    def attach(self,accounts,store):
        self.dispatch=CampusDispatch(self.bridge,accounts,store)
        self.scheduler=CampusScheduler(accounts,self.dispatch,store)
        self.scheduler.runtime_diagnostics=self.diagnostics
        self.dispatch.recovery_classifier=self.scheduler._reconcile_unregistered
        path=Path(self.settings.data_dir)/'pilot-canary.json'
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            self.pilot_error='试点配置无法读取'
        else:
            try:
                selector=PilotSelection.from_file(path)
                coordinator=PilotProofCoordinator(selector,self.dispatch)
                self.dispatch.pilot_matcher=selector.selects
                self.dispatch.pilot_guard=selector.verify
                self.bridge.pilot_probe_guard=coordinator.prevent_probe
                self.pilot_coordinator=coordinator
            except (OSError,ValueError):
                self.pilot_error='试点配置无效，未启用试点'
        if not self.bridge.outbox.failed:
            try:self.dispatch.recover_runs()
            except (ConnectionFailure,SyncPending,OSError,ProtocolError):
                self.bridge.outbox.failed=True;self.process.close_control()
                self.recovery_message='初始记录核对暂未完成，正在自动恢复'
        # Native-owned rows are classified above; never apply Python's generic
        # interrupted-run reset to an uncertain or claimed native task.
        store.runs.update_many({'spec.kind':'manual','status':'submitting'},
            {'$set':{'status':'uncertain','message':'上次手动提交中断，请查询学校记录核对'}})
        return self.dispatch.service,self.scheduler

    def start(self):
        if not self.bridge.outbox.failed:
            try:self.bridge.set_control(live=self.settings.allow_live and not self.operator_paused,clock_ok=True)
            except (ConnectionFailure,SyncPending,OSError,ProtocolError):
                self.bridge.outbox.failed=True;self.process.close_control()
        self.scheduler.start()
        self.start_recovery()
        if self.pilot_coordinator:
            try:self.pilot_coordinator.start()
            except Exception:self.pilot_error='试点时间检查未能启动'

    def diagnostics(self):
        result=super().diagnostics()
        if self.pilot_coordinator:
            result['pilot_canary']={'configured':True,'release_at':self.pilot_coordinator.selector.due_at,
                                    'proof_loaded':bool(getattr(self.dispatch,'pilot_evidence',None)),
                                    'coordinator_alive':bool(self.pilot_coordinator.thread and self.pilot_coordinator.thread.is_alive()),
                                    'fallback_if_unverified':True,
                                    'probe_stop_unconfirmed':self.pilot_coordinator.stop_unconfirmed,
                                    'error':self.pilot_error}
        elif self.pilot_error:
            result['pilot_canary']={'configured':False,'error':self.pilot_error}
        return result

    def quiesce(self):
        # First revoke sends; keep the management store open while form/inbox
        # workers finish legitimate configuration writes during Runtime.close.
        self.closed=True
        if hasattr(self,'stopping'):self.stopping.set()
        if self.pilot_coordinator:self.pilot_coordinator.close()
        if self.scheduler:
            self.scheduler.stopping.set();self.scheduler.maintenance.set()
        if self.dispatch:self.dispatch.close()
        for name in ('thread','clock_thread'):
            thread=getattr(self,name,None)
            if thread:thread.join()

    def close(self):
        self.quiesce()
        if self.scheduler:self.scheduler.stop()
        elif self.dispatch:self.dispatch.close()
        if self.bridge:self.bridge.close()
        if self.process:self.process.close()
