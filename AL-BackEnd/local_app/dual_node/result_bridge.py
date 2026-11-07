"""Journal-acknowledged, credential-free mirror into the existing run history."""
from dataclasses import asdict
from datetime import datetime
import time

from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from ..models import RunResult, RunSpec, ZONE, stamp
from .contracts import (DualNodeError, NODES, canonical_hash, exact, identifier,
                        require, task_identity, timestamp, validate_task, result_diagnostics, RESULT_REASON_MESSAGES)

FINAL = ("completed", "failed", "not_sent", "uncertain")
PHASES = ("collecting", "selected", "ready", "authorized", "cancel_requested",
          "cancelled", "expired_unissued", *FINAL)


def run_spec(task, account_id):
    task = validate_task(task); task_id, _ = task_identity(task)
    def hhmm(value): return f"{value // 60:02d}:{value % 60:02d}"
    run = RunSpec(account_id, task["revision"], task["day"], hhmm(task["start_minute"]),
                  hhmm(task["end_minute"]), tuple(task["seat_priorities"]), task["release_at"],
                  operation_id="dual:" + task_id, expires_at=task["release_at"] + 30)
    require(run.key == account_id + ":" + task["day"] + ":dual:" + task_id, "dual_runspec_not_supported")
    return run


def _result(value, task, phase):
    if phase not in FINAL:
        require(value is None, "unexpected_result")
        return None
    exact(value, {"status", "verified", "attempt_count", "result_at"}, {"receipt_ref", "seat_index", "diagnostics"})
    require(value["status"] == phase and type(value["verified"]) is bool
            and type(value["attempt_count"]) is int and 0 <= value["attempt_count"] <= 6,
            "invalid_mirror_result")
    timestamp(value["result_at"])
    if 'diagnostics' in value:
        diagnostic = result_diagnostics(value['diagnostics'], task)
        require(len(diagnostic['attempts']) <= value['attempt_count'], 'invalid_result_diagnostics')
    if phase == "completed":
        identifier(value.get("receipt_ref"), size=64)
        require(value["verified"] is True and type(value.get("seat_index")) is int
                and 0 <= value["seat_index"] < len(task["seat_priorities"])
                and 1 <= value["attempt_count"] <= 6, "unverified_completion")
    else:
        require(value["verified"] is False and "receipt_ref" not in value and "seat_index" not in value,
                "invalid_mirror_result")
    return dict(value)


def dual_power_row(rows, run, *, credential_revision=None, account=None):
    """Read-only matching of a current plan to its namespaced dual record."""
    found = []
    for row in rows:
        try:
            binding, value = row["dual_binding"], row["dual_task"]
            task_id, digest = task_identity(value)
            require(binding["schema"] == 1 and binding["mode"] == "production"
                    and binding["task_id"] == task_id and binding["binding_sha256"] == digest)
            candidate = run_spec(value, run.account_id)
            require(row["_id"] == candidate.key and "native_binding" not in row
                    and canonical_hash(row["spec"]) == canonical_hash(asdict(candidate)))
            require(type(credential_revision) is int and credential_revision >= 1
                    and value['credential_revision'] == credential_revision
                    and candidate.revision <= run.revision, 'mirror_plan_changed')
            fields = ("account_id", "target_date", "begin", "end", "seat_ids", "due_at", "kind")
            require(all(getattr(candidate, key) == getattr(run, key) for key in fields))
            if row['status'] == 'success':
                if row.get('dual_reconciliation') is not None:
                    from .receipt_view import receipt_view
                    view = receipt_view(account, row)
                    require(view is not None and view.status == 'success', 'mirror_reconciliation_conflict')
                else:
                    _result(row['dual_result'], value, 'completed')
            found.append(row)
        except (DualNodeError, KeyError, TypeError, ValueError):
            continue
    return found[0] if len(found) == 1 else None


