"""Read-only conversion of existing plans into opaque server task specifications."""
from datetime import datetime, timedelta

from local_app.models import AppError, SegmentedPlan, ZONE, resolve_runs, validate_plan


class PublishError(RuntimeError):
    pass


def _control_reply(value, action):
    if (type(value) is not dict or value.get('operator_paused') is not (action == 'pause')
            or type(value.get('pending_owners')) is not int or not 0 <= value['pending_owners'] <= 2):
        raise PublishError('dual_control_unconfirmed')
    return value


def _tasks_on_dates(row, registration, catalog, release_dates):
    """Resolve content independently of the moving future-publish horizon."""
    from .contracts import validate_task
    if (type(registration) is not dict
            or row.get('credential_revision') != registration.get('credential_revision')):
        raise PublishError('credential_enrollment_mismatch')
    if row.get('plan') is None:
        return []
    plan = validate_plan(row['plan'], catalog)
    if plan.enabled and isinstance(plan, SegmentedPlan):
        raise PublishError('dual_segmented_unsupported')
    tasks = []
    for release_date in release_dates:
        runs = resolve_runs(row['_id'], row.get('revision'), plan, release_date,
                            row.get('date_exceptions'))
        if not runs:
            continue
        if len(runs) != 1 or any(run.operation_id is not None for run in runs):
            raise PublishError('dual_segmented_unsupported')
        run, = runs
        def minute(value):
            hour, minutes = value.split(':'); return int(hour)*60+int(minutes)
        tasks.append(validate_task({'schema':1, 'account_ref':registration.get('account_ref'),
            'day':run.target_date, 'kind':'S', 'operation':'-', 'revision':run.revision,
            'credential_revision':row['credential_revision'], 'release_at':run.due_at,
            'start_minute':minute(run.begin), 'end_minute':minute(run.end),
            'seat_priorities':list(run.seat_ids)}))
    return tasks


def same_task_content(previous, current):
    """A newer account CAS revision alone cannot revoke a bound execution."""
    from .contracts import validate_task
    previous, current = validate_task(previous), validate_task(current)
    return (previous['revision'] <= current['revision']
            and {k:v for k,v in previous.items() if k != 'revision'}
                == {k:v for k,v in current.items() if k != 'revision'})


def plan_tasks(accounts, registrations, catalog, *, now, days=7):
    """No credentials or school identity leave this function; never mutates a plan.

    A disabled/removed plan yields no desired task so the publisher can cancel
    outstanding ungranted tasks. The authority keeps every authorized owner.
    """
    if type(days) is not int or not 1 <= days <= 7:
        raise PublishError('invalid_publish_horizon')
    today = datetime.fromtimestamp(now, ZONE).date()
    tasks, seen = [], set()
    for row in accounts:
        if type(row) is not dict:
            raise PublishError('invalid_account_snapshot')
        account_id = row.get('_id')
        registration = registrations.get(account_id)
        if registration is None:
            continue
        # Enrollment and credential rotation are explicit administrative actions;
        # a publisher cannot silently declare a different secret revision ready.
        if (type(registration) is not dict
                or row.get('credential_revision') != registration.get('credential_revision')):
            raise PublishError('credential_enrollment_mismatch')
        ref = registration.get('account_ref')
        if ref in seen:
            raise PublishError('duplicate_account_reference')
        seen.add(ref)
        tasks.extend(task for task in _tasks_on_dates(row, registration, catalog,
            (today+timedelta(days=offset) for offset in range(days))) if task['release_at'] > now)
    return sorted(tasks, key=lambda task:(task['release_at'],task['account_ref']))


