"""Acquire short-lived time evidence only for one selected scheduled run."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

from local_app.date_exceptions import validate_date_exceptions
from local_app.models import ZONE, resolve_run, validate_plan
from .clock_pair import sample_clock_pair
from .protocol import clock_ns
from .trusted_clock import validate_evidence


def near_any_release(now, before, triggers):
    today = datetime.fromtimestamp(now, ZONE).date()
    for trigger in set(('07:00:00', *triggers)):
        for shift in (-1, 0, 1):
            try:
                day = today + timedelta(days=shift)
                due = datetime.fromisoformat(day.isoformat()+'T'+trigger).replace(tzinfo=ZONE).timestamp()
                if -before <= now-due <= 40:return True
            except (ValueError, TypeError, OverflowError):
                return True  # Unknown projected schedules must not admit a probe.
    return False


class PilotProofCoordinator:
    def __init__(self, selector, dispatch, fixture_command=None):
        self.selector = selector
        self.dispatch = dispatch
        self.bridge = dispatch.bridge
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.child = None
        self.thread = None
        self.generation = 0
        self.blockers = 0
        self.stop_unconfirmed = False
        self.fixture_command = fixture_command
        if fixture_command is not None:
            endpoint = self.dispatch.native.endpoint
            if (not endpoint.fixture_mode or not endpoint.origin.startswith('https://127.0.0.1:') or
                    not self.bridge.raw.name.startswith('njfu_test_')):
                raise ValueError('fixture probe requires isolated database and endpoint')

    @contextmanager
    def prevent_probe(self):
        with self.lock:
            self.blockers += 1
            self.generation += 1
            child = self.child
        try:
            if child is not None:self._stop_child(child)
            yield
        finally:
            with self.lock:self.blockers -= 1

    def _command(self):
        if self.fixture_command is not None:return list(self.fixture_command)
        if self.dispatch.native.endpoint.fixture_mode:
            raise ValueError('fixture probe command must be explicitly isolated')
        return [sys.executable, '-B', '-m', 'local_app.native.clock_probe']

    def _valid_target(self):
        selector = self.selector
        row = self.dispatch.store.get_account(selector.account_id)
        state = self.bridge.outbox.applied(selector.account_id)
        if not row or not state or not state.get('enabled') or not state.get('plan'):return False
        desired, projection = self.bridge.project(selector.account_id)
        if not desired.get('enabled') or projection != self.bridge.outbox.projection(selector.account_id):return False
        plan = validate_plan(row.get('plan'), self.dispatch.accounts.catalog)
        exceptions = validate_date_exceptions(row.get('date_exceptions', {}), self.dispatch.accounts.catalog)
        day = datetime.fromtimestamp(selector.due_at, ZONE).date()
        run = resolve_run(selector.account_id, row['revision'], plan, day, date_exceptions=exceptions)
        return bool(run and selector.verify(run, row, state))

    def _triggers(self):
        result = []
        for applied, projection in self.bridge.outbox.db.execute(
                "SELECT applied,projection FROM states WHERE key!='global'"):
            if applied is None:raise ValueError('missing state')
            state = json.loads(applied)
            if not isinstance(state, dict):raise ValueError('invalid state')
            if not state.get('enabled') or not state.get('plan'):continue
            value = json.loads(projection) if projection else None
            plan = value.get('plan') if isinstance(value, dict) else None
            trigger = plan.get('trigger') if isinstance(plan, dict) else None
            if not isinstance(trigger, str) or not trigger:raise ValueError('missing trigger')
            result.append(trigger)
        return result

    def _stop_child(self, child):
        try:
            if child.poll() is None:child.kill()
            child.wait(timeout=1)
            return child.poll() is not None
        except Exception:
            self.stop_unconfirmed = True
            return False

    def acquire(self):
        child = None
        started = False
        data = bytearray()
        deadline = time.monotonic()+2.5
        try:
            with self.bridge.outbox.lock:
                self.bridge.outbox.require_ready()
                now = time.time(); lead = self.selector.due_at-now
                master = self.bridge.outbox.applied('global') or {}
                if (self.stop.is_set() or not 45 <= lead <= 90 or not master.get('enabled') or
                        not master.get('plan') or not self._valid_target() or
                        near_any_release(now, 45, self._triggers())):
                    return False
                native = self.dispatch.native
                native_generation = (native.identity, native.epoch, native.session)
                with self.lock:
                    if self.stop.is_set() or self.stop_unconfirmed or self.blockers or self.child is not None:return False
                    generation = self.generation
                    child = subprocess.Popen(self._command(), cwd=Path(__file__).resolve().parents[2],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'})
                    self.child = child
                os.set_blocking(child.stdout.fileno(), False)
                while time.monotonic() < deadline and not started:
                    if self.stop.is_set():return False
                    ready, _, _ = select.select([child.stdout], [], [], .01)
                    if ready:
                        chunk = os.read(child.stdout.fileno(), 4096)
                        if chunk:data.extend(chunk)
                    if len(data) > 8192:return False
                    if b'\n' in data:
                        line, _, rest = data.partition(b'\n');data = bytearray(rest)
                        if line != b'STARTED':return False
                        started = True
                        break
                    if child.poll() is not None:return False
                if not started:return False
            while time.monotonic() < deadline:
                if self.stop.is_set():return False
                ready, _, _ = select.select([child.stdout], [], [], .01)
                if ready:
                    chunk = os.read(child.stdout.fileno(), 4096)
                    if chunk:data.extend(chunk)
                if len(data) > 8192:return False
                if child.poll() is not None:
                    try:
                        while chunk := os.read(child.stdout.fileno(), 4096):data.extend(chunk)
                    except BlockingIOError:pass
                    break
            if child.poll() != 0 or not data.startswith(b'PROOF ') or not data.endswith(b'\n'):
                return False
            evidence = json.loads(data[6:].decode('utf-8'))
            with self.bridge.outbox.lock:
                self.bridge.outbox.require_ready()
                pair = sample_clock_pair(time.time, clock_ns)
                now = time.time()
                master = self.bridge.outbox.applied('global') or {}
                with self.lock:
                    valid_generation = generation == self.generation and not self.stop.is_set()
                if (not valid_generation or not master.get('enabled') or not master.get('plan') or
                        self.selector.due_at-now < 45 or near_any_release(now, 45, self._triggers()) or
                        not self._valid_target() or native_generation !=
                        (native.identity, native.epoch, native.session) or
                        not validate_evidence(evidence, pair) or
                        evidence['expires_ns'] <= pair.deadline_ns(self.selector.due_at)+1_000_000_000):
                    return False
                self.dispatch.pilot_evidence = evidence
                self.dispatch.pilot_evidence_generation = native_generation
                return True
        except Exception:
            return False
        finally:
            if child is not None:
                self._stop_child(child)
                if child.stdout is not None:child.stdout.close()
                with self.lock:
                    if self.child is child:self.child = None

    def start(self):
        with self.lock:
            if self.thread is not None or self.stop.is_set():return
            def loop():
                attempts = 0;next_attempt = 0
                while not self.stop.is_set():
                    remaining = self.selector.due_at-time.time()
                    if remaining < 45:return
                    if remaining <= 90 and attempts < 2 and time.monotonic() >= next_attempt:
                        attempts += 1;next_attempt = time.monotonic()+15
                        if self.acquire():return
                    if attempts >= 2:return
                    self.stop.wait(min(1.0, max(.05, remaining-90)))
            self.thread = threading.Thread(target=loop, name='pilot-time-evidence', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        with self.lock:
            self.generation += 1
            child = self.child
        if child is not None:self._stop_child(child)
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():thread.join(timeout=4)
