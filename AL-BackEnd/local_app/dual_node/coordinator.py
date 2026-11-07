"""One local server authority. Rollback or uncertain durability fails closed."""
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import statistics
import subprocess
import threading
import time
import uuid

from . import public_probe as probe
from .contracts import (DualNodeError, MODES, NODES, account_reference, canonical_bytes,
                        canonical_hash, exact, identifier, parse_json, require,
                        task_identity, timestamp, validate_task)
from .grants import GrantSigner

TERMINAL = ("completed", "failed", "not_sent", "uncertain", "cancelled", "expired_unissued")
REQUEST_CACHE_LIMIT = 2048
REQUEST_CACHE_SECONDS = 8 * 86400


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical_bytes(value)); stream.flush(); os.fsync(stream.fileno())


def _private(path, directory=False):
    info = path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
            and (directory or info.st_nlink == 1), "unsafe_authority_path")
    return info


def _read(path):
    _private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 1048576,
                "unsafe_authority_path")
        return parse_json(stream.read(1048577))


def _path(value):
    path = Path(value)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
            "unsafe_authority_path")
    return path


def _report(value, node, task, now, fixture, required_releases=()):
    required = {"schema", "probe_version", "kind", "node", "observed_at", "started_at", "finished_at",
                "endpoint", "measurement", "clock_jump_detected", "samples", "parameters",
                "authentication", "booking_readiness", "automatic_selection_authorized"}
    exact(value, required, {"clock_accuracy", "curl_version", "evidence_source"})
    require(type(value["schema"]) is int and value["schema"] == 1 and value["probe_version"] == probe.VERSION
            and value["kind"] == "sample" and value["node"] == node and value["endpoint"] == probe.ENDPOINT
            and value["measurement"] == "fresh_curl_public_get" and value["clock_jump_detected"] is False
            and value["authentication"] == "not_checked" and value["booking_readiness"] == "not_checked"
            and value["automatic_selection_authorized"] is False
            and value.get("clock_accuracy", "not_verified") == "not_verified", "invalid_report")
    if "evidence_source" in value:
        require(fixture and value["evidence_source"] == "synthetic_fixture", "fixture_evidence_forbidden")
    started, finished = timestamp(value["started_at"]), timestamp(value["finished_at"])
    timestamp(value["observed_at"])
    require(started <= finished and -5 <= now - finished <= 180, "stale_report")
    parameters = exact(value["parameters"], {"sample_count", "interval_seconds", "connect_timeout_seconds",
                       "request_timeout_seconds", "quiet_before_seconds", "quiet_after_seconds", "release_times"})
    require(type(parameters["sample_count"]) is int and 3 <= parameters["sample_count"] <= 10
            and type(parameters["interval_seconds"]) is int and 10 <= parameters["interval_seconds"] <= 60,
            "invalid_report")
    for key, expected in (("connect_timeout_seconds", 1), ("request_timeout_seconds", 3),
                          ("quiet_before_seconds", 300), ("quiet_after_seconds", 300)):
        require(type(parameters[key]) is int and parameters[key] == expected, "invalid_report")
    releases = parameters["release_times"]
    require(type(releases) is list and releases and len(releases) <= 1440
            and all(type(v) is str and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]", v)
                    for v in releases) and releases == sorted(set(releases)) and "07:00:00" in releases,
            "invalid_report")
    due_clock = datetime.fromtimestamp(task["release_at"], probe.ZONE).strftime("%H:%M:%S")
    require(due_clock in releases and (fixture or set(required_releases) <= set(releases)),
            "release_schedule_mismatch")
    samples = value["samples"]
    require(type(samples) is list and len(samples) == parameters["sample_count"], "invalid_report")
    times, flags, previous = [], [], None
    for sample in samples:
        exact(sample, {"at", "finished_at", "public_response_valid"},
              {"metrics_ms", "http_status", "public_captcha_flag", "error", "curl_exit_code"})
        at, end = timestamp(sample["at"]), timestamp(sample["finished_at"])
        require(started <= at <= end <= finished and end - at <= probe.GUARD_BUDGET, "invalid_report")
        require(previous is None or at - previous >= parameters["interval_seconds"] - .05, "invalid_report")
        previous = at
        require(fixture or not probe.guarded(at, releases), "release_window")
        require(type(sample["public_response_valid"]) is bool, "invalid_report")
        if "public_captcha_flag" in sample:
            require(type(sample["public_captcha_flag"]) is str
                    and sample["public_captcha_flag"] in ("0", "1", "2"), "invalid_report")
            flags.append(sample["public_captcha_flag"])
        if "http_status" in sample:
            require(type(sample["http_status"]) is int and 100 <= sample["http_status"] <= 599, "invalid_report")
        if "metrics_ms" in sample:
            metrics = exact(sample["metrics_ms"], {"dns", "tcp", "tls", "ttfb", "total"})
            require(all(type(x) in (int, float) and 0 <= x <= 4000 and math.isfinite(x) for x in metrics.values())
                    and 0 < metrics["total"] <= 4000
                    and metrics["dns"] + metrics["tcp"] + metrics["tls"] <= metrics["ttfb"] + .003
                    and metrics["ttfb"] <= metrics["total"] <= (end - at) * 1000 + 3, "invalid_report")
        if sample["public_response_valid"]:
            require(sample.get("http_status") == 200 and "metrics_ms" in sample
                    and "error" not in sample and "curl_exit_code" not in sample
                    and sample.get("public_captcha_flag") in ("0", "1", "2"), "invalid_report")
            require(-5 <= now - end <= 180, "stale_report")
            times.append(sample["metrics_ms"]["total"])
        else:
            error = sample.get("error")
            require(error in ("curl_failed", "unexpected_http_status", "invalid_metrics",
                              "invalid_public_response", "command_timeout", "output_limit"), "invalid_report")
            if error == "curl_failed":
                code = sample.get("curl_exit_code")
                require(type(code) is int and -255 <= code <= 255 and code != 0, "invalid_report")
            else:
                require("curl_exit_code" not in sample, "invalid_report")
    require(-5 <= now - end <= 180, "stale_report")
    require(finished - end <= probe.GUARD_BUDGET and len(times) >= 3
            and len(times) / len(samples) >= .75, "invalid_report")
    require(flags and set(flags) == {"0"}, "captcha_required")
    return {"median_ms": statistics.median(times), "max_ms": max(times),
            "success_fraction": len(times) / len(samples), "evidence_sha256": canonical_hash(value)}


