"""Actual bounded verified TLS Date checks through the private school fixture."""
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from local_app.native.recovery import trusted_wall_clock
from support.native_school import CampusFixture,FixtureTransport


class NativeClockEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.fixture=self.stack.enter_context(CampusFixture())
        self.session=FixtureTransport(self.fixture)
        self.stack.enter_context(patch('local_app.native.recovery.requests.Session',return_value=self.session))
        self.stack.enter_context(patch('local_app.native.recovery.ntp_clock_consensus',return_value={
            'ok':False,'source':'ntp_quorum','reason':'reference_unavailable','samples':[]}))
    def test_verified_public_date_can_confirm_clock_without_authentication(self):
        self.assertTrue(trusted_wall_clock())
        self.assertEqual(self.fixture.posts(),[])
        self.assertFalse(any(row['kind']=='login' for row in self.fixture.events))
    def test_malformed_date_type_is_untrusted_instead_of_raising(self):
        request=self.session.get
        def malformed(*args,**kwargs):
            response=request(*args,**kwargs);response.headers['Date']=123;return response
        self.session.get=malformed
        self.assertFalse(trusted_wall_clock())
        self.assertEqual(self.fixture.posts(),[])
    def test_unavailable_or_expired_public_date_never_confirms_clock(self):
        request=self.session.get
        def old(*args,**kwargs):
            response=request(*args,**kwargs);response.headers['Date']='Wed, 01 Jan 2020 00:00:00 GMT';return response
        self.session.get=old
        self.assertFalse(trusted_wall_clock())
        self.assertEqual(self.fixture.posts(),[])
