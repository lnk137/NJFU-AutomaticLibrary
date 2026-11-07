"""Pinned private installation selection survives launches and fails closed."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_app.config import load_settings
from local_app.models import AppError
try:
    from local_app.native.selection import select_native,verify_selection,release_identity
except ImportError:
    select_native=verify_selection=release_identity=None


class NativeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(select_native,'Private native selection is not implemented')
        self.temp=tempfile.TemporaryDirectory(prefix='njfu-selection-');self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name).resolve();os.chmod(self.folder,0o700)
        self.root=Path(__file__).resolve().parents[1]

    def test_selection_binds_full_adapter_and_binary_and_persists_for_all_launchers(self):
        value=select_native(self.root,self.folder)
        settings=load_settings(self.root,{'data_dir':self.folder})
        self.assertEqual(settings.executor,'cpp')
        self.assertEqual(settings.executor_release,value['release_sha256'])
        self.assertEqual(verify_selection(settings).binary_sha256,value['binary_sha256'])
        self.assertEqual((self.folder/'native-selection.json').stat().st_mode&0o777,0o600)
        self.assertIn('AL-BackEnd/local_app/runtime.py',value['sources'])

    def test_bad_adapter_or_binary_identity_never_silently_selects_python(self):
        value=select_native(self.root,self.folder)
        settings=load_settings(self.root,{'data_dir':self.folder})
        with patch('local_app.native.selection.release_identity',return_value=('0'*64,value['sources'])):
            with self.assertRaises(AppError):verify_selection(settings)
        path=self.folder/'native-selection.json';value['binary_sha256']='0'*64
        path.write_text(json.dumps(value))
        with self.assertRaises(AppError):verify_selection(load_settings(self.root,{'data_dir':self.folder}))

    def test_malformed_or_insecure_selection_is_contained_as_blocked_cpp(self):
        path=self.folder/'native-selection.json';path.write_text('{broken');os.chmod(path,0o600)
        for mode in (0o600,0o644):
            os.chmod(path,mode)
            settings=load_settings(self.root,{'data_dir':self.folder})
            self.assertEqual(settings.executor,'cpp')
            with self.assertRaises(AppError):verify_selection(settings)

    def test_explicit_private_python_selection_is_the_only_persisted_rollback(self):
        path=self.folder/'native-selection.json';path.write_text('{"schema":1,"executor":"python"}');os.chmod(path,0o600)
        self.assertEqual(load_settings(self.root,{'data_dir':self.folder}).executor,'python')
        path.unlink()
        self.assertEqual(load_settings(self.root,{'data_dir':self.folder}).executor,'python')