class ResultBridge:
    def __init__(self, store, registrations, managed_gate, *, mode="production", clock=None):
        require(mode in ("fixture", "production"), "invalid_mode")
        self.store, self.registrations, self.gate = store, registrations, managed_gate
        self.mode, self.clock = mode, clock or time.time
        self.runs = store.runs.with_options(write_concern=WriteConcern(w=1, j=True))
        self.scopes = store.db.campus_dual_task_scope.with_options(write_concern=WriteConcern(w=1, j=True))
        self.scopes.create_index([("binding.mode", 1), ("binding.task_id", 1), ("binding.binding_sha256", 1)])

    def local_account(self, task):
        task_id, binding = task_identity(task)
        candidates = [key for key, value in self.registrations.items()
                      if value.get("account_ref") == task["account_ref"]]
        require(len(candidates) <= 1, "duplicate_account_reference")
        scope = self.scopes.find_one({"binding.mode": self.mode, "binding.task_id": task_id,
                                      "binding.binding_sha256": binding})
        account_id = candidates[0] if candidates else scope.get("account_id") if scope else None
        require(type(account_id) is str and self.gate.is_managed(account_id) is True,
                "mirror_account_unavailable")
        return account_id

    def _source(self, task, status):
        task = validate_task(task); task_id, binding = task_identity(task)
        require(type(status) is dict and status.get("mode") == self.mode
                and status.get("task_id") == task_id and status.get("binding_sha256") == binding
                and status.get("phase") in PHASES, "mirror_binding_conflict")
        authority = identifier(status.get("authority_id"), size=32)
        grant = status.get("grant")
        if grant is not None:
            require(type(grant) is dict and grant.get("domain") == "njfu-dual-node-" + self.mode + "-v1",
                    "wrong_grant_domain")
            payload = grant.get("payload", {})
            require(payload.get("authority_id") == authority and payload.get("task_id") == task_id
                    and payload.get("binding_sha256") == binding and payload.get("node_id") == status.get("owner")
                    and payload.get("node_id") in NODES and payload.get("release_at") == task["release_at"]
                    and payload.get("credential_revision") == task["credential_revision"], "mirror_binding_conflict")
            identifier(payload.get("grant_id"), size=32)
        if status["phase"] in FINAL: require(grant is not None, "missing_result_grant")
        binding_value = {"schema": 1, "mode": self.mode, "authority_id": authority,
                         "task_id": task_id, "binding_sha256": binding}
        return task, binding_value

    def _scope(self, task, binding):
        key = binding["authority_id"] + ":" + binding["task_id"] + ":" + binding["binding_sha256"]
        existing = self.scopes.find_one({"_id": key})
        candidates = [key for key, value in self.registrations.items()
                      if value.get("account_ref") == task["account_ref"]]
        require(len(candidates) <= 1, "duplicate_account_reference")
        account_id = candidates[0] if candidates else existing.get("account_id") if existing else None
        require(type(account_id) is str and self.gate.is_managed(account_id) is True
                and self.store.accounts.find_one({"_id": account_id}, {"_id": 1}) is not None,
                "mirror_account_unavailable")
        if existing is not None:
            require(existing["account_id"] == account_id and existing["binding"] == binding
                    and existing["task"] == task, "mirror_scope_conflict")
            return existing
        value = {"_id": key, "account_id": account_id, "binding": binding, "task": task,
                 "created_at": self.clock(), "last_polled_at": None, "result_sha256": None,
                 "grant_id": None, "ack_confirmed": False, "retired": False}
        result = self.scopes.update_one({"_id": key}, {"$setOnInsert": value}, upsert=True)
        require(result.acknowledged, "mirror_write_unacknowledged")
        stored = self.scopes.find_one({"_id": key})
        require(stored is not None and stored["account_id"] == account_id and stored["binding"] == binding,
                "mirror_scope_conflict")
        return stored

    def record_scope(self, task, status):
        task, binding = self._source(task, status)
        current = self._scope(task, binding)
        # A server-accepted higher binding is authoritative evidence that this
        # stable task never issued a grant under its prior binding.
        previous = self.scopes.find({"binding.mode": self.mode,
            "binding.authority_id": binding["authority_id"], "binding.task_id": binding["task_id"],
            "task.revision": {"$lt": task["revision"]}})
        for scope in previous:
            old_run = run_spec(scope["task"], scope["account_id"])
            row = self.runs.find_one({"_id": old_run.key})
            if row is not None and row.get("dual_binding") == scope["binding"]:
                self._owned(row, scope["task"], scope["binding"], old_run)
                require(not (row.get("dual_progress") or {}).get("grant_id")
                        and row.get("dual_result") is None, "mirror_history_conflict")
            self._retire(scope, "superseded_unissued", {"accepted_binding": binding,
                "revision": task["revision"], "previous_binding": scope["binding"]})
        return current["_id"]

    def _retire(self, scope, reason, evidence):
        require(scope.get("grant_id") is None and scope.get("result_sha256") is None,
                "mirror_history_conflict")
        digest = canonical_hash(evidence)
        if (scope.get("retired") is True and scope.get("retirement_reason") == reason
                and scope.get("retirement_sha256") == digest):
            return
        updated = self.scopes.update_one({"_id": scope["_id"], "binding": scope["binding"],
            "grant_id": None, "result_sha256": None}, {"$set": {"retired": True,
            "retirement_reason": reason, "retirement_sha256": digest, "retired_at": self.clock()}})
        require(updated.acknowledged and updated.matched_count == 1, "mirror_write_conflict")
        checked = self.scopes.find_one({"_id": scope["_id"]})
        require(checked is not None and checked.get("retired") is True
                and checked.get("retirement_sha256") == digest, "mirror_readback_failed")

    def _owned(self, row, task, binding, run):
        require(row.get("dual_binding") == binding and "native_binding" not in row
                and canonical_hash(row.get("spec")) == canonical_hash(asdict(run))
                and row.get("dual_task") == task, "mirror_history_conflict")

    def _revision(self, row, task, binding, run):
        previous = row.get("dual_binding") or {}
        require(previous.get("mode") == binding["mode"]
                and previous.get("authority_id") == binding["authority_id"]
                and previous.get("task_id") == binding["task_id"], "mirror_history_conflict")
        old = self.scopes.find_one({"binding": previous, "account_id": run.account_id})
        require(old is not None and old["task"]["revision"] < task["revision"]
                and (row["status"] in ("queued", "preparing", "skipped")
                     or (row["status"] == "uncertain" and old.get("retired") is True
                         and old.get("retirement_reason") == "superseded_unissued"))
                and row.get("attempted_seat_id") is None and row.get("dual_result") is None
                and not (row.get("dual_progress") or {}).get("grant_id"), "mirror_history_conflict")
        self._owned(row, old["task"], previous, run_spec(old["task"], run.account_id))
        changed = self.runs.update_one({"_id": run.key, "dual_binding": previous, "spec": row["spec"],
            "status": row["status"], "dual_progress": row["dual_progress"],
            "native_binding": {"$exists": False}}, {"$set": {"dual_binding": binding, "dual_task": task,
                "spec": asdict(run), "dual_progress": {}, "status": "queued",
                "message": "双机计划版本已更新，等待执行", "updated_at": stamp()}})
        require(changed.acknowledged and changed.matched_count == 1, "mirror_write_conflict")
        return self.runs.find_one({"_id": run.key})

    def apply(self, task, status):
        task, binding = self._source(task, status)
        phase = status["phase"]; metadata = _result(status.get("result"), task, phase)
        scope = self._scope(task, binding); run = run_spec(task, scope["account_id"])
        desired = {"completed": "success", "failed": "failed", "not_sent": "skipped",
                   "uncertain": "uncertain", "cancel_requested": "uncertain",
                   "cancelled": "skipped", "expired_unissued": "skipped",
                   "collecting": "queued", "selected": "preparing", "ready": "preparing",
                   "authorized": "submitting"}[phase]
        if phase == "authorized" and self.clock() > task["release_at"] + 30: desired = "uncertain"
        grant_id = status["grant"]["payload"]["grant_id"] if status.get("grant") else None
        digest = canonical_hash(metadata) if metadata is not None else None
        messages = {"success": "学校有效预约已由双机执行节点核验", "failed": "学校明确拒绝，双机执行已结束",
                    "skipped": "双机任务未发送或已取消", "uncertain": "双机执行结果待核对，禁止重新提交",
                    "queued": "双机任务等待选机", "preparing": "双机节点正在认证及准备",
                    "submitting": "双机授权已签发，等待学校结果"}
        message = messages[desired]
        diagnostic = metadata.get('diagnostics') if metadata is not None else None
        if diagnostic is not None: message += '；' + RESULT_REASON_MESSAGES[diagnostic['reason']]
        label = {"mac": "Mac", "ubuntu": "Ubuntu"}.get(status.get("owner"))
        if label is not None: message = label + "执行：" + message
        seat = task["seat_priorities"][metadata["seat_index"]] if phase == "completed" else None
        if seat is not None: message += "；实际座位" + seat
        initial = {"_id": run.key, "spec": asdict(run), "account_id": run.account_id,
                   "target_date": run.target_date, "status": "queued", "created": self.clock(),
                   "updated_at": stamp(), "message": "双机任务等待选机", "attempted_seat_id": None,
                   "dual_binding": binding, "dual_task": task, "dual_progress": {},
                   "dual_result": None}
        inserted = self.runs.update_one({"_id": run.key}, {"$setOnInsert": initial}, upsert=True)
        require(inserted.acknowledged, "mirror_write_unacknowledged")
        row = self.runs.find_one({"_id": run.key})
        if row.get("dual_binding") != binding:
            row = self._revision(row, task, binding, run)
        self._owned(row, task, binding, run)
        local_confirmation = row.get('dual_reconciliation') is not None
        if local_confirmation:
            from .receipt_view import receipt_view
            account = self.store.get_account(run.account_id)
            require(receipt_view(account, row) is not None, 'mirror_reconciliation_conflict')
        if row["status"] == "success" and desired != "success" and not local_confirmation:
            return None
        old_grant = (row.get("dual_progress") or {}).get("grant_id")
        require(old_grant is None or old_grant == grant_id, "mirror_grant_conflict")
        if row["status"] == "uncertain" and desired in ("queued", "preparing", "submitting"):
            desired, message = "uncertain", messages["uncertain"]
        if row["status"] in ("failed", "skipped") and desired in ("queued", "preparing", "submitting"):
            return None
        if row["status"] == "success" and row.get("dual_result") != metadata and not local_confirmation:
            raise DualNodeError("mirror_history_conflict")
        progress = {"phase": phase, "owner": status.get("owner"), "grant_id": grant_id}
        fields = {"status": desired, "message": message, "dual_progress": progress,
                  "dual_result": metadata, "updated_at": stamp()}
        if diagnostic is not None:
            fields['attempts'] = [{'seat_id': task['seat_priorities'][item['seat_index']],
                'outcome': item['outcome'], 'sent': item['sent'],
                'message': RESULT_REASON_MESSAGES[item['reason']],
                'timings': {key: item[key] for key in ('request_ms', 'request_started_at') if key in item}}
                for item in diagnostic['attempts']]
            if 'prepare_ms' in diagnostic:
                fields['scheduler_timings'] = {'prepare': {'elapsed_ms': diagnostic['prepare_ms']}}
        if metadata is not None:
            at = datetime.fromtimestamp(metadata["result_at"], ZONE).isoformat(timespec="seconds")
            result = asdict(RunResult(desired, message, [], at if desired == "success" else None))
            if phase == "completed": result.update(receipt_ref=metadata["receipt_ref"], seat_id=seat)
            fields.update(result=result, attempted_seat_id=seat,
                          dual_mirror={"result_sha256": digest, "grant_id": grant_id})
        if local_confirmation:
            # The immutable server result still advances and gets acknowledged;
            # independently queried success remains a separate local projection.
            desired = 'success'
            fields.update(status=desired, message=row['message'])
            require(receipt_view(account, row | fields) is not None, 'mirror_reconciliation_conflict')
        stable = {key: row.get(key) for key in fields if key != "updated_at"}
        if stable != {key: value for key, value in fields.items() if key != "updated_at"}:
            changed = self.runs.update_one({"_id": run.key, "dual_binding": binding, "spec": row["spec"],
                "status": row["status"], "dual_progress": row["dual_progress"],
                "native_binding": {"$exists": False}}, {"$set": fields})
            require(changed.acknowledged and changed.matched_count == 1, "mirror_write_conflict")
        checked = self.runs.find_one({"_id": run.key})
        self._owned(checked, task, binding, run)
        require(checked["status"] == desired and checked["dual_progress"] == progress
                and checked.get("dual_result") == metadata, "mirror_readback_failed")
        # The source-tagged insert already established ownership; this path is
        # now LocalStore.ensure_run's exact-spec read branch, never its rebinder.
        self.store.ensure_run(run)
        if metadata is None:
            if phase in ("cancelled", "expired_unissued") and grant_id is None:
                self._retire(scope, phase, {"mode": self.mode, "binding": binding,
                                           "phase": phase, "grant_id": None})
            return None
        checkpoint = self.scopes.update_one({"_id": scope["_id"], "binding": binding},
            {"$set": {"result_sha256": digest, "grant_id": grant_id, "ack_confirmed": False}})
        require(checkpoint.acknowledged and checkpoint.matched_count == 1, "mirror_write_conflict")
        return {"task": task, "grant_id": grant_id, "result_sha256": digest}

    def _ack(self, client, scope):
        run = run_spec(scope["task"], scope["account_id"])
        row = self.runs.find_one({"_id": run.key})
        require(row is not None, "mirror_history_missing")
        self._owned(row, scope["task"], scope["binding"], run)
        require(canonical_hash(row.get("dual_result")) == scope["result_sha256"]
                and row.get("dual_mirror") == {"result_sha256": scope["result_sha256"],
                                               "grant_id": scope["grant_id"]}, "mirror_history_conflict")
        result = client.call("result_ack", task=scope["task"], grant_id=scope["grant_id"],
                             result_sha256=scope["result_sha256"])
        task, binding = self._source(scope["task"], result)
        require(binding == scope["binding"] and result.get("mirror_ack") is True
                and canonical_hash(result.get("result")) == scope["result_sha256"], "mirror_ack_conflict")
        updated = self.scopes.update_one({"_id": scope["_id"], "result_sha256": scope["result_sha256"],
            "grant_id": scope["grant_id"]}, {"$set": {"ack_confirmed": True}})
        require(updated.acknowledged and updated.matched_count == 1, "mirror_write_conflict")

    def sync(self, client, known_tasks, *, max_polls=8):
        require(type(max_polls) is int and 1 <= max_polls <= 16, "invalid_mirror_budget")
        now = self.clock()
        result = {"requests": 0, "polled": 0, "mirrored": 0, "acknowledged": 0, "blocked": 0}
        pending = list(self.scopes.find({'binding.mode': self.mode, 'result_sha256': {'$ne': None},
            'ack_confirmed': False, 'retired': {'$ne': True},
            '$or': [{'ack_last_attempt_at': None}, {'ack_last_attempt_at': {'$lte': now-15}}]})
            .sort([('ack_last_attempt_at', 1), ('task.release_at', 1), ('_id', 1)]).limit(max_polls))
        candidates = {}
        # The server's short inventory omits old, ungranted terminal tasks.
        # Recover them from durable local scopes, oldest poll first. A refused
        # scope consumes only this bounded poll; it cannot starve its siblings.
        recoverable = self.scopes.find({'binding.mode': self.mode,
            'retired': {'$ne': True}, 'ack_confirmed': {'$ne': True},
            'result_sha256': None, 'task.release_at': {'$lt': now-305},
            '$or': [{'last_polled_at': None}, {'last_polled_at': {'$lte': now-15}}]})
        for scope in recoverable.sort([('last_polled_at', 1), ('task.release_at', 1), ('_id', 1)]).limit(max_polls):
            try:
                value = validate_task(scope['task']); task_id, binding = task_identity(value)
                require(scope['binding']['task_id'] == task_id
                        and scope['binding']['binding_sha256'] == binding, 'mirror_scope_conflict')
                last = scope.get('last_polled_at')
                if last is not None: last = timestamp(last)
                candidates[(task_id, binding)] = (last if last is not None else float('-inf'), value, scope)
            except (DualNodeError, KeyError, TypeError, ValueError):
                # A corrupt local scope is not retired or sent. Rotate its
                # bounded inspection slot so intact scopes can still recover.
                self.scopes.update_one({'_id': scope['_id'], 'last_polled_at': scope.get('last_polled_at')},
                    {'$set': {'last_polled_at': now}})
                result['blocked'] += 1
        for value in known_tasks:
            try:
                value = validate_task(value); task_id, binding = task_identity(value)
                scope = self.scopes.find_one({"binding.mode": self.mode, "binding.task_id": task_id,
                                               "binding.binding_sha256": binding})
                mapped = any(row.get("account_ref") == value["account_ref"] for row in self.registrations.values())
                if scope is None and not mapped: continue
                if scope and (scope.get("ack_confirmed") or scope.get("retired")): continue
                if scope and scope.get('result_sha256') is not None: continue
                if now < value["release_at"] - 450: continue
                last = scope.get("last_polled_at") if scope else None
                if last is not None: last = timestamp(last)
                if last is not None and 0 <= now - last < 15: continue
                candidates[(task_id, binding)] = (last if last is not None else float("-inf"), value, scope)
            except (DualNodeError, KeyError, TypeError, ValueError):
                result["blocked"] += 1
        # A failed acknowledgement and a status read share one fair budget.
        # Persistent timestamps retain progress through publisher restarts.
        work = [(last, value['release_at'], 0, task_identity(value)[0], value, scope)
                for last, value, scope in candidates.values()]
        for scope in pending:
            try:
                value = validate_task(scope['task'])
                last = scope.get('ack_last_attempt_at')
                if last is not None: last = timestamp(last)
                work.append((last if last is not None else float('-inf'), value['release_at'],
                             1, scope['_id'], None, scope))
            except (DualNodeError, KeyError, TypeError, ValueError):
                self.scopes.update_one({'_id': scope['_id']}, {'$set': {'ack_last_attempt_at': now}})
                result['blocked'] += 1
        for _, _, kind, _, value, scope in sorted(work, key=lambda item: item[:4]):
            if result["requests"] >= max_polls: break
            try:
                if kind == 1:
                    self.scopes.update_one({'_id': scope['_id']}, {'$set': {'ack_last_attempt_at': now}})
                    result['requests'] += 1
                    self._ack(client, scope); result['acknowledged'] += 1
                    continue
                if scope is not None:
                    self.scopes.update_one({"_id": scope["_id"]}, {"$set": {"last_polled_at": now}})
                result["requests"] += 1; result["polled"] += 1
                status = client.call("status", task=value)
                ack = self.apply(value, status)
                result["mirrored"] += 1
                if ack is not None and result["requests"] < max_polls:
                    scope = self.scopes.find_one({"binding.mode": self.mode,
                        "binding.task_id": status["task_id"], "binding.binding_sha256": status["binding_sha256"]})
                    self.scopes.update_one({'_id': scope['_id']}, {'$set': {'ack_last_attempt_at': now}})
                    result["requests"] += 1; self._ack(client, scope); result["acknowledged"] += 1
            except Exception: result["blocked"] += 1
        return result
