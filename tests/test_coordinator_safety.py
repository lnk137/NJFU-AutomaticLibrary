from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Deadline I/O, live-domain clock gates, pagination and global quiet windows."""
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_coordinator_ledger as fixtures
from test_coordinator_ledger import NOW, capability, prepared, report, task


class SafetyTests(unittest.TestCase):
    def initial(self, root, clock):
        return fixtures.LedgerTests().initial(root, clock)

    def test_slow_signature_cannot_commit_authorization_after_release(self):
        helper = fixtures.LedgerTests()
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                helper.reports(authority); authority.select(task()); helper.ready(authority, "mac", clock)
                original = authority.signer.sign_grant
                def delayed(payload):
                    signed = original(payload); clock[0] = task()["release_at"] + 1
                    return signed
                with patch.object(authority.signer, "sign_grant", side_effect=delayed):
                    with self.assertRaises(ValueError): authority.authorize("mac", task())
                self.assertIsNone(authority.status(task())["grant"])
                clock[0] -= .5
                with self.assertRaises(ValueError): authority.status(task())

    def test_in_process_database_restore_and_physical_corruption_fail_closed(self):
        for damage in ("restore", "header"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / "authority"; clock = [NOW]
                with self.initial(root, clock) as authority:
                    before = (root / "state.db").read_bytes()
                    authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
                    replacement = before if damage == "restore" else b"not a database!!" + (root / "state.db").read_bytes()[16:]
                    (root / "state.db").write_bytes(replacement)
                    with self.assertRaises(ValueError): authority.status(task())

    def test_task_listing_is_paged_and_keeps_recent_terminal_quiet_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [NOW]
            with self.initial(Path(directory).resolve() / "authority", clock) as authority:
                expected = [task()]
                for index in range(35):
                    reference = "fixture-page-" + str(index)
                    authority.enroll(reference, active=True)
                    value = task() | {"account_ref": reference}
                    authority.publish(value); expected.append(value)
                found, after = [], None
                while True:
                    page = authority.tasks("mac", after=after, limit=8)
                    self.assertLessEqual(len(page["tasks"]), 8)
                    found.extend(page["tasks"]); after = page["next_cursor"]
                    if after is None: break
                self.assertEqual(len(found), len(expected))
                self.assertEqual({v["account_ref"] for v in found}, {v["account_ref"] for v in expected})
                with self.assertRaises(ValueError): authority.tasks("mac", limit=True)
                with self.assertRaises(ValueError): authority.tasks("mac", after="bad")
                authority.cancel(task())
                clock[0] = task()["release_at"] + 300
                self.assertIn(task(), self.all_tasks(authority))
                clock[0] += 6
                self.assertNotIn(task(), self.all_tasks(authority))

    @staticmethod
    def all_tasks(authority):
        values, after = [], None
        while True:
            page = authority.tasks(after=after); values.extend(page["tasks"])
            after = page["next_cursor"]
            if after is None: return values

    def production(self, root, clock, health):
        from local_app.dual_node.coordinator import Coordinator
        key = root.parent / "test-production-key"
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       capture_output=True, check=True, timeout=10)
        authority = Coordinator.initialize(root, key, mode="production", time_health=lambda: {
            "synchronized": health[0], "checked_at": clock[0], "uncertainty_ms": 1.0})
        value = task() | {"account_ref": "acct-production-test"}
        self.activate(authority, value["account_ref"])
        authority.publish(value)
        return authority, value

    @staticmethod
    def activate(authority, reference):
        authority.enroll(reference, native_hashes={"mac": "a" * 64, "ubuntu": "a" * 64})
        authority.activate(reference, drain_ack={"schema": 1, "account_ref": reference,
            "legacy_drained": True, "state_ack_sha256": "d" * 64})

    def test_production_time_and_grant_require_current_server_clock_health(self):
        from local_app.dual_node import coordinator
        with tempfile.TemporaryDirectory() as directory:
            clock, health = [NOW], [False]
            with patch.object(coordinator.time, "time", side_effect=lambda: clock[0]):
                with self.production(Path(directory).resolve() / "authority", clock, health) [0] as authority:
                    value = task() | {"account_ref": "acct-production-test"}
                    with self.assertRaises(ValueError): authority.time("mac", "c" * 32)
                    self.assertTrue(authority.inspect_status(value)["operator_paused"])
                    with self.assertRaises(ValueError): authority.control(action="resume")
                    health[0] = True
                    authority.control(action="resume")
                    health[0] = False
                    clock[0] = NOW + 260
                    authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
                    raw = report("mac", clock[0]); raw.pop("evidence_source")
                    authority.report("mac", value, raw, capability("mac", clock[0]))
                    clock[0] = value["release_at"] - 305
                    self.assertEqual(authority.select(value)["owner"], "mac")
                    clock[0] = value["release_at"] - 5
                    authority.heartbeat("mac", boot_id="mac-boot-01", seq=2)
                    authority.ready("mac", value, prepared("mac", clock[0], value))
                    with self.assertRaises(ValueError): authority.authorize("mac", value)
                    self.assertIsNone(authority.status(value)["grant"])
                    health[0] = True
                    self.assertIsNotNone(authority.authorize("mac", value)["grant"])

    def test_production_report_declares_every_intersecting_published_release(self):
        from local_app.dual_node import coordinator, public_probe
        with tempfile.TemporaryDirectory() as directory:
            clock, health = [NOW], [True]
            with patch.object(coordinator.time, "time", side_effect=lambda: clock[0]):
                with self.production(Path(directory).resolve() / "authority", clock, health)[0] as authority:
                    value = task() | {"account_ref": "acct-production-test"}
                    self.activate(authority, "acct-other-test")
                    other = value | {"account_ref": "acct-other-test", "release_at": NOW + 400}
                    authority.publish(other)
                    clock[0] = NOW + 260
                    authority.heartbeat("mac", boot_id="mac-boot-01", seq=1)
                    raw = report("mac", clock[0]); raw.pop("evidence_source")
                    with self.assertRaises(ValueError) as caught:
                        authority.report("mac", value, raw, capability("mac", clock[0]))
                    self.assertEqual(str(caught.exception), "release_schedule_mismatch")
                    trigger = datetime.fromtimestamp(other["release_at"], public_probe.ZONE).strftime("%H:%M:%S")
                    raw["parameters"]["release_times"] = sorted(set(raw["parameters"]["release_times"] + [trigger]))
                    with self.assertRaises(ValueError) as caught:
                        authority.report("mac", value, raw, capability("mac", clock[0]))
                    self.assertEqual(str(caught.exception), "release_window")


if __name__ == "__main__": unittest.main()
