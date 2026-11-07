"""Bounded, best-effort node diagnostics containing fixed fields only."""
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

PHASES = frozenset(('startup','loop','status','probe','capability','report','clock',
                    'prepare','ready','authorize','execute','result','worker'))
CODES = frozenset(('ok','unexpected_error','io_error','clock_invalid','probe_failed',
    'probe_started','probe_complete','probe_budget_exhausted','node_preflight_or_runtime_failed',
    'invalid_report','stale_report','release_window','captcha_required','release_schedule_mismatch',
    'node_not_ready','native_build_mismatch','report_not_accepted','unknown_task','binding_conflict',
    'credential_enrollment_mismatch','account_inactive','account_not_assigned','account_not_enrolled',
    'node_unavailable','node_restarted','clock_untrusted','clock_rollback','ledger_unavailable',
    'ledger_replaced','anchor_mismatch','authority_busy','retired_boot','stale_sequence',
    'rpc_result_unknown','rpc_response_invalid','rpc_queue_deadline','rpc_rejected',
    'operator_paused','not_selected_owner','cancellation_requested','authorization_deadline',
    'preparation_failed','fallback_exhausted','node_executor_restart_required'))


def error_code(error):
    # Only an exact match to a fixed protocol code may leave the exception.
    value = error.args[0] if isinstance(error, BaseException) and len(error.args) == 1 else None
    if type(value) is str and value in CODES:
        return value
    return 'io_error' if isinstance(error, OSError) else 'unexpected_error'


class NodeDiagnostics:
    LIMIT = 60000

    def __init__(self, directory=None, *, node_id='mac', clock=time.time):
        self.directory = None if directory is None else Path(directory).absolute()
        self.node = node_id if node_id in ('mac','ubuntu') else 'unknown'
        self.clock, self.lock = clock, threading.RLock()
        self.recent = {}
        self.last = None

    def record(self, phase, code, *, task=None, report=None):
        """Logging must never authorize, abort, repeat, or delay a business retry."""
        try:
            at = self.clock()
            if type(at) not in (int,float) or not math.isfinite(at): return
            row = {'at':at,'node':self.node,'phase':phase if phase in PHASES else 'loop',
                   'code':code if code in CODES else 'unexpected_error'}
            if task is not None:
                from .contracts import task_identity
                try: row['task_ref'] = task_identity(task)[0][:16]
                except (ValueError, TypeError, KeyError): pass
            if type(report) is dict and type(report.get('samples')) is list:
                samples = report['samples'][:10]
                row.update(sample_count=len(samples),success_count=sum(
                    type(x) is dict and x.get('public_response_valid') is True for x in samples))
            with self.lock:
                self.last = dict(row)
                key = (row['phase'],row['code'],row.get('task_ref'))
                old = self.recent.get(key)
                if old is not None and 0 <= at-old < 30: return
                if len(self.recent) >= 256: self.recent.clear()
                self.recent[key] = at
                if self.directory is not None: self._write(row)
        except Exception:
            # Filesystem failures are diagnostic failures, never execution policy.
            return

    def _write(self, row):
        folder = self.directory
        aliases = {Path('/tmp'):Path('/private/tmp'),Path('/var'):Path('/private/var')}
        for parent in (folder,*folder.parents):
            if parent.is_symlink() and not (parent in aliases and parent.resolve() == aliases[parent]):
                return
        folder.mkdir(mode=0o700,parents=False,exist_ok=True)
        info = folder.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700: return
        path, previous = folder/'events.jsonl', folder/'events.previous.jsonl'
        for item in (path,previous):
            if item.is_symlink(): return
            if item.exists():
                info=item.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600: return
        encoded=(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n').encode()
        if path.exists() and path.stat().st_size+len(encoded)>self.LIMIT:
            os.replace(path,previous)
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600:
                os.write(fd,encoded)
        finally:
            os.close(fd)
