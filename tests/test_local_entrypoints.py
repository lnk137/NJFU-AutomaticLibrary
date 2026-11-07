"""Explicit live opt-in crosses the real backend entry and local status API."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from support.account_fixture import School
from support.mongo_fixture import MongoFixture

spec = importlib.util.spec_from_file_location('njfu_backend_entry_under_test', ROOT / 'AL-BackEnd/main.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class EntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def observed_live_mode(self, options, environment=None):
        from local_app.runtime import create_runtime
        observed = []
        with tempfile.TemporaryDirectory(prefix='njfu-entry-test-') as directory:
            def isolated_runtime(settings):
                settings = replace(settings, data_dir=Path(directory), mongo_uri=self.mongo.uri,
                                   database_name=self.mongo.database().name)
                return create_runtime(settings, client_factory=School)

            def inspect_local_status(app, **kwargs):
                response = app.test_client().get('/health', base_url='http://127.0.0.1:5003')
                self.assertEqual(response.status_code, 200)
                observed.append(response.get_json()['allow_live'])

            with patch('sys.argv', ['main.py', *options]), patch.dict('os.environ', environment or {}), \
                    patch('local_app.runtime.create_runtime', isolated_runtime), \
                    patch('flask.Flask.run', inspect_local_status), patch.object(entry.signal, 'signal'):
                entry.main()
        self.assertEqual(len(observed), 1)
        return observed[0]

    def test_default_entry_keeps_real_booking_disabled(self):
        self.assertFalse(self.observed_live_mode([]))

    def test_explicit_cli_option_enables_backend_gate(self):
        self.assertTrue(self.observed_live_mode(['--enable-live-booking']))

    def test_environment_alone_cannot_enable_booking(self):
        self.assertFalse(self.observed_live_mode([], {'NJFU_ALLOW_LIVE': '1'}))
