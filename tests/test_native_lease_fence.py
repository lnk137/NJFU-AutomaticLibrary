"""Deterministic checks for stopping native lease renewal after pause failure."""
from contextlib import nullcontext
from threading import Event,Lock,RLock,Thread
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from local_app.models import AppError
from local_app.native.dispatch_base import NativeDispatch
from local_app.native.recovery import EngineRecovery


class LeaseFenceTests(unittest.TestCase):
    def test_failure_is_rechecked_after_heartbeat_waits_for_lock(self):
        dispatch=NativeDispatch.__new__(NativeDispatch)
        dispatch.bridge=SimpleNamespace(outbox=SimpleNamespace(failed=False))
        dispatch.heartbeat_lock=RLock();dispatch.stop=Event()
        entered,renewed=Event(),Event()
        dispatch.clock_ok=lambda: (entered.set() or True)
        dispatch.native=SimpleNamespace(lease=lambda ttl:renewed.set())
        dispatch._lease_ack=lambda deadline:None
        with dispatch.heartbeat_lock:
            thread=Thread(target=dispatch._renew,daemon=True);thread.start()
            self.assertTrue(entered.wait(2))
            dispatch.bridge.outbox.failed=True
        try:self.assertFalse(renewed.wait(.2),'a failed gate renewed its lease')
        finally:dispatch.stop.set();thread.join(2)
        self.assertFalse(thread.is_alive())

    @staticmethod
    def engine(global_enabled):
        outbox=SimpleNamespace(lock=RLock(),failed=False,
            transaction=lambda:nullcontext(),_meta=Mock(),pending=lambda key:None,
            applied=lambda key:{'enabled':global_enabled,'plan':global_enabled})
        sender=SimpleNamespace(poll=lambda:None,kill=Mock(side_effect=OSError('fixture kill failed')))
        process=SimpleNamespace(process=sender,close_control=Mock(side_effect=OSError('fixture close failed')))
        engine=EngineRecovery();engine.recovery_lock=Lock();engine.bridge=SimpleNamespace(outbox=outbox,
            set_control=Mock(side_effect=OSError('fixture ACK lost')))
        engine.scheduler=SimpleNamespace(clock_ok=True);engine.process=process;engine.operator_paused=False
        return engine,sender

    def test_unconfirmed_pause_blocks_renewal_even_if_stopping_sender_fails(self):
        engine,sender=self.engine(True)
        with self.assertRaises(AppError) as caught:engine.pause()
        self.assertEqual(caught.exception.code,'native_recovery_pending')
        self.assertTrue(engine.operator_paused)
        self.assertTrue(engine.bridge.outbox.failed)
        sender.kill.assert_called_once()

    def test_durable_acknowledged_pause_does_not_kill_sender(self):
        engine,sender=self.engine(False)
        self.assertFalse(engine._fence_unconfirmed_sender())
        self.assertTrue(engine.bridge.outbox.failed)
        sender.kill.assert_not_called()

if __name__=='__main__':unittest.main()
