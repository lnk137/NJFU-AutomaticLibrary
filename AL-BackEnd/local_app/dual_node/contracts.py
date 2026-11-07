"""Strict, credential-free candidate wire contracts. No execution authority here."""
from datetime import date
import hashlib
import json
import math
import re

NODES = ("mac", "ubuntu")
LIMIT = 1048576
MODES = ("fixture", "production")

RESULT_REASON_MESSAGES = {
    'accepted': '学校已接收，等待记录核实', 'busy': '学校提示设备正在被预约',
    'occupied': '学校提示座位已被占用', 'rate_limited': '学校提示请求频繁',
    'school_rejected': '学校明确拒绝预约', 'transport_uncertain': '提交结果未确认',
    'not_sent': '最终执行校验阻断，预约未发送', 'receipt_matched': '学校有效回执已核实',
    'receipt_unconfirmed': '学校有效回执尚未唯一核实',
    'execution_interrupted': '执行或结果核实中断', 'permission_changed': '执行资格已变化',
}


class DualNodeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code="invalid_request"):
    if not condition:
        raise DualNodeError(code)


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 4102444800
    except OverflowError:
        return False


def timestamp(value):
    require(number(value), "invalid_time")
    return float(value)


def identifier(value, *, size=None):
    require(type(value) is str and bool(re.fullmatch(
        r"[0-9a-f]{" + str(size) + "}" if size else r"[A-Za-z0-9_-]{1,128}", value)))
    return value


def account_reference(value):
    require(type(value) is str and bool(re.fullmatch(r"(?:acct|fixture)-[A-Za-z0-9_-]{1,80}", value)),
            "invalid_account_reference")
    return value


def exact(value, keys, optional=()):
    require(type(value) is dict and set(keys) <= set(value) <= set(keys) | set(optional))
    reject_secrets(value)
    return value


def reject_secrets(value, depth=0):
    require(depth <= 20)
    forbidden = {"password", "passwd", "cookie", "setcookie", "authorization", "token",
                 "accesstoken", "refreshtoken", "privatekey", "secret", "username",
                 "studentid", "schoolaccount", "schoolcredentials"}
    if type(value) is dict:
        for key, item in value.items():
            require(type(key) is str and re.sub(r"[^a-z]", "", key.lower()) not in forbidden,
                    "credential_field_forbidden")
            reject_secrets(item, depth + 1)
    elif type(value) is list:
        require(len(value) <= 4096)
        for item in value:
            reject_secrets(item, depth + 1)
    elif type(value) is float:
        require(math.isfinite(value))
    else:
        require(type(value) in (str, int, bool, type(None)))


def canonical_bytes(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode()
        require(len(data) <= LIMIT, "input_limit")
        return data
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise DualNodeError("invalid_json") from None


def canonical_hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate_json_key")
        value[key] = item
    return value


def parse_json(data):
    try:
        require(type(data) in (bytes, str) and len(data) <= LIMIT, "input_limit")
        def nonfinite(_value):
            raise DualNodeError("invalid_json")
        value = json.loads(data, object_pairs_hook=_unique, parse_constant=nonfinite)
        require(type(value) is dict)
        reject_secrets(value)
        return value
    except DualNodeError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise DualNodeError("invalid_json") from None


def validate_task(value):
    exact(value, {"schema", "account_ref", "day", "kind", "operation", "revision",
                  "credential_revision", "release_at", "start_minute", "end_minute",
                  "seat_priorities"})
    require(type(value["schema"]) is int and value["schema"] == 1)
    account_reference(value["account_ref"])
    try:
        require(type(value["day"]) is str and date.fromisoformat(value["day"]).isoformat() == value["day"])
    except ValueError:
        raise DualNodeError("invalid_task") from None
    require(value["kind"] == "S" and value["operation"] == "-")
    for name in ("revision", "credential_revision"):
        require(type(value[name]) is int and 1 <= value[name] <= 2147483647)
    require(type(value["start_minute"]) is int and type(value["end_minute"]) is int
            and 0 <= value["start_minute"] < value["end_minute"] <= 1440)
    seats = value["seat_priorities"]
    require(type(seats) is list and 1 <= len(seats) <= 10
            and all(type(s) is str and re.fullmatch(r"[1-9][0-9]{0,15}", s) for s in seats)
            and len(set(seats)) == len(seats))
    result = dict(value)
    result["seat_priorities"] = list(seats)
    result["release_at"] = timestamp(value["release_at"])
    return result


def task_identity(value):
    value = validate_task(value)
    logical = {key: value[key] for key in ("account_ref", "day", "kind", "operation")}
    return canonical_hash({"namespace": "njfu-dual-node-task-v1", **logical}), canonical_hash(value)


def result_diagnostics(value, task=None):
    """Strict optional observation, never an execution permission or raw response."""
    exact(value, {'stage', 'reason', 'attempts'}, {'prepare_ms'})
    require(value['stage'] in ('prepare', 'submit', 'readback', 'complete')
            and value['reason'] in RESULT_REASON_MESSAGES, 'invalid_result_diagnostics')
    def duration(item):
        require(type(item) in (int, float) and 0 <= item <= 900000 and math.isfinite(item),
                'invalid_result_diagnostics')
    if 'prepare_ms' in value: duration(value['prepare_ms'])
    require(type(value['attempts']) is list and len(value['attempts']) <= 6, 'invalid_result_diagnostics')
    seats = len(task['seat_priorities']) if task is not None else 10
    for attempt in value['attempts']:
        exact(attempt, {'seat_index', 'outcome', 'reason', 'sent'}, {'request_ms', 'request_started_at'})
        require(type(attempt['seat_index']) is int and 0 <= attempt['seat_index'] < seats
                and attempt['outcome'] in ('accepted', 'rejected', 'uncertain')
                and attempt['reason'] in RESULT_REASON_MESSAGES
                and (attempt['sent'] is None or type(attempt['sent']) is bool), 'invalid_result_diagnostics')
        if 'request_ms' in attempt: duration(attempt['request_ms'])
        if 'request_started_at' in attempt: timestamp(attempt['request_started_at'])
    return json.loads(canonical_bytes(value))
