"""Ed25519 SSHSIG grants, separated from fixture and production domains."""
from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from types import MappingProxyType

from .contracts import (DualNodeError, MODES, NODES, canonical_bytes, exact,
                        identifier, require, task_identity, timestamp)

_SEAL = object()


def _domain(mode):
    require(mode in MODES, "invalid_mode")
    return "njfu-dual-node-" + mode + "-v1"


def _run(args, data=None):
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=8, check=True,
                                env={k: v for k, v in os.environ.items()
                                     if k not in ("SSH_AUTH_SOCK", "SSH_ASKPASS", "DISPLAY")})
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise DualNodeError("signature_failed") from None


def _public(value):
    require(type(value) is str and "\n" not in value.strip(), "invalid_public_key")
    parts = value.strip().split()
    require(len(parts) in (2, 3) and parts[0] == "ssh-ed25519", "invalid_public_key")
    return " ".join(parts[:2])


@dataclass(frozen=True)
class VerifiedGrant(Mapping):
    _data: Mapping
    mode: str
    _seal: object

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __getattr__(self, name):
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)


def require_verified_grant(value):
    require(isinstance(value, VerifiedGrant) and value._seal is _SEAL, "unverified_grant")
    return value


class GrantSigner:
    def __init__(self, private_key, *, mode="fixture", authority_id):
        self.private_key = Path(private_key)
        self.mode = mode
        self.authority_id = identifier(authority_id, size=32)
        _domain(mode)
        info = self.private_key.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                "unsafe_signing_key")
        self.public_key = _public(_run(["/usr/bin/ssh-keygen", "-y", "-f", str(self.private_key)]).decode())

    def _sign(self, kind, payload):
        payload = {**payload, "kind": kind, "authority_id": self.authority_id}
        envelope = {"schema": 1, "domain": _domain(self.mode), "payload": payload}
        signature = _run(["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(self.private_key),
                          "-n", envelope["domain"], "-"], canonical_bytes(envelope)).decode()
        return {**envelope, "signature": signature}

    def sign_grant(self, payload):
        return self._sign("grant", payload)

    def sign_time(self, payload):
        return self._sign("time", payload)


class GrantVerifier:
    def __init__(self, public_key, *, mode="fixture", authority_id=None):
        self.public_key = _public(public_key)
        self.mode = mode
        self.authority_id = identifier(authority_id, size=32) if authority_id is not None else None
        _domain(mode)

    def _verify(self, envelope, kind, node_id):
        exact(envelope, {"schema", "domain", "payload", "signature"})
        require(type(envelope["schema"]) is int and envelope["schema"] == 1
                and envelope["domain"] == _domain(self.mode), "wrong_grant_domain")
        signature = envelope["signature"]
        require(type(signature) is str and len(signature) <= 16384
                and signature.startswith("-----BEGIN SSH SIGNATURE-----\n"), "invalid_signature")
        payload = envelope["payload"]
        require(type(payload) is dict and payload.get("kind") == kind
                and payload.get("node_id") == node_id and node_id in NODES, "wrong_grant_identity")
        identifier(payload.get("authority_id"), size=32)
        if self.authority_id is not None:
            require(payload["authority_id"] == self.authority_id, "wrong_authority")
        with tempfile.TemporaryDirectory(prefix="njfu-grant-verify-") as directory:
            root = Path(directory)
            (root / "allowed").write_text("njfu-authority " + self.public_key + "\n")
            (root / "signature").write_text(signature)
            _run(["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(root / "allowed"),
                  "-I", "njfu-authority", "-n", envelope["domain"], "-s", str(root / "signature")],
                 canonical_bytes({k: envelope[k] for k in ("schema", "domain", "payload")}))
        return dict(payload)

    def verify_grant(self, envelope, *, node_id, task, now=None):
        payload = self._verify(envelope, "grant", node_id)
        exact(payload, {"kind", "authority_id", "grant_id", "task_id", "binding_sha256", "node_id",
                        "issued_at", "release_at", "expires_at", "credential_revision"})
        task_id, binding = task_identity(task)
        identifier(payload["grant_id"], size=32)
        timestamp(payload["release_at"]); timestamp(payload["expires_at"])
        require(payload["task_id"] == task_id and payload["binding_sha256"] == binding
                and payload["release_at"] == task["release_at"]
                and payload["expires_at"] == task["release_at"] + 30
                and type(payload["credential_revision"]) is int
                and payload["credential_revision"] == task["credential_revision"], "wrong_task_binding")
        issued = timestamp(payload["issued_at"])
        require(issued <= timestamp(payload["expires_at"]), "invalid_grant_time")
        if now is not None:
            require(issued - 5 <= timestamp(now) <= payload["expires_at"], "expired_grant")
        return VerifiedGrant(MappingProxyType(payload), self.mode, _SEAL)

    def verify_time(self, envelope, *, node_id):
        payload = self._verify(envelope, "time", node_id)
        exact(payload, {"kind", "authority_id", "node_id", "nonce", "server_wall", "sent_at", "valid_until"})
        identifier(payload["nonce"], size=32)
        wall, sent, valid = (timestamp(payload[k]) for k in ("server_wall", "sent_at", "valid_until"))
        require(wall <= sent <= valid <= wall + 900, "invalid_time_sample")
        return payload
