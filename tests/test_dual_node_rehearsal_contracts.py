"""Synthetic intent and private file contracts; no network or app imports."""
from pathlib import Path
import copy
import json
import os
import tempfile
import unittest

try:
    from experimental.dual_node_rehearsal import contracts as c
except ImportError:
    c = None


def fixture_intent():
    return {"schema": 1, "account_ref": "fixture-user", "day": "2026-11-06",
            "kind": "S", "operation": "-", "plan_version": 1,
            "start_minute": 480, "end_minute": 1320,
            "seat_ref": "fixture-seat-a", "release_at": 1793920200.0}


class AvailabilityTests(unittest.TestCase):
    def test_contracts_are_available(self):
        self.assertIsNotNone(c, "the isolated rehearsal contracts are not implemented")


@unittest.skipIf(c is None, "contracts not implemented")
class ContractTests(unittest.TestCase):
    def test_logical_intent_is_stable_across_seat_and_plan_changes(self):
        intent = fixture_intent(); first = c.intent_identity(intent)
        other = intent | {"seat_ref": "fixture-seat-b", "plan_version": 2,
                          "start_minute": 540, "release_at": intent["release_at"] + 10}
        second = c.intent_identity(other)
        self.assertEqual(first[0], second[0]); self.assertNotEqual(first[1], second[1])
        for key, value in (("account_ref", "fixture-other"), ("day", "2026-11-07")):
            self.assertNotEqual(first[0], c.intent_identity(intent | {key: value})[0])

    def test_invalid_or_real_account_intent_refuses(self):
        for key, value in (("schema", True), ("account_ref", "real-school-account"),
                           ("day", "2026-02-30"), ("plan_version", False),
                           ("start_minute", 1320), ("end_minute", 1441),
                           ("seat_ref", "seat-without-fixture-prefix"),
                           ("release_at", float("nan")), ("kind", "C")):
            with self.subTest(key=key), self.assertRaises(c.RehearsalError):
                c.intent_identity(fixture_intent() | {key: value})
        for value in (None, [], fixture_intent() | {"nonce": "must-not-rekey"}):
            with self.assertRaises(c.RehearsalError): c.intent_identity(value)

    def test_wall_time_is_finite_exact_numeric_and_bounded(self):
        for bad in (True, "12", float("inf"), -1, 10 ** 400, 4102444801):
            with self.subTest(value=str(bad)[:20]), self.assertRaises(c.RehearsalError):
                c.wall_time(bad)
        self.assertEqual(c.wall_time(100), 100.0)

    def test_envelope_cannot_be_converted_into_live_permission(self):
        value = c.envelope(live_execution_authorized=True, rehearsal_only=False, mode="live")
        self.assertIs(value["live_execution_authorized"], False)
        self.assertIs(value["rehearsal_only"], True)
        self.assertEqual(value["mode"], "dual_node_rehearsal_v1")

    def test_canonical_hash_is_order_independent_and_rejects_nan(self):
        self.assertEqual(c.canonical_hash({"b": 1, "a": 2}), c.canonical_hash({"a": 2, "b": 1}))
        with self.assertRaises(c.RehearsalError): c.canonical_hash({"x": float("nan")})

    def test_exclusive_private_output_under_umask_002(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "new.json"
            previous = os.umask(0o002)
            try: c.write_json(path, {"fixture": True})
            finally: os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(c.safe_read_json(path), {"fixture": True})
            with self.assertRaises(c.RehearsalError): c.write_json(path, {"overwrite": True})
            self.assertEqual(json.loads(path.read_text()), {"fixture": True})

    def test_input_rejects_duplicate_keys_nonfinite_and_large_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "input.json"
            for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[]' + b' ' * 1048576):
                path.write_bytes(data)
                with self.assertRaises(c.RehearsalError): c.safe_read_json(path)

    def test_symlink_hardlink_and_fifo_refuse_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / "input.json"; path.write_text("{}")
            alias = root / "alias"; alias.symlink_to(path)
            with self.assertRaises(c.RehearsalError): c.safe_read_json(alias)
            hard = root / "hard"; os.link(path, hard)
            with self.assertRaises(c.RehearsalError): c.safe_read_json(hard)
            fifo = root / "fifo"; os.mkfifo(fifo)
            with self.assertRaises(c.RehearsalError): c.safe_read_json(fifo)
            folder = root / "linked"; folder.symlink_to(root, target_is_directory=True)
            with self.assertRaises(c.RehearsalError): c.write_json(folder / "new.json", {})
            with self.assertRaises(c.RehearsalError): c.safe_read_json(root)


if __name__ == "__main__": unittest.main()
