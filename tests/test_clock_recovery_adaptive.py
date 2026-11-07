"""Regression for stale school Date and adaptive recovery; no external IO."""
from email.utils import formatdate
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from local_app.native import recovery


class ClockValidationRegressionTests(unittest.TestCase):
    def check(self, *, skew=-5, unavailable=False, age=None, quorum=True):
        headers={'Date':formatdate(time.time()+skew,usegmt=True)}
        if age is not None:headers['Age']=age
        response=SimpleNamespace(status_code=200,headers=headers,history=[],close=Mock())
        session=Mock()
        if unavailable:session.get.side_effect=recovery.requests.ConnectionError('isolated')
        else:session.get.return_value=response
        result={'ok':quorum,'reason':'confirmed' if quorum else 'reference_unavailable',
                'source':'ntp_quorum','samples':[]}
        with patch.object(recovery.requests,'Session',return_value=session), patch.object(
                recovery,'ntp_clock_consensus',return_value=result,create=True) as fallback:
            accepted=recovery.trusted_wall_clock()
        return accepted,fallback.call_count,session

    def test_five_second_school_lag_recovers_from_independent_quorum(self):
        accepted,calls,session=self.check()
        self.assertTrue(accepted)
        self.assertEqual(calls,1)
        session.close.assert_called_once()
        self.assertFalse(session.trust_env)

    def test_school_network_failure_can_use_independent_quorum(self):
        accepted,calls,_=self.check(unavailable=True)
        self.assertTrue(accepted)
        self.assertEqual(calls,1)

    def test_cached_school_date_does_not_confirm_without_other_sources(self):
        accepted,calls,_=self.check(skew=0,age='90',quorum=False)
        self.assertFalse(accepted)
        self.assertEqual(calls,1)

    def test_no_valid_quorum_stays_paused(self):
        accepted,_,_=self.check(quorum=False)
        self.assertFalse(accepted)

    def test_fresh_verified_school_date_keeps_existing_fast_path(self):
        accepted,calls,_=self.check(skew=0,quorum=False)
        self.assertTrue(accepted)
        self.assertEqual(calls,0)
