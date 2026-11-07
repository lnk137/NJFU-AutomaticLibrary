from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Forced-role RPC identity, persistent write replay and bounded storage."""
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import test_coordinator_ledger as fixtures
from test_coordinator_ledger import NOW, capability, prepared, report, task


class RPCTests(unittest.TestCase):
    def send(self, authority, op, body=None, *, node="mac", role="node", request_id=None):
        from local_app.dual_node.rpc import dispatch
        return dispatch(authority, {"schema": 1, "op": op, "request_id": request_id or uuid.uuid4().hex,
                                  "body": body or {}}, node_id=node, role=role)

    def initial(self, root, clock):
        return fixtures.LedgerTests().initial(root, clock)

    def test_fixed_node_and_publisher_roles_cannot_escalate(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                for op in ("enroll", "activate", "publish", "cancel"):
                    result = self.send(authority, op, {"task": task()})
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["code"], "operation_forbidden")
                result = self.send(authority, "heartbeat", {"boot_id": "mac-boot-01", "seq": 1, "node": "ubuntu"})
                self.assertFalse(result["ok"])
                self.assertFalse(self.send(authority, "activate", {}, node=None, role="publisher")["ok"])
                self.assertTrue(self.send(authority, "tasks", node=None, role="publisher")["ok"])
                self.assertTrue(self.send(authority, "publish", {"task": task()}, node=None, role="publisher")["ok"])
                result = self.send(authority, "report", {"task": task(), "report": report("ubuntu"),
                                                       "readiness": capability("mac")})
                self.assertFalse(result["ok"])

    def test_read_and_presence_requests_do_not_accumulate_durable_replay_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                for index in range(5):
                    self.assertTrue(self.send(authority, "tasks")["ok"])
                    self.assertTrue(self.send(authority, "status", {"task": task()})["ok"])
                    self.assertTrue(self.send(authority, "time", {"nonce": uuid.uuid4().hex})["ok"])
                    self.assertTrue(self.send(authority, "heartbeat", {"boot_id": "mac-boot-01", "seq": index + 1})["ok"])
                self.assertEqual(authority.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_mutating_replay_cache_is_bounded_and_payload_conflicts_refuse(self):
        from local_app.dual_node import coordinator
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                with patch.object(coordinator, "REQUEST_CACHE_LIMIT", 4, create=True):
                    for _ in range(8):
                        self.assertTrue(self.send(authority, "publish", {"task": task()}, node=None, role="publisher")["ok"])
                self.assertLessEqual(authority.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 4)
                request_id = uuid.uuid4().hex
                self.assertTrue(self.send(authority, "publish", {"task": task()}, node=None,
                                          role="publisher", request_id=request_id)["ok"])
                conflict = self.send(authority, "publish", {"task": task() | {"revision": 2}}, node=None,
                                     role="publisher", request_id=request_id)
                self.assertEqual(conflict["code"], "request_id_conflict")

    def test_lost_authorize_reply_replays_same_ticket_but_cancellation_is_current(self):
        helper = fixtures.LedgerTests()
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                helper.reports(authority); authority.select(task()); helper.ready(authority, "mac", clock)
                request_id = uuid.uuid4().hex
                first = self.send(authority, "authorize", {"task": task()}, request_id=request_id)
                self.assertTrue(first["ok"])
                repeated = self.send(authority, "authorize", {"task": task()}, request_id=request_id)
                self.assertEqual(first["result"]["grant"], repeated["result"]["grant"])
                authority.cancel(task())
                cancelled = self.send(authority, "authorize", {"task": task()}, request_id=request_id)
                self.assertFalse(cancelled["ok"])
                self.assertEqual(cancelled["code"], "cancellation_requested")

    def test_invalid_request_id_and_credential_fields_never_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                sentinel = "PRIVATE_" + "S" * 24
                response = self.send(authority, "tasks", request_id=sentinel)
                self.assertNotIn(sentinel, str(response))
                response = self.send(authority, "tasks", {"password": sentinel})
                self.assertNotIn(sentinel, str(response))
                self.assertFalse(response["ok"])

    def test_node_account_scope_and_nonowner_grant_visibility(self):
        helper = fixtures.LedgerTests()
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                reference = "fixture-mac-only"
                authority.enroll(reference, nodes=["mac"], active=True)
                private_task = task() | {"account_ref": reference}
                authority.publish(private_task)
                self.assertFalse(self.send(authority, "status", {"task": private_task}, node="ubuntu")["ok"])
                helper.reports(authority); authority.select(task()); helper.ready(authority, "mac", clock)
                issued = authority.authorize("mac", task())["grant"]
                other = self.send(authority, "status", {"task": task()}, node="ubuntu")
                self.assertTrue(other["ok"])
                self.assertEqual(other["result"]["owner"], "mac")
                self.assertIsNone(other["result"]["grant"])
                self.assertEqual(self.send(authority, "status", {"task": task()})["result"]["grant"], issued)


if __name__ == "__main__": unittest.main()
