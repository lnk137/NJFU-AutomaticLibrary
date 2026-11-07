"""Recover native authority by readback; never replay a booking or Mongo action."""
from datetime import timedelta
from email.utils import parsedate_to_datetime
import logging
import threading
import time

import requests

from ..elapsed_clock import elapsed_time
from ..inbox import io_quiet
from ..models import AppError
from .clock_validation import ProbeSlot,ntp_clock_consensus
from .recovery_retry import RecoveryBudget


_school_probe=ProbeSlot()


def _school_clock_check():
    session=response=None;before=time.time();started=elapsed_time()
    reason='school_unavailable'
    try:
        session=requests.Session();session.trust_env=False
        response=session.get('https://libseat.njfu.edu.cn/ic-web/sysConfig/public',
            timeout=(.8,1.2),allow_redirects=False,
            headers={'Cache-Control':'no-cache, no-store','Pragma':'no-cache'})
        after=time.time();duration=elapsed_time()-started
        if abs((after-before)-duration)>.25:
            return {'ok':False,'source':'local','reason':'clock_changed','checked_at':after}
        value=response.headers.get('Date');age=response.headers.get('Age')
        parsed=parsedate_to_datetime(value) if isinstance(value,str) else None
        fresh=age is None or (isinstance(age,str) and age.isdecimal() and int(age)==0)
        if (response.status_code==200 and not response.history and fresh and parsed is not None
                and parsed.tzinfo is not None and parsed.utcoffset()==timedelta(0)
                and 0<=duration<=2.5 and before-3<=parsed.timestamp()<=after+3):
            return {'ok':True,'source':'school_https','reason':'confirmed','checked_at':after}
        reason='school_date_untrusted'
    except (OSError,ValueError,TypeError,requests.RequestException):pass
    finally:
        if response is not None:response.close()
        if session is not None:session.close()
    return {'ok':False,'source':'school_https','reason':reason,'checked_at':time.time()}


def verify_wall_clock():
    """Bound DNS and HTTP waiting too; a hung probe cannot block recovery exit."""
    before=time.time();started=elapsed_time()
    try:school=_school_probe.submit(_school_clock_check).result(timeout=2.1)
    except Exception:school={'ok':False,'reason':'school_unavailable'}
    after=time.time();duration=elapsed_time()-started
    if abs((after-before)-duration)>.25 or not 0<=duration<=5:
        return {'ok':False,'source':'local','reason':'clock_changed','checked_at':after}
    if school['reason']=='clock_changed':return school
    if school['ok'] and 0<=after-school['checked_at']<=2.5:return school
    result=ntp_clock_consensus()
    after=time.time();duration=elapsed_time()-started
    if not 0<=duration<=5 or abs((after-before)-duration)>.25:
        return {'ok':False,'source':'local','reason':'clock_changed','checked_at':after}
    return result | {'checked_at':after,'school_reason':school['reason']}


def trusted_wall_clock():
    return verify_wall_clock()['ok']


