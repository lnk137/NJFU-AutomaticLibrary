"""Production launch policy remains fail-closed when fixture support is used."""
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from local_app.native.protocol import Endpoint,NativeProcess


class NativeLaunchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='njfu-launch-policy-')
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name).resolve()
        self.binary=self.directory/'fake-executor'
        self.binary.write_bytes(b'not executable test data');self.binary.chmod(0o700)
        self.digest=hashlib.sha256(self.binary.read_bytes()).hexdigest()

    def test_school_endpoint_cannot_use_direct_or_unverified_launcher(self):
        with patch.dict(os.environ,{'NJFU_EXECUTOR_MODE':'direct'}):
            with patch('local_app.native.protocol.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    NativeProcess(self.directory,self.binary,endpoint=Endpoint.production(),
                                  count=1,verified_binary_sha256=self.digest)
                launch.assert_not_called()
        with self.assertRaises(ValueError):
            NativeProcess(self.directory,self.binary,endpoint=Endpoint.production(),count=1)

    def test_explicit_interactive_fixture_still_requires_isolation_root(self):
        cert=self.directory/'fixture.pem';cert.write_text('fixture-only')
        with patch.dict(os.environ,{'NJFU_EXECUTOR_MODE':'interactive'}):
            os.environ.pop('NJFU_TEST_ISOLATION_ROOT',None)
            with patch('local_app.native.protocol.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError,'fixture isolation root'):
                    NativeProcess(self.directory,self.binary,endpoint=Endpoint.fixture(12345,cert),count=1)
                launch.assert_not_called()

if __name__=='__main__':unittest.main()
