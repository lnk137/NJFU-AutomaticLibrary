#!/usr/bin/env python3
"""Standalone observations only; never imports the app or authenticates."""
import argparse
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import stat
import statistics
import subprocess
import sys
import time

VERSION = "1"
ENDPOINT = "https://libseat.njfu.edu.cn/ic-web/sysConfig/public"
ZONE = timezone(timedelta(hours=8))
REQUEST_SECONDS, PROCESS_SECONDS, GUARD_BUDGET = 3, 4, 5
BODY_LIMIT, REPORT_LIMIT = 65536, 1048576
MARKER = b"\nNJFU_PROBE_METRICS:"
DISCLAIMER = {"authentication": "not_checked", "booking_readiness": "not_checked",
              "automatic_selection_authorized": False}


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def release_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]", value):
        raise argparse.ArgumentTypeError("release time must be HH:MM:SS")
    return value


def guarded(now, releases, budget=GUARD_BUDGET):
    """Protect the full process budget, including triggers near midnight."""
    day = datetime.fromtimestamp(now, ZONE).date()
    for trigger in set(("07:00:00", *releases)):
        release_time(trigger)
        for shift in (-1, 0, 1):
            due = datetime.fromisoformat(str(day + timedelta(days=shift)) + "T" + trigger)
            due = due.replace(tzinfo=ZONE).timestamp()
            if now <= due + 300 and now + budget >= due - 300:
                return True
    return False


def bounded_run(argv, timeout=PROCESS_SECONDS, limit=8192):
    """Bound stdout bytes/time and discard stderr, including sensitive errors."""
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selected:
            selected.register(child.stdout, selectors.EVENT_READ)
            while selected.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("command_timeout")
                for key, _ in selected.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selected.unregister(key.fileobj)
                    else:
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError("output_limit")
        return child.wait(timeout=max(.01, deadline - time.monotonic())), bytes(data)
    except subprocess.TimeoutExpired:
        raise TimeoutError("command_timeout") from None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()


def base(kind, node=None):
    result = {"schema": 1, "probe_version": VERSION, "kind": kind,
              "observed_at": time.time(), **DISCLAIMER}
    if node is not None:
        result["node"] = node
    return result


def command_info(name):
    executable = shutil.which(name)
    result = {"available": executable is not None}
    if executable:
        try:
            code, output = bounded_run([executable, "--version"], timeout=2)
            result["version"] = output.decode("utf-8", "replace").splitlines()[0][:160] if code == 0 and output else None
        except (OSError, ValueError, TimeoutError):
            result["version"] = None
    return result