class EngineRecovery:
    def initialize_recovery(self):
        self.recovery_lock=threading.Lock();self._clock_lock=threading.Lock()
        self.stopping=threading.Event();self._retry=threading.Event();self.thread=None;self.clock_thread=None
        self.closed=False;self.recovering=False;self.hard_error=None;self.recovery_message=None
        self.last_recovery_at=self.last_wake_at=None;self._last_pair=None
        self.clock_validator=self._verify_clock;self.sync_callback=None
        self.last_clock_check=None;self.last_clock_fault=None;self._clock_fault_active=False
        self._clock_generation=0
        self.recovery_budget=RecoveryBudget();self._clock_deferred=False
        self._stable_since=self.elapsed()
        with self.bridge.outbox.lock:
            value=self.bridge.outbox.db.execute("SELECT value FROM meta WHERE key='operator_paused'").fetchone()
            if value is None:
                with self.bridge.outbox.transaction():self.bridge.outbox._meta('operator_paused','0')
                self.operator_paused=False
            else:self.operator_paused=value[0]=='1'

    def _verify_clock(self):
        self.last_clock_check=verify_wall_clock()
        return self.last_clock_check['ok']

    @staticmethod
    def elapsed():return elapsed_time()

    def observe_clock(self,wall,elapsed):
        with self._clock_lock:
            previous=self._last_pair;self._last_pair=(wall,elapsed)
            if not previous:return
            delta_wall,delta_elapsed=wall-previous[0],elapsed-previous[1]
            if abs(delta_wall-delta_elapsed)>.25:
                self._stable_since=self.elapsed();self._clock_generation+=1
        if delta_elapsed>30:
            self.last_wake_at=wall
            self.request_recovery()
            self.notify_sync()

    def notify_sync(self):
        try:
            if self.sync_callback:self.sync_callback()
        except Exception:
            # The inbox also polls continuously. A failed optional nudge must
            # not kill recovery or reclassify a healthy sender as unavailable.
            logging.getLogger(__name__).warning('Inbox synchronization notification deferred')

    def diagnostics(self):
        failed=(self.bridge.outbox.failed or self.process.process is None or self.process.process.poll() is not None)
        clock_bad=not self.scheduler.clock_ok or not self.dispatch.clock_ok() or self.dispatch.clock_fault.is_set()
        state=('blocked' if self.hard_error else 'recovering' if self.recovering or failed or clock_bad else
               'paused' if self.operator_paused or not self.settings.allow_live or self.closed else 'ready')
        message=self.hard_error or self.recovery_message if state in {'recovering','blocked'} else None
        return {'executor':'cpp','executor_state':state,'executor_message':message,
                'operator_paused':self.operator_paused,'last_recovery_at':self.last_recovery_at,'last_wake_at':self.last_wake_at,
                'clock_check':self.last_clock_check,'last_clock_fault':self.last_clock_fault,
                'recovery_policy':self.recovery_budget.snapshot(self.elapsed())}

    def request_recovery(self):
        self._retry.set()

    def _fence_unconfirmed_sender(self):
        """A failed revoke must stop the owned sender even if control close fails."""
        # Stop granting new leases even when both control close and kill fail.
        self.bridge.outbox.failed=True
        # An earlier durable ACK may already prove the global gate is closed.
        # Keep the outbox failed while holding its lock so no re-enable can race
        # this decision. A pending state is never treated as confirmation.
        try:
            with self.bridge.outbox.lock:
                applied=self.bridge.outbox.applied('global')
                pending=self.bridge.outbox.pending('global')
                if applied and not applied['enabled'] and not applied['plan'] and pending is None:
                    self.bridge.outbox.failed=True
                    return False
        except Exception:pass
        try:self.process.close_control()
        except Exception:pass
        child=self.process.process
        if child is None or child.poll() is not None:return False
        try:
            child.kill();child.wait(timeout=3)
            if child.poll() is not None:return True
        except Exception:pass
        raise AppError('native_recovery_pending','撤权未确认且执行器停止未确认，保持暂停',503)

    def pause(self):
        with self.recovery_lock,self.bridge.outbox.lock:
            with self.bridge.outbox.transaction():self.bridge.outbox._meta('operator_paused','1')
            self.operator_paused=True
            try:self.bridge.set_control(live=False,clock_ok=self.scheduler.clock_ok)
            except Exception:self._fence_unconfirmed_sender();raise

    def recover_once(self,*,resume=False,trust_clock=False):
        if not self.recovery_lock.acquire(blocking=False):raise AppError('native_recovery_busy','恢复正在进行，请稍后查看',409)
        try:
            if self.closed:raise AppError('native_paused','执行器已停止',409)
            if self.hard_error:raise AppError('native_state_invalid',self.hard_error,503)
            self.recovering=True;self.scheduler.maintenance.set();self._clock_deferred=False
            validation_wall,validation_elapsed=time.time(),self.elapsed()
            with self._clock_lock:validation_generation=self._clock_generation
            clock_bad=not self.scheduler.clock_ok or not self.dispatch.clock_ok() or self.dispatch.clock_fault.is_set()
            if clock_bad:self.dispatch.clock_fault.set()  # Keep clock repair fenced through IO.
            try:self.bridge.set_control(live=False,clock_ok=False)
            except Exception as exc:
                if self._fence_unconfirmed_sender():
                    raise AppError('native_recovery_pending','撤权未确认，执行器已停止，等待核对',503) from exc
            if clock_bad and not trust_clock:
                triggers=tuple(row.plan.get('trigger') for row in self.dispatch.raw_accounts.store.list_accounts() if row.plan and row.plan.get('enabled'))
                if self.elapsed()-self._stable_since<.5 or io_quiet(time.time(),triggers):
                    self._clock_deferred=True
                    raise AppError('native_clock_pending','等待稳定校时窗口；本轮不补交',503)
                before=time.time();started=self.elapsed()
                if not self.clock_validator():
                    raise AppError('native_clock_pending','校时尚未确认，将有限重试；本轮不补交',503)
                after=time.time();duration=self.elapsed()-started
                if abs((after-before)-duration)>.25 or self.elapsed()-self._stable_since<.5:
                    raise AppError('native_clock_pending','校时期间时间变化，稍后重试；本轮不补交',503)
                if io_quiet(after,triggers):
                    self._clock_deferred=True
                    raise AppError('native_clock_pending','等待稳定校时窗口；本轮不补交',503)
            count=self.scheduler.recover()
            if self.closed:raise AppError('native_paused','执行器正在关闭',409)
            if resume:
                with self.bridge.outbox.lock,self.bridge.outbox.transaction():self.bridge.outbox._meta('operator_paused','0')
                self.operator_paused=False
            def clock_error():
                duration=self.elapsed()-validation_elapsed
                changed=abs((time.time()-validation_wall)-duration)>.25
                if changed or validation_generation!=self._clock_generation or (clock_bad and not trust_clock and duration>5):
                    return AppError('native_clock_pending','恢复期间时钟变化，需重新核对；本轮不补交',503)
                if not self.scheduler.clock_ok or not self.dispatch.clock_ok():
                    return AppError('native_clock_pending','本机时间变化，正在核对；本轮不补交',503)
                return None

            def latch():
                self.scheduler.clock_ok=False;self.scheduler.maintenance.set()
                self.dispatch.clock_fault.set();self._stable_since=self.elapsed()

            # Keep management writes serialized, but never hold the observer
            # lock over Mongo/SQLite/control IO. It must see out-and-back steps.
            with self.bridge.outbox.lock:
                if clock_bad and not trust_clock:
                    triggers=tuple(row.plan.get('trigger') for row in self.dispatch.raw_accounts.store.list_accounts() if row.plan and row.plan.get('enabled'))
                    if io_quiet(time.time(),triggers):
                        self._clock_deferred=True;latch()
                        raise AppError('native_clock_pending','等待稳定校时窗口；本轮不补交',503)
                with self._clock_lock:
                    error=clock_error()
                    if error:latch()
                if error:raise error
                self.bridge.set_control(live=self.settings.allow_live and not self.operator_paused,clock_ok=True)
                with self._clock_lock:
                    error=clock_error()
                    if self.closed:error=AppError('native_paused','执行器正在关闭',409)
                    if clock_bad and not trust_clock and io_quiet(time.time(),triggers):
                        self._clock_deferred=True
                        error=AppError('native_clock_pending','等待稳定校时窗口；本轮不补交',503)
                    if error:latch()
                    else:self.dispatch.clock_fault.clear()
                if error:
                    try:self.bridge.set_control(live=False,clock_ok=False)
                    except Exception:self._fence_unconfirmed_sender()
                    raise error
            self.hard_error=self.recovery_message=None;self.last_recovery_at=time.time()
            self._clock_fault_active=False;self.recovery_budget.success()
            self.scheduler.refresh();self.scheduler.tick()
            self.notify_sync()
            return count
        except AppError as exc:
            self.bridge.outbox.failed=True
            if exc.code=='native_state_invalid':self.hard_error=str(exc)
            else:self.recovery_message=str(exc)
            raise
        except Exception:
            self.bridge.outbox.failed=True;self.recovery_message='执行连接暂时中断，正在恢复；现有计划保留'
            raise AppError('native_recovery_pending',self.recovery_message,503) from None
        finally:self.recovering=False;self.recovery_lock.release()

    def _start_clock_monitor(self):
        if self.clock_thread:return
        def observe():
            while not self.stopping.is_set():
                self.observe_clock(time.time(),self.elapsed())
                self.stopping.wait(.05)
        self.clock_thread=threading.Thread(target=observe,name='njfu-clock-monitor',daemon=True)
        self.clock_thread.start()

    def start_recovery(self):
        if self.thread:return
        self._start_clock_monitor()
        def loop():
            while not self.stopping.is_set():
                failed=(self.bridge.outbox.failed or not self.scheduler.clock_ok or not self.dispatch.clock_ok()
                        or self.dispatch.clock_fault.is_set()
                        or self.process.process is None or self.process.process.poll() is not None)
                now=self.elapsed()
                clock_bad=not self.scheduler.clock_ok or not self.dispatch.clock_ok() or self.dispatch.clock_fault.is_set()
                if clock_bad and not self._clock_fault_active:
                    self._clock_fault_active=True
                    offset=(time.time()-self.scheduler.anchor_wall)-(now-self.scheduler.anchor_mono)
                    self.last_clock_fault={'at':time.time(),'offset_ms':round(offset*1000,3)}
                    logging.getLogger(__name__).warning('Native clock protection: at=%s offset_ms=%s',
                        self.last_clock_fault['at'],self.last_clock_fault['offset_ms'])
                if self._retry.is_set():
                    self._retry.clear();self.recovery_budget.nudge(now)
                if failed and not self.hard_error and self.recovery_budget.ready(now):
                    try:self.recover_once()
                    except Exception:
                        now=self.elapsed()
                        if self._clock_deferred:self.recovery_budget.defer(now)
                        else:self.recovery_budget.failure(now)
                    else:self.recovery_budget.success()
                self.stopping.wait(.1)
        self.thread=threading.Thread(target=loop,name='njfu-executor-recovery',daemon=True);self.thread.start()
