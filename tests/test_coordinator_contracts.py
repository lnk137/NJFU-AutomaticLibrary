from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
"""Candidate-only protocol contracts and Ed25519 domain separation."""
import copy
import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK / "AL-BackEnd"))


def task():
    return {"schema": 1, "account_ref": "fixture-account", "day": "2026-11-07",
            "kind": "S", "operation": "-", "revision": 1, "credential_revision": 1,
            "release_at": 1794006000.0, "start_minute": 480, "end_minute": 1320,
            "seat_priorities": ["3422", "3423"]}


class ContractTests(unittest.TestCase):
    def modules(self):
        try:
            return (importlib.import_module("local_app.dual_node.contracts"),
                    importlib.import_module("local_app.dual_node.grants"))
        except ImportError:
            self.fail("candidate contracts and grants are not implemented")

    def test_stable_task_identity_and_full_binding(self):
        contracts, _ = self.modules()
        original = task()
        changed = copy.deepcopy(original); changed["revision"] += 1
        changed["seat_priorities"].reverse()
        self.assertEqual(contracts.task_identity(original)[0], contracts.task_identity(changed)[0])
        self.assertNotEqual(contracts.task_identity(original)[1], contracts.task_identity(changed)[1])
        for key, value in (("revision", True), ("password", "DO_NOT_ECHO"), ("seat_priorities", [])):
            invalid = dict(original); invalid[key] = value
            with self.assertRaises(contracts.DualNodeError):
                contracts.validate_task(invalid)

    def test_strict_json_refuses_duplicates_nonfinite_and_credentials(self):
        contracts, _ = self.modules()
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"password":"DO_NOT_ECHO"}',
                     b'{"cookie":"DO_NOT_ECHO"}', b'[]', b" " * (1048576 + 1)):
            with self.subTest(data=data[:40]), self.assertRaises(contracts.DualNodeError) as caught:
                contracts.parse_json(data)
            self.assertNotIn("DO_NOT_ECHO", str(caught.exception))

    def test_signed_grant_and_time_are_node_task_and_domain_bound(self):
        contracts, grants = self.modules()
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "fixture-key"
            subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, capture_output=True, timeout=10)
            signer = grants.GrantSigner(key, mode="fixture", authority_id="a" * 32)
            verifier = grants.GrantVerifier(signer.public_key, mode="fixture", authority_id=signer.authority_id)
            value = task(); task_id, binding = contracts.task_identity(value)
            payload = {"grant_id": "b" * 32, "task_id": task_id, "binding_sha256": binding,
                       "node_id": "mac", "issued_at": value["release_at"] - 310,
                       "release_at": value["release_at"], "expires_at": value["release_at"] + 30,
                       "credential_revision": 1}
            signed = signer.sign_grant(payload)
            verified = verifier.verify_grant(signed, node_id="mac", task=value)
            self.assertIsInstance(verified, grants.VerifiedGrant)
            self.assertEqual(verified.grant_id, "b" * 32)
            self.assertEqual(verified.mode, "fixture")
            self.assertIs(grants.require_verified_grant(verified), verified)
            with self.assertRaises(contracts.DualNodeError):
                grants.require_verified_grant(dict(verified))
            tampered = copy.deepcopy(signed); tampered["payload"]["node_id"] = "ubuntu"
            production = grants.GrantVerifier(signer.public_key, mode="production", authority_id=signer.authority_id)
            for receiver, envelope, node, bound_task in (
                    (verifier, tampered, "ubuntu", value), (production, signed, "mac", value),
                    (verifier, signed, "ubuntu", value), (verifier, signed, "mac", value | {"revision": 2})):
                with self.assertRaises(contracts.DualNodeError):
                    receiver.verify_grant(envelope, node_id=node, task=bound_task)
            time = signer.sign_time({"node_id": "mac", "nonce": "c" * 32,
                                     "server_wall": 1000.0, "sent_at": 1000.0, "valid_until": 1900.0})
            self.assertEqual(verifier.verify_time(time, node_id="mac")["nonce"], "c" * 32)
            with self.assertRaises(contracts.DualNodeError):
                verifier.verify_time(time, node_id="ubuntu")
            early_task = value | {"release_at": 1.0}
            early_id, early_binding = contracts.task_identity(early_task)
            malformed = signer.sign_grant(payload | {"task_id": early_id, "binding_sha256": early_binding,
                "issued_at": 0, "release_at": True, "expires_at": 31.0})
            with self.assertRaises(contracts.DualNodeError):
                verifier.verify_grant(malformed, node_id="mac", task=early_task)


if __name__ == "__main__":
    unittest.main()
