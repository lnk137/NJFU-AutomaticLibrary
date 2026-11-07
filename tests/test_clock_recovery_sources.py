"""Bounded NTP protocol, quorum and recovery pacing using local fixtures."""
from concurrent.futures import Future
from contextlib import contextmanager
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from local_app.native import clock_validation as clock
from local_app.native.recovery_retry import RecoveryBudget


def timestamp(value):
    whole=int(value)
    return struct.pack('!II',whole+clock.NTP_EPOCH,int((value-whole)*2**32))


@contextmanager
def server(change=None, offset=0):
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1',0));sock.settimeout(2)
    port=sock.getsockname()[1]
    errors=[]
    def serve():
        try:
            request,peer=sock.recvfrom(512)
            data=bytearray(48);data[0]=0x24;data[1]=1
            data[24:32]=request[40:48]
            data[32:40]=timestamp(time.time()+offset)
            data[40:48]=timestamp(time.time()+offset)
            if change:change(data)
            sock.sendto(bytes(data),peer)
        except Exception as exc:errors.append(type(exc).__name__)
    thread=threading.Thread(target=serve);thread.start()
    try:yield port
    finally:
        thread.join(3);sock.close()
        if thread.is_alive() or errors:raise AssertionError('UDP fixture not closed: '+str(errors))


class NtpProtocolTests(unittest.TestCase):
    def test_real_loopback_udp_echo_confirms_a_bounded_sample(self):
        with server() as port:sample=clock.read_ntp('127.0.0.1',port)
        self.assertIsNotNone(sample)
        self.assertLess(abs(sample['offset_s']),.05)

    def test_wrong_nonce_is_rejected(self):
        with server(lambda p:p.__setitem__(slice(24,32),bytes(8))) as port:
            self.assertIsNone(clock.read_ntp('127.0.0.1',port))

    def test_unsynchronized_server_is_rejected(self):
        with server(lambda p:p.__setitem__(0,0xe4)) as port:
            self.assertIsNone(clock.read_ntp('127.0.0.1',port))

    def test_client_mode_reply_is_rejected(self):
        with server(lambda p:p.__setitem__(0,0x23)) as port:
            self.assertIsNone(clock.read_ntp('127.0.0.1',port))

    def test_kiss_of_death_and_invalid_stratum_are_rejected(self):
        for stratum in (0,16):
            with self.subTest(stratum=stratum),server(lambda p:p.__setitem__(1,stratum)) as port:
                self.assertIsNone(clock.read_ntp('127.0.0.1',port))

    def test_excessive_dispersion_is_rejected(self):
        with server(lambda p:p.__setitem__(slice(8,12),struct.pack('!I',65536))) as port:
            self.assertIsNone(clock.read_ntp('127.0.0.1',port))

    def test_clock_jump_during_probe_invalidates_sample(self):
        with server() as port,patch.object(clock,'elapsed_time',side_effect=[0,2]):
            self.assertIsNone(clock.read_ntp('127.0.0.1',port))

    def test_remote_offset_is_measured_without_setting_local_clock(self):
        with server(offset=2) as port:sample=clock.read_ntp('127.0.0.1',port)
        self.assertGreater(sample['lower'],1.9)


class NtpQuorumTests(unittest.TestCase):
    def sample(self,source,offset=0):
        return {'source':source,'lower':offset-.05,'upper':offset+.05,
                'offset_s':offset,'rtt_s':.1}

    def consensus(self,results):
        with patch.object(clock,'read_ntp',side_effect=lambda source:results.get(source)):
            return clock.ntp_clock_consensus()

    def test_two_independent_sources_accept_while_one_is_unavailable(self):
        result=self.consensus({source:self.sample(source) for source in clock.SOURCES[1:]})
        self.assertTrue(result['ok'])

    def test_one_source_alone_never_confirms(self):
        result=self.consensus({clock.SOURCES[0]:self.sample(clock.SOURCES[0])})
        self.assertFalse(result['ok'])

    def test_two_sources_disagree_never_confirm(self):
        result=self.consensus({s:self.sample(s,o) for s,o in zip(clock.SOURCES,(-.3,.3))})
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'reference_disagreement')

    def test_agreeing_sources_reject_a_wrong_local_clock(self):
        result=self.consensus({s:self.sample(s,2) for s in clock.SOURCES})
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'local_clock_mismatch')

    def test_one_outlier_cannot_overrule_two_agreeing_sources(self):
        result=self.consensus({s:self.sample(s,o) for s,o in zip(clock.SOURCES,(0,.01,3))})
        self.assertTrue(result['ok'])

    def test_pending_resolver_keeps_one_slot_instead_of_accumulating_threads(self):
        slots={s:clock.ProbeSlot() for s in clock.SOURCES}
        for slot in slots.values():slot.pending=Future()
        with patch.object(clock,'_slots',slots),patch.object(clock,'read_ntp') as query,patch.object(clock,'wait',return_value=(set(),set(slot.pending for slot in slots.values()))):
            for _ in range(10):self.assertFalse(clock.ntp_clock_consensus()['ok'])
            query.assert_not_called()


class RecoveryBudgetTests(unittest.TestCase):
    def test_blocked_dns_probes_do_not_block_process_exit(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        program="""import threading
from local_app.native import clock_validation as c, recovery as r
c.read_ntp=lambda *args: threading.Event().wait(600)
r._school_clock_check=lambda: threading.Event().wait(600)
assert r.verify_wall_clock()['ok'] is False
print('bounded exit')
"""
        environment=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
        started=time.monotonic()
        result=subprocess.run([sys.executable,'-B','-c',program],env=environment,
                              capture_output=True,text=True,timeout=7)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('bounded exit',result.stdout)
        self.assertLess(time.monotonic()-started,6.5)

    def test_six_failure_cycle_then_cooldown_starts_a_new_bounded_cycle(self):
        budget=RecoveryBudget();now=100
        for attempt,delay in enumerate((1,3,10,30,60,60),1):
            self.assertTrue(budget.ready(now));budget.failure(now)
            self.assertEqual(budget.attempts,attempt)
            self.assertFalse(budget.ready(now+delay-.01))
            now+=delay
        self.assertTrue(budget.cooldown)
        self.assertTrue(budget.ready(now))
        self.assertEqual(budget.attempts,0)
        self.assertFalse(budget.cooldown)

    def test_wake_cannot_bypass_cooldown_or_reset_failure_budget(self):
        budget=RecoveryBudget()
        for _ in range(6):budget.failure(0)
        for now in (.1,1,2,3,59):
            budget.nudge(now);self.assertFalse(budget.ready(now))
        self.assertEqual(budget.attempts,6)

    def test_repeated_wake_notifications_do_not_create_a_retry_storm(self):
        budget=RecoveryBudget();budget.failure(100);budget.nudge(100.1)
        self.assertFalse(budget.ready(100.1))
        self.assertTrue(budget.ready(101));budget.failure(101)
        for now in (101.1,101.2,101.8):
            budget.nudge(now);self.assertFalse(budget.ready(now))

    def test_intentional_deferral_does_not_consume_failure_budget(self):
        budget=RecoveryBudget();budget.failure(100)
        budget.defer(101)
        self.assertEqual(budget.attempts,1)
        self.assertFalse(budget.ready(101.9))

    def test_success_resets_attempts_and_cooldown(self):
        budget=RecoveryBudget()
        for _ in range(6):budget.failure(0)
        budget.success()
        self.assertEqual(budget.snapshot(5),{'attempts':0,'limit':6,'cooldown':False,'retry_in_seconds':0})