class PlanPublisherLoop:
    """Optional read-only plan watcher; the authority owns all execution state."""
    @classmethod
    def from_runtime(cls, runtime):
        path = runtime.settings.data_dir/'dual-node-publisher.json'
        if not path.exists() and not path.is_symlink():
            return None
        return cls(runtime, path)

    def __init__(self, runtime, path):
        import threading
        self.runtime, self.path = runtime, path
        self.stop = threading.Event()
        self.thread = None
        self.client = None
        self.client_identity = None
        self.result_bridge = None
        self.state = {'status':'not_started','last_synced_at':None}
        from .monitor import AlertJournal
        self.alert_journal = AlertJournal(path.parent/'dual-monitor-alerts.json')

    def _config(self):
        import json
        import os
        import stat
        path = self.path
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            raise PublishError('unsafe_publisher_config')
        from .contracts import parse_json
        value = parse_json(path.read_bytes())
        if (type(value) is not dict or set(value) != {'schema','enabled','ssh_config','ssh_host','registrations','days'}
                or type(value['schema']) is not int or value['schema'] != 1
                or type(value['enabled']) is not bool or type(value['registrations']) is not dict
                or type(value['days']) is not int or not 1 <= value['days'] <= 7):
            raise PublishError('invalid_publisher_config')
        return value

    def credential_sync_state(self, account_id, credential_revision):
        """Local enrollment comparison only; never claims node readiness."""
        try:
            registration = self._config()['registrations'].get(account_id)
            if not isinstance(registration, dict):
                return 'unknown'
            registered = registration.get('credential_revision')
            if type(registered) is not int or registered < 1 or type(credential_revision) is not int:
                return 'unknown'
            return 'matched' if registered == credential_revision else 'maintenance_required'
        except (OSError, ValueError, TypeError, KeyError, PublishError):
            return 'unknown'

    def sync_once(self):
        import time
        from .agent import RPCClient, all_tasks
        from .contracts import task_identity
        from .transport import SSHTransport
        from .result_bridge import ResultBridge
        from local_app.store import LocalStore
        config = self._config()
        if not config['enabled']:
            self.state = {'status':'disabled','last_synced_at':None}
            return
        identity = (config['ssh_config'],config['ssh_host'])
        if identity != self.client_identity:
            if self.client is not None: self.client.close()
            self.client = RPCClient(lambda:SSHTransport(*identity))
            self.client_identity = identity
        settings = getattr(self.runtime, 'settings', None)
        native = getattr(self.runtime, 'native', None)
        locally_paused = ((settings is not None and getattr(settings, 'allow_live', None) is False)
                          or (native is not None and getattr(native, 'operator_paused', None) is True))
        pause_error = False
        if locally_paused:
            try: _control_reply(self.client.call('control', action='pause'), 'pause')
            except Exception: pause_error = True
        # AccountView contains plans/revisions, never the encrypted password.
        rows = []
        for account in self.runtime.store.list_accounts():
            auth = getattr(self.runtime, 'auth', None)
            if auth is not None:
                try:
                    allowed = auth.is_allowed(account.student_id)
                    if type(allowed) is not bool:
                        raise PublishError('publisher_access_snapshot_unavailable')
                except Exception:
                    # An unreadable allowlist is not an operator revocation.
                    # Reconcile only a complete snapshot; otherwise a transient
                    # read failure would irreversibly cancel valid bindings.
                    self.state = {'status':'blocked','code':'publisher_access_snapshot_unavailable',
                                  'last_synced_at':self.state.get('last_synced_at'),
                                  'pending_changes':1,'local_operator_paused':locally_paused}
                    return
                if not allowed: continue
            rows.append({'_id':account.id,'revision':account.revision,
                         'credential_revision':account.credential_revision,'plan':account.plan,
                         'date_exceptions':account.date_exceptions})
        now = time.time()
        if isinstance(self.runtime.store, LocalStore):
            if self.result_bridge is None:
                self.result_bridge = ResultBridge(self.runtime.store, config['registrations'],
                                                  self.runtime.managed_execution)
            self.result_bridge.registrations = config['registrations']
        desired, planning_errors = [], 0
        references = {}
        for registration in config['registrations'].values():
            if type(registration) is dict and type(registration.get('account_ref')) is str:
                ref = registration['account_ref']; references[ref] = references.get(ref, 0) + 1
        for row in rows:
            registration = config['registrations'].get(row['_id'])
            if registration is None: continue
            try:
                if references.get(registration.get('account_ref'), 0) != 1:
                    raise PublishError('duplicate_account_reference')
                desired.extend(plan_tasks([row],config['registrations'],self.runtime.accounts.catalog,
                                          now=now,days=config['days']))
            except (PublishError, AppError, ValueError, TypeError, KeyError, AttributeError):
                # A stale credential or malformed plan invalidates this account,
                # not the healthy accounts or cancellation of its old tasks.
                planning_errors += 1
        # Only refs actually fenced on this runtime may be published. A missing
        # fence cannot silently enable the old and new submission paths together.
        for account_id in config['registrations']:
            if not self.runtime.managed_execution.is_managed(account_id):
                raise PublishError('publisher_account_not_fenced')
        known = all_tasks(self.client)
        desired_by_id = {task_identity(task)[0]:task for task in desired}
        rows_by_ref = {config['registrations'][row['_id']]['account_ref']:row for row in rows
            if row['_id'] in config['registrations']
            and type(config['registrations'][row['_id']]) is dict
            and type(config['registrations'][row['_id']].get('account_ref')) is str
            and references.get(config['registrations'][row['_id']].get('account_ref')) == 1}
        errors = planning_errors + int(pause_error)
        def local_id(task):
            if self.result_bridge is not None:
                return self.result_bridge.local_account(task)
            matches = [key for key, value in config['registrations'].items()
                       if value['account_ref'] == task['account_ref']]
            if len(matches) != 1: raise PublishError('publisher_scope_unavailable')
            return matches[0]
        blocked_accounts = set()
        for old in known:
            if old['release_at'] <= now: continue
            try:
                account_id = local_id(old)
                self.runtime.managed_execution.remember_auth_window(account_id, old['release_at'])
            except Exception:
                errors += 1
                try: blocked_accounts.add(local_id(old))
                except Exception: pass
        cancel_blocked = set()
        for old in known:
            if self.stop.is_set(): return
            old_id, _ = task_identity(old)
            row = rows_by_ref.get(old['account_ref'])
            unchanged = False
            if row is not None:
                try:
                    effective = _tasks_on_dates(row, config['registrations'][row['_id']],
                        self.runtime.accounts.catalog, (datetime.fromtimestamp(old['release_at'], ZONE).date(),))
                    unchanged = any(same_task_content(old, task) for task in effective)
                except (PublishError, AppError, ValueError, TypeError, KeyError, AttributeError):
                    errors += 1
            if unchanged:
                # Keep the exact server binding, including its original revision.
                # Past release is natural lifecycle progress, never user cancellation.
                if old_id in desired_by_id:
                    desired_by_id[old_id] = old
            else:
                try:
                    self.client.call('cancel',task=old,reason='operator_cancelled')
                except Exception:
                    errors += 1
                    cancel_blocked.add(old_id)
        for task in desired_by_id.values():
            if self.stop.is_set(): return
            try:
                account_id = local_id(task)
                if task_identity(task)[0] in cancel_blocked: raise PublishError('publisher_cancel_unconfirmed')
                if account_id in blocked_accounts: raise PublishError('publisher_auth_window_unavailable')
                self.runtime.managed_execution.remember_auth_window(account_id, task['release_at'])
                published = self.client.call('publish',task=task)
                if self.result_bridge is not None: self.result_bridge.record_scope(task, published)
                # A matching binding may already be terminal. Keep its durable
                # owner/grant intact, but never advertise it as a runnable plan.
                if (published.get('cancel_requested') is True or published.get('phase') in
                        ('cancel_requested','cancelled','expired_unissued','failed','not_sent','uncertain')):
                    raise PublishError('publisher_task_not_runnable')
            except Exception:
                errors += 1
        bridge_status = None
        if self.result_bridge is not None:
            bridge_status = self.result_bridge.sync(self.client, known)
            errors += bridge_status['blocked']
        # Observation is a publisher-only read; it never advances execution.
        # A downlevel server or malformed response leaves execution unconfirmed.
        from .monitor import projection
        observed = None
        monitor_code = None
        try:
            snapshot = self.client.call('health', deadline=time.time()+2)
            observed = projection(snapshot, config['registrations'])
            self.alert_journal.observe(snapshot)
        except Exception:
            monitor_code = 'monitor_unavailable'
        self.state = {'status':'blocked' if errors else 'synchronized',
                      'last_synced_at':self.state.get('last_synced_at') if errors else time.time(),
                      'future_tasks':len(desired),'pending_changes':errors,
                      'result_bridge':bridge_status, 'local_operator_paused':locally_paused,
                      'execution_health':observed,'monitor_code':monitor_code,
                      'monitor_alerts':self.alert_journal.status()}

    def control(self, action):
        """Synchronous admin barrier; never claims remote native cancellation."""
        from local_app.models import AppError
        from .agent import RPCClient
        from .transport import SSHTransport
        if action not in ('pause', 'resume'):
            raise AppError('invalid_dual_control', '双机控制操作无效', 400)
        try:
            managed = self.runtime.managed_execution.has_managed_scope()
            if type(managed) is not bool:
                raise AppError('dual_control_unconfirmed', '双机隔离状态尚未确认', 503)
            try: config = self._config()
            except FileNotFoundError:
                if not managed: return {'status':'inactive','pending_owners':0}
                if action == 'pause' and self.client is not None:
                    result = _control_reply(self.client.call('control', action=action), action)
                    if result['pending_owners']:
                        raise AppError('dual_pause_pending', '双机暂停已记录，等待执行主机确认', 409)
                    return result
                raise
            if not config['enabled'] and not managed:
                return {'status':'inactive','pending_owners':0}
            settings, native = getattr(self.runtime, 'settings', None), getattr(self.runtime, 'native', None)
            if action == 'resume' and (settings is None or getattr(settings, 'allow_live', None) is not True
                                      or native is None or getattr(native, 'operator_paused', None) is not False):
                raise AppError('dual_resume_blocked', '双机恢复未执行：预约总开关关闭或原执行器仍暂停', 409)
            if action == 'resume' and not config['enabled']:
                raise AppError('dual_resume_blocked', '双机配置已关闭，未恢复执行', 409)
            identity = (config['ssh_config'], config['ssh_host'])
            if self.client_identity != identity:
                if self.client is not None: self.client.close()
                self.client = RPCClient(lambda:SSHTransport(*identity))
                self.client_identity = identity
            result = _control_reply(self.client.call('control', action=action), action)
        except AppError: raise
        except Exception:
            raise AppError('dual_control_unconfirmed', '双机控制尚未取得服务器持久确认', 503) from None
        if action == 'pause' and result['pending_owners']:
            raise AppError('dual_pause_pending', '双机暂停已记录，等待执行主机确认', 409)
        return result

    def start(self):
        import threading
        if self.thread is not None: return
        self.thread = threading.Thread(target=self._run,name='dual-plan-publisher',daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.sync_once()
            except Exception:
                self.state = {'status':'blocked','code':'publisher_sync_failed',
                              'last_synced_at':self.state.get('last_synced_at')}
            self.stop.wait(2)

    def status(self):
        return dict(self.state)

    def close(self):
        self.stop.set()
        if self.thread is not None: self.thread.join(timeout=35)
        if self.client is not None: self.client.close()
