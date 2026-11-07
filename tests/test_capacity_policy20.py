"""The ordinary scheduled and inbox account pools share the 20-account ceiling."""
from types import SimpleNamespace
import unittest
from local_app.limits import PARALLEL_ACCOUNT_LIMIT
from local_app.scheduler import LocalScheduler
from local_app.inbox import InboxWorker


class CapacityPolicyTests(unittest.TestCase):
    def test_python_scheduled_warm_and_submit_pools_remain_bounded_at_twenty(self):
        self.assertEqual(PARALLEL_ACCOUNT_LIMIT,20)
        scheduler=LocalScheduler(SimpleNamespace(busy=set()),SimpleNamespace(),None)
        self.addCleanup(scheduler.stop)
        self.assertEqual(scheduler.warm_pool._max_workers,20)
        self.assertEqual(scheduler.submit_pool._max_workers,20)
        self.assertEqual(scheduler.prep_pool._max_workers,4)

    def test_inbox_account_pool_uses_the_same_ceiling(self):
        db=SimpleNamespace(campus_inbox_auth=None,campus_inbox_receipts=None,campus_inbox_state=None)
        worker=InboxWorker(SimpleNamespace(),SimpleNamespace(),SimpleNamespace(db=db),None,None,{})
        self.addCleanup(worker.close)
        self.assertEqual(worker.pool._max_workers,20)
        self.assertEqual(worker.ack_pool._max_workers,4)

if __name__=='__main__':unittest.main()