def inspect_node(node):
    result = base("inspect", node)
    result["platform"] = {"system": platform.system(), "release": platform.release(),
                          "machine": platform.machine(), "cpu_count": os.cpu_count()}
    result["python"] = {"version": platform.python_version(),
                        "implementation": platform.python_implementation(),
                        "executable_name": Path(sys.executable).name}
    dependencies = {}
    for package in ("requests", "urllib3", "pymongo", "cryptography", "pycryptodome", "waitress"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    result["python"]["installed_packages"] = dependencies
    result["tools"] = {name: command_info(name) for name in
                       ("curl", "c++", "clang++", "g++", "pkg-config", "systemctl")}
    result["native_execution_support"] = "not_verified"
    if sys.platform.startswith("linux"):
        result["platform"]["libc"] = list(platform.libc_ver())
        try:
            fields = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text()[:8192].splitlines()
                          if "=" in line and not line.startswith("#"))
            result["platform"]["distribution"] = {
                key: fields[key].strip('"')[:160] for key in ("ID", "VERSION_ID") if key in fields}
        except OSError:
            pass
    result["native_library_versions"] = {}
    pkg_config = shutil.which("pkg-config")
    if pkg_config:
        for library in ("libcurl", "sqlite3"):
            try:
                code, output = bounded_run([pkg_config, "--modversion", library], timeout=2)
                result["native_library_versions"][library] = output.decode("utf-8", "replace").strip()[:80] if code == 0 else None
            except (OSError, ValueError, TimeoutError):
                result["native_library_versions"][library] = None
    result["disk_free_bytes"] = shutil.disk_usage(Path.cwd()).free
    home = Path.home()
    folders = [home / ".conda/envs", Path("/opt/conda/envs"), Path("/opt/anaconda3/envs")]
    folders += [home / name / "envs" for name in ("anaconda3", "miniconda3", "miniforge3", "mambaforge")]
    names = set()
    for folder in folders:
        try:
            if folder.is_dir() and not folder.is_symlink():
                names.update(p.name for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            pass
    result["existing_conda_env_names"] = sorted(names)
    result["clock_sync"] = "not_checked"
    executable = shutil.which("timedatectl")
    if sys.platform.startswith("linux") and executable:
        try:
            code, output = bounded_run([executable, "show", "--property=NTPSynchronized", "--value"], timeout=2)
            value = output.decode().strip()
            if code == 0 and value in ("yes", "no"):
                result["clock_sync"] = "synchronized" if value == "yes" else "not_synchronized"
        except (OSError, ValueError, TimeoutError):
            pass
    result["school_network_requests"] = 0
    return result


def curl_command(executable):
    metrics = "%{http_code},%{time_namelookup},%{time_connect},%{time_appconnect},%{time_starttransfer},%{time_total}"
    return [executable, "--disable", "--silent", "--show-error", "--noproxy", "*",
            "--proxy", "", "--proto", "=https", "--proto-redir", "=https",
            "--request", "GET", "--http1.1", "--max-redirs", "0",
            "--connect-timeout", "1", "--max-time", str(REQUEST_SECONDS),
            "--max-filesize", str(BODY_LIMIT), "--header", "Accept: application/json",
            "--user-agent", "NJFU-DualNodeProbe/1", "--write-out", MARKER.decode() + metrics, ENDPOINT]


def decode_response(output):
    try:
        body, encoded = output.rsplit(MARKER, 1)
        values = encoded.decode("ascii").strip().split(",")
        if len(body) > BODY_LIMIT or len(values) != 6 or not re.fullmatch(r"[0-9]{3}", values[0]):
            raise ValueError()
        cumulative = [float(value) for value in values[1:]]
        if not all(finite(value) and value >= 0 for value in cumulative):
            raise ValueError()
        if cumulative != sorted(cumulative) or cumulative[-1] <= 0:
            raise ValueError()
        dns, connect, tls, first, total = cumulative
        result = {"http_status": int(values[0]), "metrics_ms": {
            "dns": round(dns * 1000, 3), "tcp": round((connect - dns) * 1000, 3),
            "tls": round((tls - connect) * 1000, 3), "ttfb": round(first * 1000, 3),
            "total": round(total * 1000, 3)}}
    except (ValueError, UnicodeError):
        return {"public_response_valid": False, "error": "invalid_metrics"}
    if result["http_status"] != 200:
        return {**result, "public_response_valid": False, "error": "unexpected_http_status"}
    try:
        value = json.loads(body)
        if (not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0
                or not isinstance(value.get("data"), list)):
            raise ValueError()
        flags = [row.get("sysValue") for row in value["data"]
                 if isinstance(row, dict) and row.get("sysKey") == "resvCode"]
        if len(flags) != 1 or type(flags[0]) not in (str, int) or str(flags[0]) not in ("0", "1", "2"):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        return {**result, "public_response_valid": False, "error": "invalid_public_response"}
    return {**result, "public_response_valid": True, "public_captcha_flag": str(flags[0])}


def sample_node(node, count=6, interval=10, releases=(), *, release_provider=None):
    if type(count) is not int or not 1 <= count <= 10 or type(interval) is not int or not 10 <= interval <= 60:
        raise ValueError("invalid_sample_parameters")
    releases = sorted(set(("07:00:00", *releases)))
    for trigger in releases:
        release_time(trigger)
    result = base("sample", node)
    result.update(endpoint=ENDPOINT, started_at=time.time(), samples=[], clock_accuracy="not_verified",
                  clock_jump_detected=False, measurement="fresh_curl_public_get")
    result["parameters"] = {"sample_count": count, "interval_seconds": interval,
                            "connect_timeout_seconds": 1, "request_timeout_seconds": REQUEST_SECONDS,
                            "quiet_before_seconds": 300, "quiet_after_seconds": 300, "release_times": releases}
    executable = shutil.which("curl")
    result["curl_version"] = command_info("curl").get("version")
    start_mono, start_wall = time.monotonic(), time.time()
    last_start = None
    for index in range(count):
        if last_start is not None:
            remaining = last_start + interval - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        provider_failed = False
        if release_provider is not None:
            try:
                releases = sorted(set((*releases, *release_provider())))
                for trigger in releases: release_time(trigger)
                result['parameters']['release_times'] = releases
            except Exception:
                provider_failed = True
        # Schedule refresh may wait on SSH. Recheck the actual request start
        # against the full five-second budget after that wait.
        now = time.time()
        last_start = time.monotonic()
        if abs((now - start_wall) - (time.monotonic() - start_mono)) > 2:
            result["clock_jump_detected"] = True
        row = {"at": now, "public_response_valid": False}
        if result["clock_jump_detected"]:
            row["error"] = "clock_jump"
        elif provider_failed or guarded(now, releases):
            row["error"] = "release_window"
        elif not executable:
            row["error"] = "curl_missing"
        else:
            try:
                code, output = bounded_run(curl_command(executable), limit=BODY_LIMIT + 1024)
                if code:
                    row.update(error="curl_failed", curl_exit_code=code)
                else:
                    row.update(decode_response(output))
            except TimeoutError:
                row["error"] = "command_timeout"
            except ValueError:
                row["error"] = "output_limit"
            except OSError:
                row["error"] = "curl_unavailable"
        row["finished_at"] = time.time()
        if abs((row["finished_at"] - start_wall) - (time.monotonic() - start_mono)) > 2:
            result["clock_jump_detected"] = True
            row.update(public_response_valid=False, error="clock_jump")
        result["samples"].append(row)
        if row.get("error") in ("release_window", "clock_jump", "curl_missing", "curl_unavailable"):
            break
    result["finished_at"] = time.time()
    return result


def read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("report_not_regular")
        body = stream.read(REPORT_LIMIT + 1)
    if len(body) > REPORT_LIMIT:
        raise ValueError("report_too_large")
    return json.loads(body)


def compare_reports(reports, now=None):
    result = base("compare")
    result.update(advisory_candidate=None, reason="invalid_evidence", caveats=[
        "Host clock accuracy is not verified.",
        "Public GET performance does not establish booking readiness or send latency."])
    now = time.time() if now is None else now
    try:
        if len(reports) != 2 or {r["node"] for r in reports} != {"mac", "ubuntu"}:
            return result
        for row in reports:
            if (type(row["schema"]) is not int or row["schema"] != 1 or row["kind"] != "sample" or row["probe_version"] != VERSION
                    or row["endpoint"] != ENDPOINT or row.get("clock_jump_detected") is not False
                    or any(row.get(key) != value for key, value in DISCLAIMER.items())):
                return result
            if (not finite(row["finished_at"]) or not finite(row["started_at"])
                    or row["started_at"] > row["finished_at"]):
                return result
            if not -5 <= now - row["finished_at"] <= 180:
                result["reason"] = "stale_or_future_evidence"
                return result
        if reports[0]["parameters"] != reports[1]["parameters"]:
            result["reason"] = "parameters_mismatch"
            return result
        if max(r["started_at"] for r in reports) >= min(r["finished_at"] for r in reports):
            result["reason"] = "nonoverlapping_observations"
            return result
        if abs(reports[0]["started_at"] - reports[1]["started_at"]) > reports[0]["parameters"]["interval_seconds"]:
            result["reason"] = "observation_start_skew"
            return result
        summaries = []
        for row in reports:
            samples, parameters = row["samples"], row["parameters"]
            if (type(parameters["sample_count"]) is not int or not 3 <= parameters["sample_count"] <= 10
                    or not isinstance(samples, list) or len(samples) != parameters["sample_count"]):
                result["reason"] = "insufficient_samples"
                return result
            if (type(parameters["interval_seconds"]) is not int or not 10 <= parameters["interval_seconds"] <= 60
                    or parameters["connect_timeout_seconds"] != 1
                    or parameters["request_timeout_seconds"] != REQUEST_SECONDS
                    or parameters["quiet_before_seconds"] != 300 or parameters["quiet_after_seconds"] != 300):
                return result
            releases = parameters["release_times"]
            if not isinstance(releases, list) or releases != sorted(set(releases)) or "07:00:00" not in releases:
                return result
            for trigger in releases:
                release_time(trigger)
            times = []
            previous_start = None
            for sample in samples:
                if (not finite(sample["at"]) or not finite(sample["finished_at"])
                        or not row["started_at"] <= sample["at"] <= sample["finished_at"] <= row["finished_at"]
                        or sample.get("error") in ("release_window", "clock_jump", "curl_missing", "curl_unavailable")):
                    return result
                if previous_start is not None and sample["at"] - previous_start < parameters["interval_seconds"] - .05:
                    return result
                previous_start = sample["at"]
                if sample.get("public_response_valid") is True:
                    metrics = sample["metrics_ms"]
                    if set(metrics) != {"dns", "tcp", "tls", "ttfb", "total"} or not all(finite(t) and t >= 0 for t in metrics.values()):
                        return result
                    if (sample["http_status"] != 200 or not 0 < metrics["total"] <= (REQUEST_SECONDS + 1) * 1000
                            or metrics["dns"] + metrics["tcp"] + metrics["tls"] > metrics["ttfb"] + .003
                            or metrics["ttfb"] > metrics["total"]
                            or sample.get("public_captcha_flag") not in ("0", "1", "2") or "error" in sample):
                        return result
                    times.append(metrics["total"])
            if len(times) < 3:
                result["reason"] = "insufficient_valid_samples"
                return result
            summaries.append({"node": row["node"], "valid_samples": len(times), "sample_count": len(samples),
                              "success_fraction": len(times) / len(samples),
                              "median_ms": statistics.median(times), "max_ms": max(times)})
        result["summaries"] = summaries
        best, other = sorted(summaries, key=lambda s: (-s["success_fraction"], s["median_ms"], s["max_ms"]))
        if best["success_fraction"] > other["success_fraction"]:
            result.update(advisory_candidate=best["node"], reason="higher_observed_success_fraction")
        elif (other["median_ms"] - best["median_ms"] > max(20, other["median_ms"] * .15)
              and best["max_ms"] <= other["max_ms"]):
            result.update(advisory_candidate=best["node"], reason="lower_observed_latency")
        else:
            result["reason"] = "no_clear_network_preference"
        return result
    except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
        return result


def check_output_path(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError("output_exists")
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("output_parent_symlink")
    return path


def write_report(path, result):
    path = check_output_path(path)
    encoded = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--node", required=True, choices=("mac", "ubuntu"))
    sample = commands.add_parser("sample")
    sample.add_argument("--node", required=True, choices=("mac", "ubuntu"))
    sample.add_argument("--samples", type=int, default=6, choices=range(1, 11))
    sample.add_argument("--interval", type=int, default=10, choices=range(10, 61))
    sample.add_argument("--release-time", action="append", default=[], type=release_time)
    compare = commands.add_parser("compare")
    compare.add_argument("reports", nargs=2)
    for command in (inspect, sample, compare):
        command.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.output:
            check_output_path(args.output)
        if args.command == "inspect":
            result = inspect_node(args.node)
        elif args.command == "sample":
            result = sample_node(args.node, args.samples, args.interval, args.release_time)
        else:
            result = compare_reports([read_report(path) for path in args.reports])
        if args.output:
            write_report(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, RecursionError):
        print(json.dumps({"error": "input_or_output_failed", **DISCLAIMER}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
