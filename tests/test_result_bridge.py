from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Real private Mongo fixture: bridge proof, restart and old-history preservation."""
import copy
from dataclasses import asdict
from datetime import date, datetime, timedelta
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import tempfile

WORK = Path(__file__).resolve().parents[1]
PROJECT = WORK
sys.path.insert(0, str(PROJECT / "tests"))
sys.path.insert(0, str(WORK / "AL-BackEnd"))
from support.mongo_fixture import MongoFixture
from local_app.models import RunSpec, ZONE, Seat
from local_app.store import LocalStore
from local_app.dual_node.contracts import canonical_hash, task_identity
from test_coordinator_contracts import task


def status(value, phase="completed", **changes):
    task_id, binding = task_identity(value)
    grant = {"schema": 1, "domain": "njfu-dual-node-fixture-v1",
             "signature": "-----BEGIN SSH SIGNATURE-----\nfixture\n-----END SSH SIGNATURE-----\n",
             "payload": {"kind": "grant", "authority_id": "a" * 32, "node_id": "ubuntu",
                         "grant_id": "b" * 32, "task_id": task_id, "binding_sha256": binding,
                         "release_at": value["release_at"], "expires_at": value["release_at"] + 30,
                         "issued_at": value["release_at"] - 5, "credential_revision": value["credential_revision"]}}
    result = {"status": phase, "verified": phase == "completed", "attempt_count": 2,
              "result_at": value["release_at"] + 2}
    if phase == "completed": result.update(receipt_ref="e" * 64, seat_index=1)
    row = {"mode": "fixture", "authority_id": "a" * 32, "task_id": task_id, "binding_sha256": binding,
           "phase": phase, "owner": "ubuntu", "grant": grant, "result": result,
           "mirror_ack": False, "cancel_requested": False, "reason": "owner_result"}
    row.update(changes)
    return row


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.store = LocalStore(self.mongo.database())
        self.store.accounts.insert_one({"_id": "local-one", "revision": 1, "credential_revision": 1})
        self.gate = Mock(); self.gate.is_managed.return_value = True
        self.registrations = {"local-one": {"account_ref": "fixture-account", "credential_revision": 1}}
        self.clock = [task()["release_at"] + 3]

    def bridge(self):
        try: module = importlib.import_module("local_app.dual_node.result_bridge")
        except ImportError: self.fail("result bridge is not implemented")
        return module.ResultBridge(self.store, self.registrations, self.gate,
                                   mode="fixture", clock=lambda: self.clock[0])

    def test_verified_completion_writes_standard_dual_history_without_touching_old_history(self):
        old = RunSpec("local-one", 1, task()["day"], "08:00", "22:00",
                      tuple(task()["seat_priorities"]), task()["release_at"])
        self.store.ensure_run(old)
        self.store.runs.update_one({"_id": old.key}, {"$set": {"status": "success", "native_binding": {"old": True},
                                                             "result": {"message": "preserve old success"}}})
        original = self.store.get_run(old.key)
        with patch("local_app.school.CampusClient", side_effect=AssertionError("school network forbidden")):
            ack = self.bridge().apply(task(), status(task()))
        self.assertEqual(self.store.get_run(old.key), original)
        rows = list(self.store.runs.find({"dual_binding": {"$exists": True}}))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotEqual(row["_id"], old.key)
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["attempted_seat_id"], "3423")
        self.assertIn("Ubuntu", row["message"])
        self.assertEqual(row["result"]["records"], [])
        self.assertEqual(row["result"]["receipt_ref"], "e" * 64)
        self.assertEqual(ack["result_sha256"], canonical_hash(status(task())["result"]))
        self.assertEqual(self.store.list_runs("local-one", task()["day"]).counts["success"], 2)
        self.assertEqual(self.bridge().apply(task(), status(task())), ack)

    def test_invalid_proof_and_unknown_binding_do_not_mutate_or_ack(self):
        bridge = self.bridge(); invalid = status(task()); invalid["result"]["verified"] = False
        with self.assertRaises(ValueError): bridge.apply(task(), invalid)
        self.assertEqual(self.store.runs.count_documents({}), 0)
        bridge.apply(task(), status(task()))
        row = self.store.runs.find_one({"dual_binding": {"$exists": True}})
        self.store.runs.update_one({"_id": row["_id"]}, {"$set": {"dual_binding.binding_sha256": "0" * 64}})
        before = self.store.get_run(row["_id"])
        with self.assertRaises(ValueError): bridge.apply(task(), status(task()))
        self.assertEqual(self.store.get_run(row["_id"]), before)

    def test_recovery_uncertainty_is_never_requeued_and_success_never_downgraded(self):
        bridge = self.bridge()
        ready = status(task(), "ready", result=None, grant=None)
        bridge.apply(task(), ready)
        row = self.store.runs.find_one({"dual_binding": {"$exists": True}})
        self.store.runs.update_one({"_id": row["_id"]}, {"$set": {"status": "uncertain"}})
        bridge.apply(task(), ready)
        self.assertEqual(self.store.get_run(row["_id"])["status"], "uncertain")
        bridge.apply(task(), status(task()))
        bridge.apply(task(), status(task(), "uncertain"))
        self.assertEqual(self.store.get_run(row["_id"])["status"], "success")

    def test_missing_registration_restarts_from_scope_and_ack_only_after_durable_write(self):
        bridge = self.bridge(); bridge.record_scope(task(), status(task(), "selected", grant=None, result=None))
        self.registrations.clear()
        resumed = self.bridge()
        ack = resumed.apply(task(), status(task()))
        calls = []
        class Client:
            def call(self, operation, **body):
                calls.append(operation)
                if operation == "result_ack": return status(task(), mirror_ack=True)
                return status(task())
        result = resumed.sync(Client(), [task()])
        self.assertEqual(calls, ["result_ack"])
        self.assertEqual(result["acknowledged"], 1)
        self.assertEqual(resumed.sync(Client(), [task()])["requests"], 0)
        self.assertEqual(ack["grant_id"], "b" * 32)

    def test_future_inventory_does_not_create_status_poll_storm(self):
        self.clock[0] = task()["release_at"] - 3600
        bridge = self.bridge()
        known = []
        for offset in range(20):
            value = task() | {"day": (date(2026, 11, 7) + timedelta(days=offset)).isoformat()}
            bridge.record_scope(value, status(value, "collecting", grant=None, result=None))
            known.append(value)
        client = Mock()
        self.assertEqual(bridge.sync(client, known)["requests"], 0)
        client.call.assert_not_called()

    def test_ungranted_result_recovers_from_scope_after_server_inventory_expires(self):
        bridge = self.bridge()
        bridge.record_scope(task(), status(task(), 'selected', grant=None, result=None))
        self.clock[0] = task()['release_at'] + 306
        client = Mock(); client.call.return_value = status(task(), 'expired_unissued', grant=None, result=None)
        report = self.bridge().sync(client, [])
        self.assertEqual(report['polled'], 1)
        client.call.assert_called_once_with('status', task=task())
        row = self.store.runs.find_one({'dual_binding': {'$exists': True}})
        self.assertEqual(row['status'], 'skipped')
        self.assertTrue(self.store.db.campus_dual_task_scope.find_one({})['retired'])
        client.reset_mock()
        self.assertEqual(self.bridge().sync(client, [])['requests'], 0)
        client.call.assert_not_called()

    def test_scope_recovery_is_bounded_and_a_failed_old_scope_does_not_starve_others(self):
        bridge = self.bridge(); values = []
        for index in range(6):
            value = task() | {'day': (date(2026, 11, 7)+timedelta(days=index)).isoformat()}
            bridge.record_scope(value, status(value, 'selected', grant=None, result=None)); values.append(value)
        self.clock[0] = task()['release_at'] + 306
        seen = []
        def call(op, **body):
            self.assertEqual(op, 'status'); seen.append(body['task']['day'])
            raise OSError('isolated unavailable status')
        client = Mock(); client.call.side_effect = call
        self.assertEqual(bridge.sync(client, [], max_polls=2)['requests'], 2)
        self.assertEqual(bridge.sync(client, [], max_polls=2)['requests'], 2)
        self.assertEqual(len(set(seen)), 4)

    def test_failed_pending_acknowledgements_do_not_starve_expired_scope_recovery(self):
        for budget in (1, 2):
            with self.subTest(budget=budget):
                self.store.runs.delete_many({});self.store.db.campus_dual_task_scope.delete_many({})
                bridge = self.bridge()
                for index in range(4):
                    value = task() | {'day': (date(2026, 11, 10)+timedelta(days=index)).isoformat()}
                    bridge.apply(value, status(value))
                bridge.record_scope(task(), status(task(), 'selected', grant=None, result=None))
                self.clock[0] = task()['release_at'] + 306
                seen = []
                def call(op, **body):
                    seen.append(op)
                    if op == 'result_ack': raise OSError('isolated acknowledgement unavailable')
                    return status(body['task'], 'expired_unissued', grant=None, result=None)
                client = Mock(); client.call.side_effect = call
                for _ in range(3):
                    self.assertLessEqual(bridge.sync(client, [], max_polls=budget)['requests'], budget)
                self.assertIn('status', seen)
                self.assertTrue(self.store.db.campus_dual_task_scope.find_one({'task.day': task()['day']})['retired'])

    def test_invalid_scope_rotates_without_being_sent_or_retired(self):
        bridge = self.bridge(); old = task() | {'day':'2026-11-10'}
        bridge.record_scope(old, status(old, 'selected', grant=None, result=None))
        bridge.record_scope(task(), status(task(), 'selected', grant=None, result=None))
        self.store.db.campus_dual_task_scope.update_one({'task.day':old['day']},
            {'$set':{'binding.binding_sha256':'0'*64,'last_polled_at':0}})
        self.store.db.campus_dual_task_scope.update_one({'task.day':task()['day']}, {'$set':{'last_polled_at':1}})
        self.clock[0] = task()['release_at']+306
        client=Mock();client.call.return_value=status(task(),'expired_unissued',grant=None,result=None)
        first=bridge.sync(client,[],max_polls=1)
        self.assertEqual(first['blocked'],1);client.call.assert_not_called()
        second=bridge.sync(client,[],max_polls=1)
        self.assertEqual(second['polled'],1)
        client.call.assert_called_once_with('status',task=task())
        broken=self.store.db.campus_dual_task_scope.find_one({'task.day':old['day']})
        self.assertFalse(broken['retired'])
        self.assertEqual(broken['binding']['binding_sha256'],'0'*64)

    def test_malformed_pending_ack_is_isolated_before_work_queue_sorting(self):
        for malformed in ({'$unset':{'task.release_at':''}}, {'$set':{'task.release_at':'invalid'}}):
            with self.subTest(malformed=malformed):
                self.store.runs.delete_many({});self.store.db.campus_dual_task_scope.delete_many({})
                bridge=self.bridge();bad=task()|{'day':'2026-11-10'}
                bridge.apply(bad,status(bad))
                self.store.db.campus_dual_task_scope.update_one({'task.day':bad['day']},malformed)
                bridge.record_scope(task(),status(task(),'selected',grant=None,result=None))
                self.clock[0]=task()['release_at']+306
                client=Mock();client.call.return_value=status(task(),'expired_unissued',grant=None,result=None)
                report=bridge.sync(client,[],max_polls=2)
                self.assertEqual(report['blocked'],1)
                self.assertEqual(report['polled'],1)
                client.call.assert_called_once_with('status',task=task())
                broken=self.store.db.campus_dual_task_scope.find_one({'task.day':bad['day']})
                self.assertEqual(broken['ack_last_attempt_at'],self.clock[0])
                self.assertFalse(broken['retired']);self.assertFalse(broken['ack_confirmed'])

    def test_failed_history_write_never_acknowledges_the_server_result(self):
        bridge = self.bridge()
        self.assertEqual(bridge.runs.write_concern.document, {"j": True, "w": 1})
        bridge.record_scope(task(), status(task(), "selected", grant=None, result=None))
        client = Mock(); client.call.return_value = status(task())
        original = bridge.runs.update_one
        def failed(query, update, **options):
            if "$set" in update and update["$set"].get("status") == "success":
                raise OSError("synthetic journal failure")
            return original(query, update, **options)
        with patch.object(bridge.runs, "update_one", side_effect=failed):
            report = bridge.sync(client, [task()])
        self.assertEqual(report["blocked"], 1)
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["status"])
        self.assertEqual(self.store.runs.count_documents({"status": "success"}), 0)

    def test_only_known_unissued_dual_binding_can_follow_a_plan_revision(self):
        bridge = self.bridge()
        bridge.apply(task(), status(task(), "ready", result=None, grant=None))
        revised = task() | {"revision": 2, "seat_priorities": ["3423", "3422"]}
        bridge.apply(revised, status(revised, "ready", result=None, grant=None))
        row = self.store.runs.find_one({"dual_binding": {"$exists": True}})
        self.assertEqual(row["spec"]["revision"], 2)
        self.assertEqual(row["dual_binding"]["binding_sha256"], task_identity(revised)[1])
        self.assertEqual(self.store.runs.count_documents({}), 1)
        self.store.runs.update_one({"_id": row["_id"]}, {"$set": {"status": "uncertain"}})
        with self.assertRaises(ValueError):
            bridge.apply(revised | {"revision": 3}, status(revised | {"revision": 3}, "ready", result=None, grant=None))

    def test_changed_history_between_durable_write_and_ack_does_not_ack(self):
        bridge = self.bridge(); bridge.apply(task(), status(task()))
        row = self.store.runs.find_one({"dual_binding": {"$exists": True}})
        self.store.runs.update_one({"_id": row["_id"]}, {"$set": {"dual_binding.binding_sha256": "0" * 64}})
        client = Mock(); client.call.return_value = status(task(), mirror_ack=True)
        report = bridge.sync(client, [task()])
        self.assertGreater(report["blocked"], 0)
        self.assertNotIn("result_ack", [call.args[0] for call in client.call.call_args_list])

    def test_unissued_terminal_status_is_locally_retired_across_restart_without_fake_receipt(self):
        for phase in ("cancelled", "expired_unissued"):
            with self.subTest(phase=phase):
                value = task() | {"day": "2026-11-09" if phase == "cancelled" else "2026-11-10"}
                bridge = self.bridge()
                bridge.record_scope(value, status(value, "selected", grant=None, result=None))
                self.assertIsNone(bridge.apply(value, status(value, phase, grant=None, result=None)))
                row = self.store.runs.find_one({"dual_task.day": value["day"]})
                self.assertEqual(row["status"], "skipped")
                self.assertIsNone(row["dual_result"])
                client = Mock()
                self.assertEqual(self.bridge().sync(client, [value])["requests"], 0)
                client.call.assert_not_called()

    def test_accepted_higher_binding_retires_only_previous_unissued_scope(self):
        bridge = self.bridge()
        bridge.record_scope(task(), status(task(), "collecting", grant=None, result=None))
        changed = task() | {"revision": 2}
        bridge.record_scope(changed, status(changed, "collecting", grant=None, result=None))
        client = Mock()
        self.assertEqual(self.bridge().sync(client, [task()])["requests"], 0)
        client.call.assert_not_called()
        current = self.store.db.campus_dual_task_scope.find_one({"binding.binding_sha256": task_identity(changed)[1]})
        self.assertFalse(current.get("retired", False))


class PowerBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def test_managed_dual_success_releases_power_without_overwriting_old_rows(self):
        from local_app.power import PowerService, decide
        release = datetime(2026, 11, 7, 7, tzinfo=ZONE).timestamp()
        value = task() | {"release_at": release, "day": "2026-11-08"}
        store = LocalStore(self.mongo.database())
        plan = {"seat_ids": ["3422", "3423"], "day_offset": 1, "mode": "day", "begin": "08:00",
                "end": "22:00", "trigger": "07:00:00", "enabled": True}
        store.accounts.insert_one({"_id": "local-one", "revision": 1, "credential_revision": 1,
            "student_id": "fixture-student", "alias": "Fixture", "verified_at": "fixture", "plan": plan})
        gate = Mock(); gate.is_managed.return_value = True; store.managed_gate = gate
        old = RunSpec("local-one", 1, value["day"], "08:00", "22:00",
                      tuple(value["seat_priorities"]), release)
        store.ensure_run(old); store.runs.update_one({"_id": old.key}, {"$set": {"status": "skipped"}})
        response = status(value); response["mode"] = "production"
        response["grant"]["domain"] = "njfu-dual-node-production-v1"
        bridge = importlib.import_module("local_app.dual_node.result_bridge").ResultBridge(
            store, {"local-one": {"account_ref": value["account_ref"], "credential_revision": 1}}, gate)
        bridge.apply(value, response)
        scheduler = Mock(); scheduler.status.return_value = {"scheduler_running": True, "clock_ok": True,
            "allow_live": True, "last_error": None, "scheduler_warning": None}
        catalog = {seat: Seat(seat, seat, "fixture") for seat in value["seat_priorities"]}
        with tempfile.TemporaryDirectory() as directory:
            service = PowerService(SimpleNamespace(data_dir=Path(directory)), SimpleNamespace(busy={}),
                                   store, scheduler, catalog)
            before = list(store.runs.find())
            snapshot = service.snapshot(release + 360, 100.)
            self.assertIsNone(snapshot["error"])
            self.assertEqual(snapshot["expected"], ["success"])
            self.assertFalse(decide(release + 360, 100., {"enabled": True}, snapshot).hold)
            self.assertEqual(list(store.runs.find()), before)
            gate.is_managed.return_value = False
            self.assertIn("skipped", service.snapshot(release + 360, 100.)["expected"])


class PublisherBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def test_auth_window_precedes_publish_and_removed_mapping_uses_scope_inventory(self):
        import json
        from local_app.dual_node.publisher import PlanPublisherLoop
        release = datetime(2026, 11, 7, 7, tzinfo=ZONE).timestamp()
        store = LocalStore(self.mongo.database())
        store.accounts.insert_one({"_id": "local-one", "revision": 1, "credential_revision": 1,
            "student_id": "fixture-student", "alias": "Fixture", "verified_at": "fixture",
            "plan": {"seat_ids": ["3422", "3423"], "day_offset": 1, "mode": "day", "begin": "08:00",
                     "end": "22:00", "trigger": "07:00:00", "enabled": True}})
        gate = Mock(); gate.is_managed.return_value = True
        events, published, known = [], [], []
        gate.remember_auth_window.side_effect = lambda account, due: events.append(("remember", account, due))
        class Client:
            def call(self, operation, **body):
                if operation == "tasks": return {"tasks": list(known), "next_cursor": None}
                if operation == "publish":
                    self_test.assertEqual(events[-1], ("remember", "local-one", body["task"]["release_at"]))
                    events.append(("publish",)); published.append(body["task"])
                    value = status(body["task"], "collecting", grant=None, result=None); value["mode"] = "production"
                    return value
                return {}
        self_test = self
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher.json"
            config = {"schema": 1, "enabled": True, "ssh_config": "/private/fixture-ssh", "ssh_host": "fixture",
                      "registrations": {"local-one": {"account_ref": "fixture-account", "credential_revision": 1}}, "days": 1}
            path.write_text(__import__("json").dumps(config)); path.chmod(0o600)
            runtime = SimpleNamespace(store=store, managed_execution=gate,
                accounts=SimpleNamespace(catalog={seat: Seat(seat, seat, "fixture") for seat in ("3422", "3423")}))
            loop = PlanPublisherLoop(runtime, path)
            loop.client, loop.client_identity = Client(), (config["ssh_config"], config["ssh_host"])
            with patch("time.time", return_value=release - 600): loop.sync_once()
            self.assertEqual(loop.state["status"], "synchronized")
            self.assertEqual(len(published), 1)
            self.assertEqual(store.db.campus_dual_task_scope.count_documents({}), 1)
            self.assertEqual(store.runs.count_documents({}), 0)
            known[:] = published
            config["registrations"] = {}
            path.write_text(json.dumps(config)); path.chmod(0o600)
            events.clear()
            with patch("time.time", return_value=release - 500): loop.sync_once()
            self.assertIn(("remember", "local-one", release), events)

    def test_auth_window_failure_blocks_publish_without_hiding_the_error(self):
        import json
        from local_app.dual_node.publisher import PlanPublisherLoop
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher.json"
            config = {"schema": 1, "enabled": True, "ssh_config": "/private/fixture-ssh", "ssh_host": "fixture",
                      "registrations": {"local-one": {"account_ref": "fixture-account", "credential_revision": 1}}, "days": 1}
            path.write_text(json.dumps(config)); path.chmod(0o600)
            account = SimpleNamespace(id="local-one", revision=1, credential_revision=1, date_exceptions={},
                plan={"seat_ids": ["3422"], "day_offset": 1, "mode": "day", "begin": "08:00",
                      "end": "22:00", "trigger": "07:00:00", "enabled": True})
            store = Mock(); store.list_accounts.return_value = [account]
            gate = Mock(); gate.is_managed.return_value = True
            gate.remember_auth_window.side_effect = ValueError("fixture window unavailable")
            runtime = SimpleNamespace(store=store, managed_execution=gate,
                accounts=SimpleNamespace(catalog={"3422": Seat("3422", "seat", "fixture")}))
            loop = PlanPublisherLoop(runtime, path)
            client = Mock(); client.call.return_value = {"tasks": [], "next_cursor": None}
            loop.client, loop.client_identity = client, (config["ssh_config"], config["ssh_host"])
            release = datetime(2026, 11, 7, 7, tzinfo=ZONE).timestamp()
            with patch("time.time", return_value=release - 600): loop.sync_once()
            self.assertEqual(loop.state["status"], "blocked")
            self.assertNotIn("publish", [call.args[0] for call in client.call.call_args_list])


if __name__ == "__main__": unittest.main()
