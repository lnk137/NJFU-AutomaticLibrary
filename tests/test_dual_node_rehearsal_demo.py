"""Real spawn-process evidence and cleanup checks for the offline demo."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from experimental.dual_node_rehearsal import demo


def fail_worker(_payload):
    raise RuntimeError("fixture_worker_failed")


def slow_worker(_payload):
    time.sleep(30)
    return {}


class DemoTests(unittest.TestCase):
    def test_each_wave_records_four_real_workers_in_both_barrier_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "demo"
            report = demo.run_demo(directory)
            self.assertEqual(report.get("assignment_process_count", 0), 4)
            self.assertEqual(report.get("consumption_process_count", 0), 4)
            self.assertEqual(report["assignment_attempts"], 8)
            self.assertEqual(report["consumption_attempts"], 8)
            self.assertEqual(report["assignments_created"], 1)
            self.assertEqual(report["successful_consumptions"], 1)
            self.assertEqual(report["network_requests"], 0)
            self.assertTrue(report["conflict_injected"])
            self.assertTrue(report["policy_race_owner_may_differ"])
            self.assertEqual(report["physical_hosts_used"], 1)
            self.assertTrue(report["virtual_clock"])
            self.assertEqual(report["evidence_source"], "synthetic_fixture")
            self.assertFalse(report["live_execution_authorized"])
            for name in ("assignment-race.json", "consumption-race.json"):
                rows = json.loads((directory / name).read_text())
                self.assertEqual(len(rows), 8)
                self.assertEqual(len({row["worker_pid"] for row in rows}), 4)
                self.assertNotIn(os.getpid(), {row["worker_pid"] for row in rows})
                self.assertEqual([row["race_attempt"] for row in rows], list(range(8)))
                for batch in range(2):
                    participants = [row for row in rows if row["barrier_batch"] == batch]
                    self.assertEqual(len(participants), 4)
                    self.assertEqual(len({row["worker_pid"] for row in participants}), 4)
                    self.assertEqual({row["barrier_ticket"] for row in participants}, set(range(4)))

    def test_worker_exception_joins_all_owned_children(self):
        before = {child.pid for child in multiprocessing.active_children()}
        with self.assertRaisesRegex(RuntimeError, "fixture_worker_failed"):
            demo._race(fail_worker, [None] * 8)
        self.assertEqual({child.pid for child in multiprocessing.active_children()} - before, set())

    def test_timeout_terminates_and_joins_all_owned_children(self):
        before = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()
        with patch.object(demo, "_RACE_TIMEOUT_SECONDS", .2, create=True):
            with self.assertRaises(multiprocessing.TimeoutError):
                demo._race(slow_worker, [None] * 8)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual({child.pid for child in multiprocessing.active_children()} - before, set())


if __name__ == "__main__":
    unittest.main()
