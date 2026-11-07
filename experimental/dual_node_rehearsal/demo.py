"""Synthetic local child-process races; never represents two physical hosts."""
from datetime import datetime, timedelta
import multiprocessing
import os
from pathlib import Path
from threading import BrokenBarrierError

from diagnostics import dual_node_probe as probe
from .contracts import RehearsalError, envelope, intent_identity, write_json

_RACE_WORKERS = 4
_RACE_ATTEMPTS = 8
_RACE_TIMEOUT_SECONDS = 20
_BARRIER_TIMEOUT_SECONDS = 5
_RACE_BARRIER = None


def synthetic_case() -> tuple[dict, list[dict], float]:
    day = datetime.now(probe.ZONE).date()
    now = datetime.fromisoformat(str(day) + "T14:00:30").replace(tzinfo=probe.ZONE).timestamp()
    release = now + 600
    release_time = datetime.fromtimestamp(release, probe.ZONE).strftime("%H:%M:%S")
    intent = {"schema": 1, "account_ref": "fixture-user", "day": str(day + timedelta(days=1)),
              "kind": "S", "operation": "-", "plan_version": 1,
              "start_minute": 480, "end_minute": 1320,
              "seat_ref": "fixture-seat-a", "release_at": release}
    reports = []
    for node, total in (("mac", 100.0), ("ubuntu", 220.0)):
        start = now - 21
        samples = [{"at": start + index * 10, "finished_at": start + index * 10 + total / 1000 + .02,
                    "public_response_valid": True, "http_status": 200, "public_captcha_flag": "0",
                    "metrics_ms": {"dns": 1.0, "tcp": 3.0, "tls": 6.0, "ttfb": 60.0, "total": total}}
                   for index in range(3)]
        reports.append({"schema": 1, "probe_version": probe.VERSION, "kind": "sample", "node": node,
                        "observed_at": start, "started_at": start, "finished_at": now - .1,
                        "endpoint": probe.ENDPOINT, "measurement": "fresh_curl_public_get",
                        "clock_jump_detected": False, "clock_accuracy": "not_verified",
                        "authentication": "not_checked", "booking_readiness": "not_checked",
                        "automatic_selection_authorized": False,
                        "evidence_source": "synthetic_fixture", "samples": samples,
                        "parameters": {"sample_count": 3, "interval_seconds": 10,
                                       "connect_timeout_seconds": 1, "request_timeout_seconds": 3,
                                       "quiet_before_seconds": 300, "quiet_after_seconds": 300,
                                       "release_times": sorted({"07:00:00", release_time})}})
    return intent, reports, now


def _assign(payload: tuple) -> dict:
    from .ledger import Authority
    state, intent, proposal, now = payload
    try:
        with Authority(Path(state)) as authority:
            return authority.assign(intent, proposal, now=now)
    except RehearsalError as exc:
        return envelope(status="error", reason=exc.code)


def _consume(payload: tuple) -> dict:
    from .ledger import Authority
    state, assignment, node, now = payload
    try:
        with Authority(Path(state)) as authority:
            return authority.consume(assignment, node=node, now=now)
    except RehearsalError as exc:
        return envelope(status="error", reason=exc.code, simulation_consumed=False)


def _initialize_race(barrier) -> None:
    global _RACE_BARRIER
    _RACE_BARRIER = barrier


