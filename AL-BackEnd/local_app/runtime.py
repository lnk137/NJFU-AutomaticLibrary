"""Single-process local application and scheduler lifecycle."""
import fcntl
import os
from pathlib import Path
from pymongo import MongoClient
from .accounts import AccountService
from .catalog import load_catalog
from .models import AppError
from .reservations import ReservationService
from .scheduler import LocalScheduler
from .school import CampusClient
from .store import LocalStore
from .vault import CredentialVault
from .today import TodayBookingService
from .web import create_app
from .auth import AuthService
from .inbox import configured_worker
from .form_imports import FormImportService
from .power import PowerService
from .dual_node.fence import ManagedAccountGate
from .booking_calendar import BookingCalendarService
from .cancellations import CancellationService
from .notifications import NotificationService


class Runtime:
    def __init__(self,settings,client_factory,*,native_factory=None):
        self.settings,self.lock,self.mongo,self.scheduler,self.accounts = settings,None,None,None,None
        self.closed=False
        self.inbox=None
        self.forms=None
        self.today=None
        self.power=None
        self.notifications=None
        self.cancellations=None
        self.native=None
        self.dual_publisher=None
        data=Path(settings.data_dir)
        if data.is_symlink():raise AppError('unsafe_data','数据目录不能为符号链接',503)
        data.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.managed_gate=ManagedAccountGate(data)
        self.managed_execution=self.managed_gate
        try:
            fd=os.open(data/'app.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            self.lock=os.fdopen(fd,'r+')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise AppError('already_running','本机后台已经运行，请使用现有窗口',409) from None
            self.mongo=MongoClient(settings.mongo_uri,serverSelectionTimeoutMS=2000,connectTimeoutMS=2000,
                                   socketTimeoutMS=2000,waitQueueTimeoutMS=2000,timeoutMS=2000)
            self.mongo.admin.command('ping')
            database=self.mongo[settings.database_name]
            from .native.selection import selection_values
            from .native.state import has_native_records
            lost_selection=(settings.executor=='python' and selection_values(data)!={'executor':'python'}
                and (selection_values(data).get('executor')=='cpp' or has_native_records(database)))
            if lost_selection:
                from .native.unavailable import UnavailableEngine
                self.native=UnavailableEngine(database)
                database=self.native.database
            elif settings.executor=='cpp':
                from .native.engine import NativeEngine
                try:
                    self.native=NativeEngine(settings,database,native_factory=native_factory,
                        client_is_fixture=getattr(client_factory,'isolated_fixture',False),managed_gate=self.managed_gate)
                except Exception:
                    from .native.unavailable import UnavailableEngine
                    self.native=UnavailableEngine(database)
                database=self.native.database
            self.store=LocalStore(database)
            self.store.managed_gate=self.managed_gate
            self.vault=CredentialVault.open(data/'credentials/key',has_encrypted_accounts=self.store.accounts.count_documents({})>0)
            catalog=load_catalog(settings.root/'AL-BackEnd/座位信息')
            self.auth=AuthService(self.store)
            self.accounts=AccountService(self.store,self.vault,catalog,self.managed_gate.client_factory(client_factory),access_allowed=self.auth.is_allowed)
            self.accounts.managed_gate=self.managed_gate
            self.auth.accounts=self.accounts
            if self.native:self.reservations,self.scheduler=self.native.attach(self.accounts,self.store)
            else:
                self.reservations=ReservationService(self.accounts,self.store,settings.allow_live)
                self.store.recover_interrupted()
                self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store)
            self.accounts.after_query=self.reservations.reconcile_account
            self.today=TodayBookingService(self.accounts,self.reservations,self.store)
            self.calendar=BookingCalendarService(self.store,catalog)
            self.cancellations=CancellationService(self.accounts,self.reservations,self.store,quiet_window=self.today.batches._quiet)
            def mail_quiet_window():
                try:self.today.batches._quiet()
                except AppError:return True
                return False
            self.notifications=NotificationService(settings,self.accounts,self.store,self.vault,quiet_window=mail_quiet_window)
            self.forms=FormImportService(self.accounts,self.store,self.auth,self.today)
            self.inbox=configured_worker(settings,self.accounts,self.store,self.auth,self.scheduler)
            if self.native and self.inbox and hasattr(self.inbox,'request_sync'):
                self.native.sync_callback=self.inbox.request_sync
            from .execution_health import execution_health
            self.power=PowerService(settings,self.accounts,self.store,self.scheduler,catalog,
                status_provider=lambda:execution_health(self.scheduler_status(),
                    managed=self.managed_execution.has_managed_scope()))
            from .dual_node.publisher import PlanPublisherLoop
            self.dual_publisher=PlanPublisherLoop.from_runtime(self)
            if self.native and self.dual_publisher:self.native.dual_control=self.dual_publisher.control
            self.app=create_app(settings,self.accounts,self.store,catalog,self.scheduler_status,
                                reconcile=self.reservations.reconcile,today=self.today,auth=self.auth,inbox=self.inbox,form_imports=self.forms,power=self.power,
                                executor=self.native,calendar=self.calendar,notifications=self.notifications,cancellations=self.cancellations,
                                credential_sync=lambda account_id, revision: self.dual_publisher.credential_sync_state(account_id, revision) if self.dual_publisher else 'unknown')
        except Exception:self.close();raise

    def start(self):
        if self.native:self.native.start()
        else:self.scheduler.start()
        if self.inbox:self.inbox.start()
        if self.power:self.power.start()
        if self.dual_publisher:self.dual_publisher.start()
        if self.notifications:self.notifications.start()

    def scheduler_status(self):
        result=self.scheduler.status()
        if self.dual_publisher:result['dual_node_publisher']=self.dual_publisher.status()
        return result

    def close(self):
        if self.closed:return
        self.closed=True
        if self.native:self.native.quiesce()
        elif self.scheduler:self.scheduler.stopping.set()
        # Revoke Python/manual/cancellation admission before draining mail or
        # any worker that may wait for an already-sent request to finish.
        if getattr(self,'cancellations',None):self.cancellations.close()
        if getattr(self,'notifications',None):self.notifications.close()
        if self.dual_publisher:self.dual_publisher.close()
        if self.today:self.today.close()
        if self.forms:self.forms.close()
        if self.power:self.power.close()
        if self.inbox:self.inbox.close()
        if self.native:self.native.close()
        elif self.scheduler:self.scheduler.stop()
        if self.accounts:self.accounts.close()
        if self.mongo:self.mongo.close()
        if self.lock:self.lock.close()


def create_runtime(settings,*,client_factory=CampusClient,native_factory=None):
    return Runtime(settings,client_factory,native_factory=native_factory)