class Coordinator:
    @classmethod
    def initialize(cls, root, signing_key=None, *, mode="fixture", clock=None, time_health=None):
        root = _path(root)
        require(mode in MODES and (mode == "fixture" or clock is None), "invalid_mode")
        require(not root.exists(), "initialize_refused")
        anchor = root.parent / ("." + root.name + ".highwater.json")
        require(not anchor.exists() and not anchor.is_symlink(), "initialize_refused")
        require(mode == "fixture" or signing_key is not None, "production_key_required")
        root.mkdir(mode=0o700)
        if signing_key is None:
            signing_key = root / "signing_key"
            subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(signing_key)],
                           check=True, capture_output=True, timeout=10)
        signer = GrantSigner(signing_key, mode=mode, authority_id=uuid.uuid4().hex)
        _write_new(root / "owner.lock", {})
        fd = os.open(root / "state.db", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        identity = {"schema": 1, "authority_id": signer.authority_id, "mode": mode,
                    "public_key": signer.public_key, "signing_key": str(Path(signing_key).absolute()),
                    "anchor": str(anchor), "anchor_id": uuid.uuid4().hex,
                    "directory": [root.stat().st_dev, root.stat().st_ino],
                    "files": {name: [root.joinpath(name).stat().st_dev, root.joinpath(name).stat().st_ino]
                              for name in ("owner.lock", "state.db")}}
        _write_new(root / "identity.json", identity)
        db = sqlite3.connect(root / "state.db", isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE"); db.execute("PRAGMA synchronous=EXTRA")
            db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE enrollments(account_ref TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE tasks(task_id TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE nodes(node_id TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE requests(request_key TEXT PRIMARY KEY,value TEXT NOT NULL);")
            db.execute("INSERT INTO enrollments VALUES(?,?)", ("__control__",
                canonical_bytes({"operator_paused": mode == "production"}).decode()))
            state_hash = cls._digest(db)
            db.executemany("INSERT INTO meta VALUES(?,?)", [
                ("identity", canonical_hash(identity)), ("sequence", "0"), ("state_hash", state_hash)])
            db.commit()
            _write_new(anchor, {"schema": 1, "anchor_id": identity["anchor_id"], "sequence": 0,
                       "state_hash": state_hash, "last_wall": 0.0})
            _fsync_directory(root); _fsync_directory(root.parent)
        finally: db.close()
        return cls(root, signing_key=signing_key, mode=mode, clock=clock, time_health=time_health)

    def __init__(self, root, signing_key=None, *, mode=None, clock=None, time_health=None):
        self.db = None; self.lock_fd = None; self._mutex = threading.RLock(); self._active = False
        self._fault = lambda _stage: None
        try:
            self.root = _path(root); _private(self.root, True)
            self.identity = _read(self.root / "identity.json")
            self.mode = self.identity["mode"]
            require(self.mode in MODES and (mode is None or self.mode == mode), "wrong_authority_mode")
            require(self.mode == "fixture" or clock is None, "production_clock_injection")
            self.clock = clock or time.time
            self.time_health = time_health
            self.authority_id, self.public_key = self.identity["authority_id"], self.identity["public_key"]
            identifier(self.authority_id, size=32)
            self.anchor = _path(self.identity["anchor"])
            require(self.anchor == self.root.parent / ("." + self.root.name + ".highwater.json"), "anchor_mismatch")
            self.signer = GrantSigner(signing_key or self.identity["signing_key"],
                                      mode=self.mode, authority_id=self.authority_id)
            require(self.signer.public_key == self.public_key, "wrong_signing_key")
            for name in ("owner.lock", "state.db"): _private(self.root / name)
            self.lock_fd = os.open(self.root / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
            self.db = sqlite3.connect((self.root / "state.db").as_uri() + "?mode=rw", uri=True,
                                     isolation_level=None, timeout=1, check_same_thread=False)
            self.db.execute("PRAGMA synchronous=EXTRA")
            self._locked_check()
        except DualNodeError:
            self.close(); raise
        except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError):
            self.close(); raise DualNodeError("ledger_unavailable") from None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def close(self):
        if self.db is not None: self.db.close(); self.db = None
        if self.lock_fd is not None: os.close(self.lock_fd); self.lock_fd = None

    @staticmethod
    def _digest(db):
        digest = hashlib.sha256()
        for table in ("enrollments", "tasks", "nodes", "requests"):
            digest.update(table.encode() + b"\0")
            for row in db.execute("SELECT * FROM " + table + " ORDER BY 1"):
                digest.update(canonical_bytes(list(row)) + b"\n")
        return digest.hexdigest()

    def _check(self):
        _private(self.root, True)
        require([self.root.stat().st_dev, self.root.stat().st_ino] == self.identity["directory"], "ledger_replaced")
        require(_read(self.root / "identity.json") == self.identity, "ledger_replaced")
        for name, expected in self.identity["files"].items():
            info = _private(self.root / name)
            require([info.st_dev, info.st_ino] == expected, "ledger_replaced")
        require(self.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
                and self.db.execute("PRAGMA synchronous").fetchone()[0] == 3
                and self.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "ledger_unavailable")
        meta = dict(self.db.execute("SELECT key,value FROM meta"))
        anchor = _read(self.anchor)
        require(meta["identity"] == canonical_hash(self.identity)
                and anchor["anchor_id"] == self.identity["anchor_id"]
                and int(meta["sequence"]) == anchor["sequence"]
                and meta["state_hash"] == anchor["state_hash"] == self._digest(self.db), "anchor_mismatch")
        timestamp(anchor["last_wall"])
        return anchor

    def _flock(self):
        deadline = time.monotonic() + 2
        while True:
            try: fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return
            except BlockingIOError:
                require(time.monotonic() < deadline, "authority_busy")
                time.sleep(.01)

    def _locked_check(self):
        self._flock()
        try: self._check()
        finally: fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def _anchor_write(self, value):
        temporary = self.anchor.parent / (self.anchor.name + "." + uuid.uuid4().hex)
        try:
            _write_new(temporary, value)
            os.replace(temporary, self.anchor); _fsync_directory(self.anchor.parent)
        finally:
            if temporary.exists(): temporary.unlink()

    def _observe(self):
        now = timestamp(self.clock())
        require(now >= self._call_anchor["last_wall"], "clock_rollback")
        self._call_anchor = {**self._call_anchor, "last_wall": now}
        self._anchor_write(self._call_anchor)
        self._now = now
        return now

    def _call(self, function):
        with self._mutex:
            if self._active:
                return function(self._now)
            require(self.db is not None and self.lock_fd is not None, "authority_closed")
            self._flock()
            try:
                # SQLite may retain cached pages after out-of-band file damage.
                self.db.close(); self.db = None
                self.db = sqlite3.connect((self.root / "state.db").as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None, timeout=1, check_same_thread=False)
                self.db.execute("PRAGMA synchronous=EXTRA")
                anchor = self._check()
                now = timestamp(self.clock())
                require(now >= anchor["last_wall"], "clock_rollback")
                # Independent clock observations survive every refused transaction.
                anchor = {**anchor, "last_wall": now}; self._anchor_write(anchor)
                self.db.execute("BEGIN IMMEDIATE")
                self._active, self._now, self._call_anchor, self._commit_deadline = True, now, anchor, None
                changes = self.db.total_changes
                result = function(now)
                if self._commit_deadline is not None:
                    require(self._observe() <= self._commit_deadline, "authorization_deadline")
                if self.db.total_changes == changes:
                    self.db.commit()
                    return result
                state_hash = self._digest(self.db); sequence = anchor["sequence"] + 1
                self.db.execute("UPDATE meta SET value=? WHERE key='sequence'", (str(sequence),))
                self.db.execute("UPDATE meta SET value=? WHERE key='state_hash'", (state_hash,))
                self._anchor_write({**self._call_anchor, "sequence": sequence, "state_hash": state_hash})
                self._fault("anchor_prepared")
                self.db.commit()
                return result
            except DualNodeError:
                if self.db is not None: self.db.rollback()
                raise
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
                if self.db is not None: self.db.rollback()
                raise DualNodeError("ledger_unavailable") from None
            finally:
                self._active = False
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def _get(self, table, key):
        row = self.db.execute("SELECT value FROM " + table + " WHERE " +
                              {"tasks": "task_id", "nodes": "node_id", "enrollments": "account_ref",
                               "requests": "request_key"}[table] + "=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, table, key, value):
        self.db.execute("INSERT OR REPLACE INTO " + table + " VALUES(?,?)",
                        (key, canonical_bytes(value).decode()))

    def _enrollment(self, task, active=True):
        enrollment = self._get("enrollments", task["account_ref"])
        require(enrollment is not None and enrollment["credential_revision"] == task["credential_revision"],
                "account_not_enrolled")
        if active: require(enrollment["active"], "account_inactive")
        return enrollment

    def enroll(self, account_ref, *, credential_revision=1, nodes=NODES, active=False,
               preferred_node="mac", native_hashes=None):
        def operation(now):
            account_reference(account_ref)
            require(type(credential_revision) is int and credential_revision >= 1
                    and type(active) is bool and type(nodes) in (list, tuple) and nodes
                    and len(set(nodes)) == len(nodes) and all(n in NODES for n in nodes)
                    and preferred_node in NODES, "invalid_enrollment")
            require(self.mode == "fixture" or not active, "activation_required")
            hashes = native_hashes or {}
            require(type(hashes) is dict and set(hashes) <= set(nodes), "invalid_enrollment")
            for value in hashes.values(): identifier(value, size=64)
            existing = self._get("enrollments", account_ref)
            record = {"account_ref": account_ref, "credential_revision": credential_revision,
                      "nodes": list(nodes), "active": active, "preferred_node": preferred_node,
                      "native_hashes": hashes, "drain_ack": None}
            require(existing is None or existing == record, "enrollment_conflict")
            self._put("enrollments", account_ref, record)
            return record
        return self._call(operation)

    def activate(self, account_ref, *, drain_ack=None):
        def operation(now):
            enrollment = self._get("enrollments", account_reference(account_ref))
            require(enrollment is not None, "account_not_enrolled")
            if self.mode == "production":
                exact(drain_ack, {"schema", "account_ref", "legacy_drained", "state_ack_sha256"})
                require(drain_ack["schema"] == 1 and drain_ack["account_ref"] == account_ref
                        and drain_ack["legacy_drained"] is True
                        and set(enrollment["native_hashes"]) == set(enrollment["nodes"]), "drain_ack_required")
                identifier(drain_ack["state_ack_sha256"], size=64)
            enrollment.update(active=True, drain_ack=drain_ack)
            self._put("enrollments", account_ref, enrollment)
            return enrollment
        return self._call(operation)

    def _task(self, task):
        task = validate_task(task); task_id, binding = task_identity(task)
        row = self._get("tasks", task_id)
        require(row is not None, "unknown_task")
        require(row["binding_sha256"] == binding, "binding_conflict")
        return task, row

    def _status(self, row):
        return {**{key: row.get(key) for key in ("task_id", "binding_sha256", "phase", "owner", "grant",
                                            "cancel_requested", "reason", "result")},
                "mode": self.mode, "authority_id": self.authority_id,
                "mirror_ack": row.get("mirror_ack") is True, "operator_paused": self._paused()}

    def _expire_unissued(self, row, now):
        if (row["grant"] is None and row["phase"] in ("collecting", "selected", "ready")
                and now > row["task"]["release_at"]):
            row.update(phase="expired_unissued", reason="release_passed_without_grant", ready=None)
            self._put("tasks", row["task_id"], row)
        return row

    def _paused(self):
        control = self._get("enrollments", "__control__")
        if control is None: return self.mode == "production"
        require(type(control.get("operator_paused")) is bool, "invalid_operator_control")
        return control["operator_paused"]

    def control(self, *, action):
        def operation(now):
            require(action in ("pause", "resume"), "invalid_control")
            if action == "resume": self._clock_health(now)
            paused = action == "pause"
            self._put("enrollments", "__control__", {"operator_paused": paused})
            pending = set()
            for key, value in list(self.db.execute("SELECT task_id,value FROM tasks")):
                row = json.loads(value)
                if row["grant"] is not None and row["phase"] not in TERMINAL:
                    if paused:
                        row.update(cancel_requested=True, phase="cancel_requested", reason="operator_pause")
                        self._put("tasks", key, row)
                    if row.get("cancel_requested"): pending.add(row["owner"])
            return {"operator_paused": paused, "pending_owners": len(pending)}
        return self._call(operation)

    def publish(self, task):
        def operation(now):
            value = validate_task(task); task_id, binding = task_identity(value)
            self._enrollment(value, active=False)
            require(now - 30 <= value["release_at"] <= now + 8 * 86400, "invalid_publish_window")
            old = self._get("tasks", task_id)
            if old is not None:
                if old["binding_sha256"] == binding: return self._status(old)
                require(old["grant"] is None and value["revision"] > old["task"]["revision"], "binding_conflict")
            row = {"task_id": task_id, "binding_sha256": binding, "task": value, "phase": "collecting",
                   "owner": None, "grant": None, "reports": {}, "ready": None, "snapshot": {},
                   "excluded": [], "selected_boot": None, "cancel_requested": False, "reason": "published"}
            self._put("tasks", task_id, row)
            return self._status(row)
        return self._call(operation)

    def tasks(self, node_id=None, *, after=None, limit=32, include_unmirrored=False):
        def operation(now):
            require(node_id is None or node_id in NODES, "unknown_node")
            if after is not None: identifier(after, size=64)
            require(type(limit) is int and 1 <= limit <= 32, "invalid_page")
            result, quiet = [], set()
            for task_id, value in list(self.db.execute("SELECT task_id,value FROM tasks ORDER BY task_id")):
                row = self._expire_unissued(json.loads(value), now)
                enrollment = self._enrollment(row["task"], active=False)
                unresolved = row["grant"] is not None and row["phase"] in ("authorized", "cancel_requested", "uncertain")
                unmirrored = (include_unmirrored and row["grant"] is not None
                              and row["phase"] in ("completed", "failed", "not_sent", "uncertain")
                              and row.get("mirror_ack") is not True)
                visible = unresolved or unmirrored or row["task"]["release_at"] + 305 >= now
                if visible:
                    quiet.add(datetime.fromtimestamp(row["task"]["release_at"], probe.ZONE).strftime("%H:%M:%S"))
                if (visible and (after is None or task_id > after)
                        and (node_id is None or node_id in enrollment["nodes"])):
                    if len(result) <= limit: result.append((task_id, row["task"]))
            return {"tasks": [value for _, value in result[:limit]],
                    "next_cursor": result[limit - 1][0] if len(result) > limit else None,
                    "quiet_release_times": sorted(quiet | {"07:00:00"}), "operator_paused": self._paused()}
        return self._call(operation)

    def health(self):
        """Publisher observation only: never select, expire, cancel or grant."""
        def operation(now):
            nodes=[]
            for node in NODES:
                presence=self._get('nodes',node)
                age=now-presence['received_at'] if presence is not None else None
                nodes.append({'node':node,'online':age is not None and 0<=age<=45,
                              'heartbeat_age':round(age,3) if age is not None and age>=0 else None})
            rows=[];paused=self._paused()
            for raw, in self.db.execute('SELECT value FROM tasks ORDER BY task_id'):
                row=json.loads(raw);task=row['task'];delta=task['release_at']-now
                if delta < -86400 or delta > 48*3600:continue
                require(len(rows)<128,'health_limit')
                online=[];eligible=[]
                enrollment=self._get('enrollments',task['account_ref'])
                if enrollment is not None:
                    for node in enrollment['nodes']:
                        try:
                            self._presence(node,now);online.append(node)
                            evidence=row['reports'].get(node)
                            require(evidence is not None,'report_missing')
                            self._presence(node,now,evidence['boot_id'])
                            cap=evidence['capability']
                            require(cap['credential_revision']==enrollment['credential_revision']
                                    and cap['expires_at']>=now and node not in row['excluded'],'node_not_ready')
                            expected=enrollment['native_hashes'].get(node)
                            require(expected is None or expected==cap['native_binary_sha256'],'native_build_mismatch')
                            _report(evidence['report'],node,task,now,self.mode=='fixture',self._quiet_releases(evidence['report']))
                            eligible.append(node)
                        except DualNodeError:pass
                phase=row['phase'];state='blocked';code='account_unavailable';ready=False
                if phase=='completed':state,code='completed','completed'
                elif phase=='failed':state,code='failed','failed'
                elif phase in ('cancelled','cancel_requested'):state,code='cancelled','cancelled'
                elif phase=='not_sent':state,code='not_sent','not_sent'
                elif phase=='uncertain':state,code='uncertain','execution_uncertain'
                elif row['grant'] is not None:state,code='authorized','authorization_issued'
                elif paused:state,code='paused','operator_paused'
                elif row['cancel_requested']:state,code='cancelled','cancelled'
                elif phase=='expired_unissued' or delta<0:state,code='missed','release_without_authorization'
                elif enrollment is None or enrollment['active'] is not True or enrollment['credential_revision']!=task['credential_revision']:pass
                elif delta>450:state,code='waiting','probe_not_due'
                elif not online:state,code='blocked','nodes_unavailable'
                elif phase in ('selected','ready'):
                    try:self._presence(row['owner'],now,row['selected_boot'])
                    except DualNodeError:code='owner_unavailable'
                    else:
                        evidence=row.get('ready')
                        if phase=='ready' and evidence is not None and -5<=now-evidence['checked_at']<=120 and now<=evidence['expires_at']:
                            try:self._clock_health(now)
                            except DualNodeError:code='clock_untrusted'
                            else:state,code,ready='ready','owner_prepared',True
                        elif delta<=15:code='preparation_not_ready'
                        else:state,code='selected','owner_selected'
                elif eligible:state,code='eligible','probe_eligible'
                elif delta<=305:code='probe_report_missing'
                else:state,code='probing','probe_in_progress'
                rows.append({'task_id':row['task_id'],'account_ref':task['account_ref'],
                             'release_at':task['release_at'],'phase':phase,'state':state,'code':code,
                             'ready':ready,'owner':row['owner'],'online_nodes':len(online),
                             'eligible_nodes':len(eligible),'degraded':len(online)<2 or len(eligible)==1})
            result={'schema':1,'checked_at':now,'operator_paused':paused,'nodes':nodes,'tasks':rows}
            require(len(canonical_bytes(result))<=60000,'health_limit')
            return result
        return self._call(operation)

    def heartbeat(self, node_id, *, boot_id, seq):
        def operation(now):
            require(node_id in NODES, "unknown_node"); identifier(boot_id)
            require(type(seq) is int and 0 <= seq <= 2**63 - 1, "invalid_sequence")
            old = self._get("nodes", node_id)
            retired = list(old.get("retired_boots", [])) if old is not None else []
            require(boot_id not in retired, "retired_boot")
            if old is not None and old["boot_id"] == boot_id:
                require(seq > old["seq"], "stale_sequence")
            elif old is not None:
                retired.append(old["boot_id"])
            row = {"boot_id": boot_id, "seq": seq, "received_at": now, "retired_boots": retired}
            self._put("nodes", node_id, row)
            return {"accepted": True, "server_wall": now}
        return self._call(operation)

    def _presence(self, node_id, now, boot=None):
        require(node_id in NODES, "unknown_node")
        presence = self._get("nodes", node_id)
        require(presence is not None and 0 <= now - presence["received_at"] <= 45, "node_unavailable")
        if boot is not None: require(presence["boot_id"] == boot, "node_restarted")
        return presence

    def _capability(self, value, task, node, now):
        exact(value, {"schema", "checked_at", "expires_at", "credential_revision", "credential_present",
                      "native_ready", "native_binary_sha256", "worker_count", "clock_checked", "boot_id", "seq"})
        presence = self._presence(node, now, value["boot_id"])
        require(type(value["seq"]) is int and presence["seq"] <= value["seq"] <= 2**63 - 1, "stale_sequence")
        require(type(value["schema"]) is int and value["schema"] == 1
                and value["credential_present"] is True and value["native_ready"] is True
                and value["clock_checked"] is True and type(value["worker_count"]) is int
                and value["worker_count"] == 20
                and type(value["credential_revision"]) is int
                and value["credential_revision"] == task["credential_revision"]
                and -5 <= now - timestamp(value["checked_at"]) <= 45
                and now <= timestamp(value["expires_at"]) <= value["checked_at"] + 120, "node_not_ready")
        identifier(value["native_binary_sha256"], size=64)
        presence["seq"] = value["seq"]; self._put("nodes", node, presence)
        return presence

    def _quiet_releases(self, report):
        started, finished = timestamp(report.get("started_at")), timestamp(report.get("finished_at"))
        releases = set()
        for raw, in self.db.execute("SELECT value FROM tasks"):
            row = json.loads(raw)
            due = row["task"]["release_at"]
            if started <= due + 300 and finished + probe.GUARD_BUDGET >= due - 300:
                releases.add(datetime.fromtimestamp(due, probe.ZONE).strftime("%H:%M:%S"))
        return releases

    def report(self, node_id, task, report, readiness):
        def operation(now):
            value, row = self._task(task); enrollment = self._enrollment(value)
            require(node_id in enrollment["nodes"] and row["phase"] == "collecting", "report_not_accepted")
            _report(report, node_id, value, now, self.mode == "fixture", self._quiet_releases(report))
            presence = self._capability(readiness, value, node_id, now)
            expected = enrollment["native_hashes"].get(node_id)
            require(expected is None or readiness["native_binary_sha256"] == expected, "native_build_mismatch")
            row["reports"][node_id] = {"report": report, "capability": readiness, "boot_id": presence["boot_id"],
                                       "received_at": now}
            self._put("tasks", row["task_id"], row)
            return self._status(row)
        return self._call(operation)

    def _select(self, row, now):
        row = self._expire_unissued(row, now)
        if (row['grant'] is None and row['phase'] in ('selected', 'ready')
                and not row['cancel_requested'] and not self._paused()):
            try:
                self._presence(row['owner'], now, row['selected_boot'])
            except DualNodeError as error:
                if error.code not in ('node_unavailable', 'node_restarted'):
                    raise
                # This transaction both revokes the old owner and selects its
                # frozen standby. A racing authorization either commits first
                # (then this branch cannot run) or the old owner is rejected.
                return self._fallback(row, now, reason='selected_owner_unavailable')
        if row["grant"] is not None or row["phase"] != "collecting":
            return row
        task = row["task"]; enrollment = self._enrollment(task)
        if self.mode == "production" and task["release_at"] - now > 305:
            return row
        if now > task["release_at"] - 3:
            row.update(phase="expired_unissued", reason="selection_deadline")
            self._put("tasks", row["task_id"], row); return row
        eligible = {}
        for node, data in row["reports"].items():
            try:
                require(node in enrollment["nodes"] and node not in row["excluded"], "node_excluded")
                self._presence(node, now, data["boot_id"])
                require(data["capability"]["credential_revision"] == enrollment["credential_revision"]
                        and data["capability"]["expires_at"] >= now, "node_not_ready")
                expected = enrollment["native_hashes"].get(node)
                require(expected is None or expected == data["capability"]["native_binary_sha256"], "native_build_mismatch")
                summary = _report(data["report"], node, task, now, self.mode == "fixture",
                                  self._quiet_releases(data["report"]))
                eligible[node] = {**data, "summary": summary}
            except DualNodeError:
                continue
        if not eligible: return row
        preferred = enrollment["preferred_node"]
        reason = "single_healthy_node"
        if len(eligible) == 2:
            reports = [eligible[n]["report"] for n in NODES]
            comparison = probe.compare_reports(reports, now=now)
            if comparison.get("reason") not in ("higher_observed_success_fraction", "lower_observed_latency",
                                               "no_clear_network_preference"):
                row["reason"] = comparison.get("reason", "incomparable_reports")
                self._put("tasks", row["task_id"], row); return row
            preferred = comparison.get("advisory_candidate") or preferred
            reason = comparison["reason"]
        chosen = preferred if preferred in eligible else next(iter(eligible))
        row.update(owner=chosen, selected_boot=eligible[chosen]["boot_id"], phase="selected",
                   snapshot=eligible, reason=reason)
        self._put("tasks", row["task_id"], row)
        return row

    def select(self, task):
        def operation(now):
            _, row = self._task(task)
            return self._status(self._select(row, now))
        return self._call(operation)

    def status(self, task):
        return self.select(task)

    def inspect_status(self, task):
        return self._call(lambda now: self._status(self._expire_unissued(self._task(task)[1], now)))

    def node_status(self, node_id, task):
        def operation(now):
            value, row = self._task(task)
            require(node_id in self._enrollment(value, active=False)["nodes"], "account_not_assigned")
            result = self._status(self._select(row, now))
            if result["owner"] != node_id: result["grant"] = None
            return result
        return self._call(operation)

    def ready(self, node_id, task, readiness):
        def operation(now):
            require(not self._paused(), "operator_paused")
            value, row = self._task(task); enrollment = self._enrollment(value)
            require(row["grant"] is None and row["phase"] in ("selected", "ready")
                    and row["owner"] == node_id and not row["cancel_requested"], "not_selected_owner")
            presence = self._presence(node_id, now, row["selected_boot"])
            required = {"schema", "task_id", "binding_sha256", "node_id", "credential_revision",
                        "checked_at", "expires_at", "authenticated", "identity_matches", "payload_protocol",
                        "native_identity", "native_binary_sha256", "platform", "durability_profile",
                        "worker_count", "clock_checked"}
            exact(readiness, required, {"boot_id", "seq"})
            require(type(readiness["schema"]) is int and readiness["schema"] == 1
                    and readiness["task_id"] == row["task_id"]
                    and readiness["binding_sha256"] == row["binding_sha256"] and readiness["node_id"] == node_id
                    and type(readiness["credential_revision"]) is int
                    and readiness["credential_revision"] == value["credential_revision"]
                    and readiness["authenticated"] is True and readiness["identity_matches"] is True
                    and readiness["payload_protocol"] == "campus-v1" and readiness["clock_checked"] is True
                    and type(readiness["worker_count"]) is int and readiness["worker_count"] == 20
                    and -5 <= now - timestamp(readiness["checked_at"]) <= 120
                    and now <= timestamp(readiness["expires_at"]) <= readiness["checked_at"] + 120,
                    "invalid_prepared_readiness")
            identifier(readiness["native_binary_sha256"], size=64)
            require(len(canonical_bytes(readiness["native_identity"])) <= 2048
                    and type(readiness["platform"]) is str and len(readiness["platform"]) <= 80
                    and type(readiness["durability_profile"]) is str and len(readiness["durability_profile"]) <= 80,
                    "invalid_prepared_readiness")
            if "boot_id" in readiness:
                require(readiness["boot_id"] == row["selected_boot"], "node_restarted")
            if "seq" in readiness:
                require(type(readiness["seq"]) is int and presence["seq"] <= readiness["seq"] <= 2**63 - 1,
                        "stale_sequence")
                presence["seq"] = readiness["seq"]; self._put("nodes", node_id, presence)
            expected = enrollment["native_hashes"].get(node_id)
            require((self.mode == "fixture" or expected is not None)
                    and (expected is None or expected == readiness["native_binary_sha256"]), "native_build_mismatch")
            require(readiness["native_binary_sha256"] == row["snapshot"][node_id]["capability"]["native_binary_sha256"],
                    "native_build_changed")
            row.update(ready=readiness, phase="ready", reason="owner_prepared")
            self._put("tasks", row["task_id"], row); return self._status(row)
        return self._call(operation)

    def _clock_health(self, now):
        if self.mode == "fixture": return
        require(callable(self.time_health), "clock_untrusted")
        health = self.time_health()
        require(type(health) is dict and health.get("synchronized") is True
                and -1 <= now - timestamp(health.get("checked_at")) <= 5
                and type(health.get("uncertainty_ms")) in (int, float)
                and 0 <= health["uncertainty_ms"] <= 250, "clock_untrusted")

    def authorize(self, node_id, task):
        def operation(now):
            require(not self._paused(), "operator_paused")
            value, row = self._task(task)
            require(node_id == row["owner"], "not_selected_owner")
            require(not row["cancel_requested"], "cancellation_requested")
            if row["grant"] is not None: return self._status(row)
            self._enrollment(value)
            require(row["phase"] == "ready" and row["ready"] is not None, "owner_not_prepared")
            self._presence(node_id, now, row["selected_boot"])
            readiness = row["ready"]
            require(-5 <= now - readiness["checked_at"] <= 120 and now <= readiness["expires_at"]
                    and now <= value["release_at"], "authorization_deadline")
            self._clock_health(now)
            now = self._observe()
            self._presence(node_id, now, row["selected_boot"])
            require(-5 <= now - readiness["checked_at"] <= 120 and now <= readiness["expires_at"]
                    and now <= value["release_at"], "authorization_deadline")
            grant = self.signer.sign_grant({"grant_id": uuid.uuid4().hex, "task_id": row["task_id"],
                    "binding_sha256": row["binding_sha256"], "node_id": node_id, "issued_at": now,
                    "release_at": value["release_at"], "expires_at": value["release_at"] + 30,
                    "credential_revision": value["credential_revision"]})
            self._commit_deadline = value["release_at"]
            row.update(phase="authorized", grant=grant, reason="authorized_once")
            self._put("tasks", row["task_id"], row); return self._status(row)
        return self._call(operation)

    def _fallback(self, row, now, *, reason):
        require(row['grant'] is None and row['phase'] in ('selected', 'ready')
                and not row['cancel_requested'], 'fallback_forbidden')
        value = row['task']; enrollment = self._enrollment(value)
        exhausted = bool(row['excluded'])
        row['excluded'].append(row['owner'])
        row.update(owner=None, selected_boot=None, ready=None, phase='expired_unissued',
                   reason='fallback_exhausted' if exhausted else 'fallback_unavailable')
        if not exhausted and now <= value['release_at'] - 3:
            for node in NODES:
                if node not in row['snapshot'] or node in row['excluded']:
                    continue
                data = row['snapshot'][node]
                try:
                    self._presence(node, now, data['boot_id'])
                    require(node in enrollment['nodes']
                            and data['capability']['credential_revision'] == enrollment['credential_revision'],
                            'node_not_ready')
                    expected = enrollment['native_hashes'].get(node)
                    require(expected is None or expected == data['capability']['native_binary_sha256'],
                            'native_build_mismatch')
                    # The frozen network observation can age during preparation;
                    # ready/authorize must still revalidate current credentials,
                    # native identity, boot identity and clock before any grant.
                    row.update(owner=node, selected_boot=data['boot_id'], phase='selected',
                               reason='frozen_candidate_fallback' if reason == 'preparation_failed' else reason)
                    break
                except DualNodeError:
                    continue
        self._put('tasks', row['task_id'], row)
        return row

    def prepare_failed(self, node_id, task, *, code):
        def operation(now):
            _, row = self._task(task)
            require(code == "preparation_failed" and row["owner"] == node_id and row["grant"] is None
                    and row["phase"] in ("selected", "ready"), "fallback_forbidden")
            require(not row["excluded"], "fallback_exhausted")
            return self._status(self._fallback(row, now, reason='preparation_failed'))
        return self._call(operation)

    def cancel(self, task, *, reason="operator_cancelled"):
        def operation(now):
            _, row = self._task(task)
            require(reason == "operator_cancelled", "invalid_cancel_reason")
            if row["phase"] not in TERMINAL:
                row.update(cancel_requested=True, phase="cancel_requested" if row["grant"] else "cancelled",
                           reason="cancellation_requested" if row["grant"] else "cancelled_before_grant")
                self._put("tasks", row["task_id"], row)
            return self._status(row)
        return self._call(operation)

    def result(self, node_id, task, *, grant_id, status, receipt_ref=None,
               verified=False, seat_index=None, attempt_count=0, diagnostics=None):
        def operation(now):
            _, row = self._task(task)
            require(node_id == row["owner"] and row["grant"] is not None
                    and row["grant"]["payload"]["grant_id"] == grant_id, "wrong_grant")
            require(status in ("completed", "failed", "not_sent", "uncertain"), "invalid_result")
            require(type(verified) is bool and type(attempt_count) is int and 0 <= attempt_count <= 6,
                    "invalid_result")
            metadata = {"status": status, "verified": verified, "attempt_count": attempt_count}
            if diagnostics is not None:
                from .contracts import result_diagnostics
                metadata['diagnostics'] = result_diagnostics(diagnostics, row['task'])
                require(len(metadata['diagnostics']['attempts']) <= attempt_count, 'invalid_result_diagnostics')
            if status == "completed":
                identifier(receipt_ref, size=64)
                require(verified is True and type(seat_index) is int
                        and 0 <= seat_index < len(row["task"]["seat_priorities"])
                        and 1 <= attempt_count <= 6, "unverified_completion")
                metadata.update(receipt_ref=receipt_ref, seat_index=seat_index)
            else:
                require(verified is False and receipt_ref is None and seat_index is None, "invalid_result")
            if row["phase"] in TERMINAL:
                previous = dict(row.get("result") or {})
                previous.pop("result_at", None)
                require(row["phase"] == status and previous == metadata, "result_conflict")
                return self._status(row)
            metadata["result_at"] = now
            row.update(phase=status, reason="owner_result", result=metadata, mirror_ack=False)
            self._put("tasks", row["task_id"], row); return self._status(row)
        return self._call(operation)

    def result_ack(self, task, *, grant_id, result_sha256):
        def operation(now):
            _, row = self._task(task)
            identifier(result_sha256, size=64)
            require(row["grant"] is not None and row["grant"]["payload"]["grant_id"] == grant_id
                    and row["phase"] in ("completed", "failed", "not_sent", "uncertain")
                    and row.get("result") is not None
                    and canonical_hash(row["result"]) == result_sha256, "wrong_result_ack")
            if row.get("mirror_ack") is not True:
                row["decision_evidence"] = {node: item.get("summary", {}).get("evidence_sha256")
                                           for node, item in row["snapshot"].items()}
                if row.get("ready") is not None:
                    row["prepared_evidence_sha256"] = canonical_hash(row["ready"])
                row.update(mirror_ack=True, mirror_ack_at=now, reports={}, snapshot={}, ready=None)
                self._put("tasks", row["task_id"], row)
            return self._status(row)
        return self._call(operation)

    def time(self, node_id, nonce):
        def operation(now):
            require(node_id in NODES, "unknown_node"); identifier(nonce, size=32)
            self._clock_health(now)
            now = self._observe()
            return {"signed_time": self.signer.sign_time({"node_id": node_id, "nonce": nonce,
                    "server_wall": now, "sent_at": now, "valid_until": now + 900})}
        return self._call(operation)

    def request(self, request, *, node_id=None, role="node"):
        from .rpc import _dispatch
        def operation(now):
            exact(request, {"schema", "op", "request_id", "body"})
            require(type(request["schema"]) is int and request["schema"] == 1
                    and role in ("node", "publisher") and (node_id in NODES if role == "node" else node_id is None))
            identifier(request["request_id"], size=32)
            key = role + ":" + str(node_id) + ":" + request["request_id"]
            digest = canonical_hash(request)
            cached = request["op"] in ("authorize", "result", "result_ack", "publish", "cancel", "prepare_failed", "control")
            old = self._get("requests", key) if cached else None
            if old is not None:
                require(old["digest"] == digest, "request_id_conflict")
                # Authorization replay still observes current cancellation state.
                if request["op"] not in ("authorize", "control"): return old["response"]
            result = _dispatch(self, request, node_id=node_id, role=role)
            response = {"schema": 1, "request_id": request["request_id"], "ok": True, "result": result}
            if cached:
                entries = [(k, json.loads(v)) for k, v in self.db.execute("SELECT request_key,value FROM requests")]
                entries.sort(key=lambda item: (item[1].get("created_at", 0), item[0]))
                retained = []
                for entry_key, data in entries:
                    if data.get("created_at", 0) < now - REQUEST_CACHE_SECONDS:
                        self.db.execute("DELETE FROM requests WHERE request_key=?", (entry_key,))
                    elif entry_key != key:
                        retained.append(entry_key)
                for entry_key in retained[:max(0, len(retained) - REQUEST_CACHE_LIMIT + 1)]:
                    self.db.execute("DELETE FROM requests WHERE request_key=?", (entry_key,))
                self._put("requests", key, {"digest": digest, "response": response, "created_at": now})
            return response
        return self._call(operation)