def _race_task(task: tuple) -> dict:
    function, payload, attempt = task
    try:
        ticket = _RACE_BARRIER.wait()
    except BrokenBarrierError:
        raise RehearsalError("demo_start_barrier_failed") from None
    row = dict(function(payload))
    row.update(worker_pid=os.getpid(), race_attempt=attempt,
               barrier_batch=attempt // _RACE_WORKERS, barrier_ticket=ticket)
    return row


def _race(function, payloads: list) -> list:
    _require(len(payloads) == _RACE_ATTEMPTS, "demo_race_requires_eight_attempts")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(_RACE_WORKERS, timeout=_BARRIER_TIMEOUT_SECONDS)
    pool = context.Pool(processes=_RACE_WORKERS, initializer=_initialize_race, initargs=(barrier,))
    try:
        # With chunksize=1, each worker must reach the four-party barrier before
        # any can finish its task. Eight tasks make two complete barrier batches.
        tasks = [(function, payload, attempt) for attempt, payload in enumerate(payloads)]
        rows = pool.map_async(_race_task, tasks, chunksize=1).get(timeout=_RACE_TIMEOUT_SECONDS)
        pool.close()
    except BaseException:
        try:
            barrier.abort()
        finally:
            pool.terminate()
        raise
    finally:
        # Success closes the queue; every error/timeout terminates our children.
        # Explicit join completes before a temporary authority can be removed.
        pool.join()
    for start in range(0, _RACE_ATTEMPTS, _RACE_WORKERS):
        participants = rows[start:start + _RACE_WORKERS]
        _require(len({row["worker_pid"] for row in participants}) == _RACE_WORKERS,
                 "demo_worker_participation_failed")
    return rows


def _require(value: bool, reason: str) -> None:
    if not value:
        raise RehearsalError(reason)


def run_demo(directory: Path) -> dict:
    from .ledger import Authority
    from .selection import propose
    if (not directory.is_absolute()
            or any(p.is_symlink() for p in (directory, *directory.parents))):
        raise RehearsalError("unsafe_demo_path")
    directory.mkdir(mode=0o700)
    intent, reports, now = synthetic_case()
    write_json(directory / "intent.json", intent)
    for report in reports:
        write_json(directory / (report["node"] + "-synthetic-samples.json"), report)
    proposal = propose(reports, intent, eligible_nodes=["mac", "ubuntu"], incumbent="mac", now=now)
    _require(proposal.get("status") == "proposed", "demo_policy_refused")
    write_json(directory / "policy-evaluation.json", proposal)
    state = directory / "authority"
    with Authority.initialize(state):
        pass
    # Conflicting eligibility assertions deliberately stress the shared authority.
    # They are fixture assertions, not evidence of either host's school account.
    proposals = [propose(reports, intent, eligible_nodes=[node], incumbent="mac", now=now)
                 for node in ("mac", "ubuntu")]
    assigned = _race(_assign, [(str(state), intent, proposals[index % 2], now) for index in range(8)])
    assignment_process_count = len({row["worker_pid"] for row in assigned})
    created = sum(row.get("created") is True for row in assigned)
    with Authority(state) as authority:
        assignment = authority.inspect(intent_identity(intent)[0])
    _require(created == 1 and assignment is not None, "demo_assignment_race_failed")
    _require(all(row.get("owner") == assignment["owner"] for row in assigned), "demo_owner_diverged")
    write_json(directory / "assignment-race.json", assigned)
    write_json(directory / "assignment.json", assignment)
    consumed = _race(_consume, [(str(state), assignment, node, intent["release_at"] + 1)
                               for node in ("mac", "ubuntu") * 4])
    consumption_process_count = len({row["worker_pid"] for row in consumed})
    successes = sum(row.get("simulation_consumed") is True for row in consumed)
    _require(successes == 1, "demo_consumption_race_failed")
    write_json(directory / "consumption-race.json", consumed)
    with Authority(state) as authority:
        uncertain = authority.finish(assignment, node=assignment["owner"],
                                     status="uncertain", now=intent["release_at"] + 2)
        rival = proposals[0 if assignment["owner"] == "ubuntu" else 1]
        retained = authority.assign(intent, rival, now=intent["release_at"] + 3)
    with Authority(state) as reopened:
        final = reopened.inspect(assignment["task_id"])
    owner_retained = retained["owner"] == assignment["owner"] and retained["state"] == "uncertain"
    restart_preserved = final["decision_id"] == assignment["decision_id"] and final["state"] == "uncertain"
    _require(owner_retained and restart_preserved, "demo_restart_or_transfer_failed")
    report = envelope(status="passed", physical_hosts_used=1, simulated_nodes=["mac", "ubuntu"],
                      virtual_clock=True, virtual_created_at=now, school_requests=0, network_requests=0,
                      policy_selected_node=proposal["selected_node"], race_owner=assignment["owner"],
                      conflict_injected=True, policy_race_owner_may_differ=True,
                      assignment_attempts=len(assigned), assignments_created=created,
                      assignment_process_count=assignment_process_count,
                      consumption_attempts=len(consumed), successful_consumptions=successes,
                      consumption_process_count=consumption_process_count,
                      owner_retained_after_uncertain=owner_retained, restart_preserved=restart_preserved,
                      state=final["state"], authority_scope="one_local_physical_sqlite_ledger",
                      evidence_source="synthetic_fixture",
                      limitations=["Race owner may differ from policy_selected_node because conflicting eligibility is injected.",
                                   "No physical cross-host communication or fencing acceptance.",
                                   "No school authentication, native ARM or booking requests.",
                                   "No cloned/NFS database or power-loss durability claim."])
    write_json(directory / "terminal-assignment.json", uncertain)
    write_json(directory / "acceptance.json", report)
    return report
