"""Bounded JSON RPC. Identity and role are fixed by the SSH forced command."""
import re
from .contracts import DualNodeError, exact, require


def _dispatch(coordinator, request, *, node_id=None, role="node"):
    op, body = request["op"], request["body"]
    require(type(op) is str and type(body) is dict)
    if role == "publisher":
        require(op in ("tasks", "publish", "cancel", "status", "result_ack", "control", "health"), "operation_forbidden")
        if op == "health":
            exact(body, set()); return coordinator.health()
        if op == "control":
            exact(body, {"action"}); return coordinator.control(action=body["action"])
        if op == "tasks":
            exact(body, set(), {"after", "limit"}); return coordinator.tasks(include_unmirrored=True, **body)
        if op == "status":
            exact(body, {"task"}); return coordinator.inspect_status(body["task"])
        if op == "result_ack":
            exact(body, {"task", "grant_id", "result_sha256"})
            return coordinator.result_ack(body["task"], grant_id=body["grant_id"], result_sha256=body["result_sha256"])
        if op == "publish":
            exact(body, {"task"}); return coordinator.publish(body["task"])
        exact(body, {"task"}, {"reason"})
        return coordinator.cancel(body["task"], reason=body.get("reason", "operator_cancelled"))
    require(role == "node", "operation_forbidden")
    if op == "tasks":
        exact(body, set(), {"after", "limit"}); return coordinator.tasks(node_id, **body)
    if op == "heartbeat":
        exact(body, {"boot_id", "seq"})
        return coordinator.heartbeat(node_id, boot_id=body["boot_id"], seq=body["seq"])
    if op == "time":
        exact(body, {"nonce"}); return coordinator.time(node_id, body["nonce"])
    if op == "report":
        exact(body, {"task", "report", "readiness"})
        return coordinator.report(node_id, body["task"], body["report"], body["readiness"])
    if op in ("status", "select", "authorize"):
        exact(body, {"task"})
        if op == "authorize": return coordinator.authorize(node_id, body["task"])
        return coordinator.node_status(node_id, body["task"])
    if op == "ready":
        exact(body, {"task", "readiness"})
        return coordinator.ready(node_id, body["task"], body["readiness"])
    if op == "prepare_failed":
        exact(body, {"task", "code"})
        return coordinator.prepare_failed(node_id, body["task"], code=body["code"])
    if op == "result":
        exact(body, {"task", "grant_id", "status"}, {"receipt_ref", "verified", "seat_index", "attempt_count", "diagnostics"})
        return coordinator.result(node_id, body["task"], grant_id=body["grant_id"],
                                  status=body["status"], receipt_ref=body.get("receipt_ref"),
                                  verified=body.get("verified", False), seat_index=body.get("seat_index"),
                                  attempt_count=body.get("attempt_count", 0), diagnostics=body.get('diagnostics'))
    raise DualNodeError("operation_forbidden")


def dispatch(coordinator, request, *, node_id=None, role="node"):
    request_id = request.get("request_id") if type(request) is dict else None
    try:
        return coordinator.request(request, node_id=node_id, role=role)
    except DualNodeError as exc:
        return {"schema": 1, "request_id": request_id if type(request_id) is str and re.fullmatch("[0-9a-f]{32}", request_id) else None,
                "ok": False, "code": exc.code}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {"schema": 1, "request_id": None, "ok": False, "code": "rpc_unavailable"}
