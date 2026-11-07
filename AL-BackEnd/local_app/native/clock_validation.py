"""Bounded, independent read-only NTP observations for recovery, never time setting."""
from concurrent.futures import Future, wait
import hmac
from itertools import combinations
import math
import secrets
import socket
import struct
import threading
import time

from ..elapsed_clock import elapsed_time

SOURCES = ('time.apple.com', 'ntp.aliyun.com', 'time.cloudflare.com')
NTP_EPOCH = 2208988800
MAX_OFFSET = .5


class ProbeSlot:
    """One daemon probe at a time, no queue and no interpreter-exit join."""
    def __init__(self):
        self.lock=threading.Lock();self.pending=None

    def submit(self,action,*args):
        with self.lock:
            if self.pending is None or self.pending.done():
                future=Future();self.pending=future
                def run():
                    try:future.set_result(action(*args))
                    except BaseException as exc:future.set_exception(exc)
                threading.Thread(target=run,name='clock-reference',daemon=True).start()
            return self.pending


_slots={source:ProbeSlot() for source in SOURCES}


def _seconds(raw):
    seconds, fraction = struct.unpack('!II', raw)
    return seconds - NTP_EPOCH + fraction / 2**32


def read_ntp(source, port=123):
    """Connected UDP pins the peer; a fresh echoed nonce rejects old replies."""
    nonce = secrets.token_bytes(8)
    packet = bytes([0x23]) + bytes(39) + nonce
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.8)
            sock.connect((source, port))
            before = time.time()
            started = elapsed_time()
            sock.send(packet)
            data = sock.recv(513)
            after = time.time()
            duration = elapsed_time() - started
        if not 48 <= len(data) <= 512:return None
        if (data[0] >> 6 == 3 or (data[0] >> 3) & 7 not in (3, 4)
                or data[0] & 7 != 4 or not 1 <= data[1] <= 15
                or not hmac.compare_digest(data[24:32], nonce)):
            return None
        received, sent = _seconds(data[32:40]), _seconds(data[40:48])
        root_delay = struct.unpack('!i', data[4:8])[0] / 65536
        dispersion = struct.unpack('!I', data[8:12])[0] / 65536
        if (not 0 <= duration <= .8 or abs((after-before)-duration) > .25
                or not 0 <= sent-received <= duration+.001
                or not 0 <= root_delay <= .5 or not 0 <= dispersion <= .25
                or data[32:40] == bytes(8) or data[40:48] == bytes(8)):
            return None
        uncertainty = dispersion + root_delay / 2
        lower, upper = sent-after-uncertainty, received-before+uncertainty
        if not all(math.isfinite(v) for v in (lower, upper)) or lower > upper:
            return None
        return {'source':source, 'lower':lower, 'upper':upper,
                'offset_s':round((lower+upper)/2, 6), 'rtt_s':round(duration, 6)}
    except (OSError, ValueError, struct.error):
        return None


def ntp_clock_consensus():
    """At most three in-flight probes, two-second wait, two independent votes.

    An unfinished DNS query retains its one slot across calls; it never creates
    an unbounded queue or block process shutdown. UDP samples are cross-checked,
    not cryptographically authenticated NTS evidence.
    """
    futures={source:_slots[source].submit(read_ntp,source) for source in SOURCES}
    done, _ = wait(futures.values(), timeout=2)
    samples = []
    for source, future in futures.items():
        if future not in done:continue
        try:sample = future.result()
        except Exception:sample = None
        if sample is not None and sample.get('source')==source:samples.append(sample)
    eligible = [s for s in samples if -MAX_OFFSET <= s['lower'] <= s['upper'] <= MAX_OFFSET]
    for left, right in combinations(eligible, 2):
        if max(left['lower'], right['lower']) <= min(left['upper'], right['upper']):
            return {'ok':True, 'source':'ntp_quorum', 'reason':'confirmed', 'samples':samples}
    reason = ('local_clock_mismatch' if len(samples) >= 2 and len(eligible) < 2
              else 'reference_disagreement' if len(eligible) >= 2 else 'reference_unavailable')
    return {'ok':False, 'source':'ntp_quorum', 'reason':reason, 'samples':samples}
