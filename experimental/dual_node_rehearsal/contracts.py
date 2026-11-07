"""Strict synthetic inputs and bounded files for the offline rehearsal."""
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

MODE = "dual_node_rehearsal_v1"
NODE_IDS = ("mac", "ubuntu")
REPORT_LIMIT = 1048576


class RehearsalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def envelope(**fields: Any) -> dict:
    return {**fields, "mode": MODE, "rehearsal_only": True,
            "live_execution_authorized": False}


def canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise RehearsalError("invalid_json_value") from None
    return hashlib.sha256(encoded).hexdigest()


def wall_time(now: Any) -> float:
    try:
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 4102444800:
            raise ValueError()
        return float(now)
    except (ValueError, OverflowError):
        raise RehearsalError("invalid_time") from None


def intent_identity(intent: Any) -> tuple[str, str]:
    keys = {"schema", "account_ref", "day", "kind", "operation", "plan_version",
            "start_minute", "end_minute", "seat_ref", "release_at"}
    try:
        if type(intent) is not dict or set(intent) != keys:
            raise ValueError()
        if type(intent["schema"]) is not int or intent["schema"] != 1:
            raise ValueError()
        for name in ("account_ref", "seat_ref"):
            if type(intent[name]) is not str or not re.fullmatch(r"fixture-[a-z0-9_-]{1,48}", intent[name]):
                raise ValueError()
        if (type(intent["day"]) is not str
                or date.fromisoformat(intent["day"]).isoformat() != intent["day"]
                or intent["kind"] != "S" or intent["operation"] != "-"):
            raise ValueError()
        if (type(intent["plan_version"]) is not int
                or not 1 <= intent["plan_version"] <= 2147483647
                or type(intent["start_minute"]) is not int
                or type(intent["end_minute"]) is not int
                or not 0 <= intent["start_minute"] < intent["end_minute"] <= 1440):
            raise ValueError()
        wall_time(intent["release_at"])
    except (ValueError, TypeError, KeyError):
        raise RehearsalError("invalid_intent") from None
    logical = {name: intent[name] for name in ("account_ref", "day", "kind", "operation")}
    return canonical_hash({"namespace": MODE, **logical}), canonical_hash(intent)


def _path(value: Any) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError()
        return path
    except (TypeError, ValueError, OSError):
        raise RehearsalError("unsafe_file_path") from None


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise ValueError("nonfinite JSON")


def safe_read_json(value: Any) -> Any:
    path = _path(value)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > REPORT_LIMIT:
                raise ValueError()
            data = stream.read(REPORT_LIMIT + 1)
        if len(data) > REPORT_LIMIT:
            raise ValueError()
        return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    except (OSError, ValueError, TypeError, RecursionError):
        raise RehearsalError("invalid_input_file") from None


def write_json(value: Any, report: Any) -> None:
    path = _path(value)
    try:
        data = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if len(data) > REPORT_LIMIT:
            raise ValueError()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError, TypeError, RecursionError):
        raise RehearsalError("output_failed") from None
