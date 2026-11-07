"""Bound signed server time to this boot; explicit verified Linux launch policy."""
import math
from pathlib import Path
import subprocess
import sys
import time

from ..native.clock_pair import sample_clock_pair
from ..native.interactive import validate
from ..native.protocol import clock_ns
from ..native.trusted_clock import boot_identifier


class ClockPermit:
    def __init__(self, verifier, samples, *, node_id, wall_clock=time.time,
                 clock_ns=clock_ns, boot_clock=boot_identifier):
        if getattr(verifier, 'mode', None) not in {'fixture', 'production'}:
            raise ValueError('signed clock verifier required')
        if verifier.mode == 'production':
            from .grants import GrantVerifier
            if type(verifier) is not GrantVerifier: raise ValueError('pinned clock verifier required')
        self.mode, self.node_id, self.verifier = verifier.mode, node_id, verifier
        self.failed = False
        self.wall_clock, self.clock_ns, self.boot_clock = wall_clock, clock_ns, boot_clock
        self.boot_id = boot_clock()
        self.anchor = sample_clock_pair(wall_clock, clock_ns)
        if not self.boot_id or self.anchor.width_ns > 1_000_000:
            raise ValueError('local clock pair unavailable')
        if not isinstance(samples, list) or not 1 <= len(samples) <= 5:
            raise ValueError('bounded signed time samples required')
        verified = []; nonces = set(); authorities = set()
        for sample in samples:
            before, after = sample['before_ns'], sample['after_ns']
            if (type(before) is not int or type(after) is not int or before < 0
                    or not 0 <= after-before <= 200_000_000 or after > self.anchor.after_ns
                    or self.anchor.after_ns-before > 2_000_000_000):
                raise ValueError('signed time roundtrip invalid')
            value = verifier.verify_time(sample['signed_time'], node_id=node_id)
            authorities.add(value.get('authority_id', getattr(verifier, 'authority_id', None)))
            server, until, sent = value['server_wall'], value['valid_until'], value['sent_at']
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (server, until, sent)):
                raise ValueError('signed clock fields invalid')
            nonce = value['nonce']
            if nonce in nonces or (sample.get('nonce', nonce) != nonce):
                raise ValueError('signed clock nonce mismatch')
            nonces.add(nonce)
            midpoint = (before+after)//2
            local_wall = self.anchor.wall_at(midpoint)
            error = (after-before)/2e9 + self.anchor.error_bound_ns/1e9
            if abs(local_wall-server)+error >= .25 or abs(sent-server) > .2 or until <= server:
                raise ValueError('coordinator time differs from local clock')
            verified.append((before, until))
        if len(authorities) != 1 or None in authorities: raise ValueError('clock authority mismatch')
        self.authority_id = authorities.pop()
        self.observed_ns = min(row[0] for row in verified)
        self.valid_until = min(row[1] for row in verified)

    def require_valid(self, task=None, *, for_send=False):
        if self.failed: raise ValueError('signed time evidence revoked')
        try:
            return self._require_valid(task, for_send=for_send)
        except Exception:
            self.failed = True
            raise ValueError('signed time evidence invalid') from None

    def _require_valid(self, task=None, *, for_send=False):
        pair = sample_clock_pair(self.wall_clock, self.clock_ns)
        if (self.boot_clock() != self.boot_id or pair.width_ns > 1_000_000
                or not 0 <= pair.after_ns-self.observed_ns <= 120_000_000_000
                or pair.wall >= self.valid_until):
            raise ValueError('signed time evidence expired')
        pair.require_consistent(self.anchor.wall, self.anchor.midpoint_ns)
        if for_send and (task is None or self.valid_until < task['release_at']+30):
            raise ValueError('signed time evidence does not cover execution')
        return pair


class LinuxVerifiedLauncher:
    def __init__(self, source_root, build, time_permit):
        if type(time_permit) is not ClockPermit or time_permit.mode != 'production':
            raise ValueError('production signed time required')
        self.source_root, self.build, self.time_permit = Path(source_root), build, time_permit

    def __call__(self, command, environment, runtime, *, expected_sha256, allow_school):
        if not sys.platform.startswith('linux') or not allow_school:
            raise ValueError('explicit Linux school launch required')
        from ..native.build import read_build
        # Revalidate the exact manifest and dependency liveness, not a caller bool.
        current = read_build(self.source_root, output=Path(self.build.executable).parent.parent)
        if (current.binary_sha256 != expected_sha256 or current.source_sha256 != self.build.source_sha256
                or Path(command[0]) != current.executable):
            raise ValueError('native release changed')
        self.time_permit.require_valid()
        validate(command, runtime, expected_sha256, environment, allow_school=True)
        clean = {'PATH': '/usr/bin:/bin'}
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=clean)
