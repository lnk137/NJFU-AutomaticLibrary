from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Local SQLite authority: real processes, rollback, replay and clock failures."""
import copy
from datetime import datetime
import importlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK / "AL-BackEnd"))
from test_coordinator_contracts import task

NOW = task()["release_at"] - 600


def report(node, now=NOW, latency=100):
    from local_app.dual_node import public_probe as probe
    release = datetime.fromtimestamp(task()["release_at"], probe.ZONE).strftime("%H:%M:%S")
    start = now - 21
    rows = [{"at": start + i * 10, "finished_at": start + i * 10 + latency / 1000 + .02,
             "public_response_valid": True, "http_status": 200, "public_captcha_flag": "0",
             "metrics_ms": {"dns": 1., "tcp": 3., "tls": 6., "ttfb": 20., "total": latency}}
            for i in range(3)]
    return {"schema": 1, "probe_version": probe.VERSION, "kind": "sample", "node": node,
            "observed_at": start, "started_at": start, "finished_at": rows[-1]["finished_at"],
            "endpoint": probe.ENDPOINT, "measurement": "fresh_curl_public_get", "samples": rows,
            "clock_jump_detected": False, "clock_accuracy": "not_verified", "authentication": "not_checked",
            "booking_readiness": "not_checked", "automatic_selection_authorized": False,
            "evidence_source": "synthetic_fixture", "parameters": {"sample_count": 3,
            "interval_seconds": 10, "connect_timeout_seconds": 1, "request_timeout_seconds": 3,
            "quiet_before_seconds": 300, "quiet_after_seconds": 300,
            "release_times": sorted({"07:00:00", release})}}


def capability(node, now=NOW, seq=1):
    return {"schema": 1, "checked_at": now, "expires_at": now + 45, "credential_revision": 1,
            "credential_present": True, "native_ready": True, "native_binary_sha256": "a" * 64,
            "worker_count": 20, "clock_checked": True, "boot_id": node + "-boot-01", "seq": seq}


def prepared(node, now, value=None, seq=2):
    from local_app.dual_node.contracts import task_identity
    value = value or task(); task_id, binding = task_identity(value)
    return {"schema": 1, "task_id": task_id, "binding_sha256": binding, "node_id": node,
            "credential_revision": 1, "checked_at": now, "expires_at": value["release_at"] + 30,
            "authenticated": True, "identity_matches": True, "payload_protocol": "campus-v1",
            "native_identity": "fixture-native-identity", "native_binary_sha256": "a" * 64,
            "platform": "linux", "durability_profile": "fixture-fsync", "worker_count": 20,
            "clock_checked": True, "boot_id": node + "-boot-01", "seq": seq}


def authorize_worker(root, node, queue):
    from local_app.dual_node.coordinator import Coordinator
    from local_app.dual_node.contracts import DualNodeError
    try:
        with Coordinator(Path(root), clock=lambda: task()["release_at"] - 5) as authority:
            value = authority.authorize(node, task())
            queue.put(value["grant"])
    except DualNodeError as exc:
        queue.put({"error": exc.code})


def crash_worker(root):
    from local_app.dual_node.coordinator import Coordinator
    with Coordinator(Path(root), clock=lambda: NOW + 1) as authority:
        authority._fault = lambda stage: os._exit(91) if stage == "anchor_prepared" else None
        authority.heartbeat("mac", boot_id="mac-boot-01", seq=100)


class LedgerTests(unittest.TestCase):
    def coordinator(self):
        try:
            return importlib.import_module("local_app.dual_node.coordinator").Coordinator
        except ImportError:
            self.fail("candidate server authority is not implemented")

    def initial(self, root, clock):
        authority = self.coordinator().initialize(root, clock=lambda: clock[0])
        authority.enroll("fixture-account", credential_revision=1, active=True)
        authority.publish(task())
        return authority

    def reports(self, authority, now=NOW, nodes=("mac", "ubuntu")):
        for node in nodes:
            authority.heartbeat(node, boot_id=node + "-boot-01", seq=1)
            authority.report(node, task(), report(node, now, 100 if node == "mac" else 220),
                             capability(node, now))

    def ready(self, authority, node, clock):
        clock[0] = task()["release_at"] - 5
        authority.heartbeat(node, boot_id=node + "-boot-01", seq=2)
        authority.ready(node, task(), prepared(node, clock[0]))

    def test_two_processes_replay_one_durable_signed_grant(self):
        from local_app.dual_node.grants import GrantVerifier
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "authority"; clock = [NOW]
            with self.initial(root, clock) as authority:
                self.reports(authority); chosen = authority.select(task())
                self.assertEqual(chosen["owner"], "mac")
                self.ready(authority, "mac", clock)
                public_key, identity = authority.public_key, authority.authority_id
            context = multiprocessing.get_context("spawn"); queue = context.Queue()
            workers = [context.Process(target=authorize_worker, args=(str(root), "mac", queue)) for _ in range(2)]
            for worker in workers: worker.start()
            results = [queue.get(timeout=15) for _ in workers]
            for worker in workers: worker.join(15); self.assertEqual(worker.exitcode, 0)
            self.assertEqual(results[0], results[1])
            verified = GrantVerifier(public_key, mode="fixture", authority_id=identity).verify_grant(
                results[0], node_id="mac", task=task())
            self.assertEqual(verified.node_id, "mac")
            with self.coordinator()(root, clock=lambda: clock[0] + 100) as reopened:
                self.assertEqual(reopened.authorize("mac", task())["grant"], results[0])
                with self.assertRaises(ValueError): reopened.authorize("ubuntu", task())
                reopened.result("mac", task(), grant_id=verified.grant_id, status="uncertain")
                self.assertEqual(reopened.select(task())["owner"], "mac")
                self.assertEqual(reopened.tasks("mac")["tasks"], [task()])

    def test_single_healthy_node_and_pregrant_failure_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                self.reports(authority)
                self.assertEqual(authority.select(task())["owner"], "mac")
                self.assertEqual(authority.prepare_failed("mac", task(), code="preparation_failed")["owner"], "ubuntu")
                self.ready(authority, "ubuntu", clock)
                self.assertIsNotNone(authority.authorize("ubuntu", task())["grant"])
                with self.assertRaises(ValueError):
                    authority.prepare_failed("ubuntu", task(), code="preparation_failed")
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                self.reports(authority, nodes=("ubuntu",))
                self.assertEqual(authority.select(task())["owner"], "ubuntu")

    def test_backdated_report_with_fresh_header_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
                old = report("mac", NOW - 3600); old["finished_at"] = NOW
                with self.assertRaises(ValueError):
                    authority.report("mac", task(), old, capability("mac"))
                self.assertIsNone(authority.select(task())["owner"])

    def test_unbounded_metrics_fail_with_a_fixed_protocol_error(self):
        from local_app.dual_node.contracts import DualNodeError
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
                invalid = report("mac")
                invalid["samples"][0]["metrics_ms"]["total"] = 10 ** 400
                with self.assertRaises(DualNodeError):
                    authority.report("mac", task(), invalid, capability("mac"))

    def test_boot_seq_and_credential_revision_are_not_caller_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                authority.heartbeat("mac", boot_id="mac-boot-01", seq=2)
                with self.assertRaises(ValueError):
                    authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
                bad = capability("mac", seq=3); bad["credential_revision"] = 2
                with self.assertRaises(ValueError):
                    authority.report("mac", task(), report("mac"), bad)
                authority.report("mac", task(), report("mac"), capability("mac", seq=3))
                authority.select(task())
                authority.heartbeat("mac", boot_id="mac-boot-02", seq=1)
                with self.assertRaises(ValueError):
                    authority.heartbeat("mac", boot_id="mac-boot-01", seq=100)
                clock[0] = task()["release_at"] - 5
                with self.assertRaises(ValueError):
                    authority.ready("mac", task(), prepared("mac", clock[0], seq=4))

    def test_plan_revision_changes_before_grant_only_and_cancel_never_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                changed = task() | {"revision": 2, "seat_priorities": ["3424"]}
                authority.publish(changed)
                with self.assertRaises(ValueError): authority.status(task())
            # Re-open a separate task to keep the authorization fixture clear.
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                self.reports(authority); authority.select(task()); self.ready(authority, "mac", clock)
                issued = authority.authorize("mac", task())["grant"]
                cancelled = authority.cancel(task())
                self.assertEqual(cancelled["owner"], "mac")
                self.assertEqual(cancelled["grant"], issued)
                with self.assertRaises(ValueError): authority.publish(task() | {"revision": 2})
                self.assertEqual(authority.select(task())["owner"], "mac")

    def test_clock_highwater_survives_rejected_request_and_database_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "authority"; clock = [NOW]
            with self.initial(root, clock) as authority:
                before = (root / "state.db").read_bytes()
                clock[0] += 100
                with self.assertRaises(ValueError):
                    authority.report("mac", task(), {}, {})
                clock[0] -= 1
                with self.assertRaises(ValueError) as caught: authority.status(task())
                self.assertIn("clock", str(caught.exception))
                clock[0] += 2
                authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
            (root / "state.db").write_bytes(before)
            with self.assertRaises(ValueError):
                self.coordinator()(root, clock=lambda: clock[0])

    def test_crash_between_anchor_and_commit_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "authority"; clock = [NOW]
            with self.initial(root, clock): pass
            worker = multiprocessing.get_context("spawn").Process(target=crash_worker, args=(str(root),))
            worker.start(); worker.join(15)
            self.assertEqual(worker.exitcode, 91)
            with self.assertRaises(ValueError):
                self.coordinator()(root, clock=lambda: NOW + 2)

    def test_production_never_accepts_clock_injection_or_unhealthy_server_clock(self):
        Coordinator = self.coordinator()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                Coordinator.initialize(Path(directory).resolve() / "authority", mode="production", clock=lambda: NOW)

    def test_verified_terminal_result_survives_mac_offline_until_publisher_ack(self):
        from local_app.dual_node.contracts import canonical_hash
        from local_app.dual_node.rpc import dispatch
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                self.assertIn("verified", inspect.signature(authority.result).parameters)
                self.reports(authority); authority.select(task()); self.ready(authority, "mac", clock)
                grant = authority.authorize("mac", task())["grant"]
                grant_id = grant["payload"]["grant_id"]
                for fields in ({}, {"verified": False}, {"verified": True, "receipt_ref": "bad", "seat_index": 0, "attempt_count": 1},
                               {"verified": True, "receipt_ref": "e" * 64, "seat_index": True, "attempt_count": 1}):
                    with self.subTest(fields=fields), self.assertRaises(ValueError):
                        authority.result("mac", task(), grant_id=grant_id, status="completed", **fields)
                result = authority.result("mac", task(), grant_id=grant_id, status="completed", verified=True,
                                          receipt_ref="e" * 64, seat_index=1, attempt_count=2)
                self.assertEqual(result["result"]["receipt_ref"], "e" * 64)
                clock[0] = task()["release_at"] + 10 * 86400
                self.assertEqual(authority.tasks("mac")["tasks"], [])
                self.assertEqual(authority.tasks(include_unmirrored=True)["tasks"], [task()])
                request = {"schema": 1, "request_id": "f" * 32, "op": "status", "body": {"task": task()}}
                self.assertTrue(dispatch(authority, request, role="publisher")["ok"])
                request = {"schema": 1, "request_id": "1" * 32, "op": "result_ack",
                    "body": {"task": task(), "grant_id": grant_id, "result_sha256": canonical_hash(result["result"])}}
                self.assertFalse(dispatch(authority, request, node_id="mac")["ok"])
                self.assertTrue(dispatch(authority, request, role="publisher")["ok"])
                self.assertEqual(authority.tasks(include_unmirrored=True)["tasks"], [])
                persisted = authority.status(task())
                self.assertTrue(persisted["mirror_ack"])
                self.assertEqual(persisted["grant"], grant)
                self.assertEqual(persisted["owner"], "mac")


if __name__ == "__main__": unittest.main()
