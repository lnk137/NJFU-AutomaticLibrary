"""Offline release metadata refresh cannot change an already resident engine."""
import json
import hashlib
from deploy.source_identity import prepare_refresh, apply_refresh
import math
import os
import shutil
import time
from unittest.mock import patch

from local_app.config import load_settings
from local_app.models import AppError
from local_app.native.build import SOURCES, read_build
from local_app.native.selection import release_identity, select_native, verify_selection
from support.native_runtime import NativeRuntimeCase


class NativeSourcePinRefreshTests(NativeRuntimeCase):
    def test_atomic_python_source_pin_refresh_preserves_resident_native_owner(self):
        root = self.directory / 'offline-release'
        (root / 'AL-BackEnd/local_app').mkdir(parents=True)
        (root / 'AL-BackEnd/native_executor').mkdir()
        for name in SOURCES:
            shutil.copyfile(self.root / 'AL-BackEnd/native_executor' / name,
                            root / 'AL-BackEnd/native_executor' / name)
        for name in ('AL-BackEnd/main.py', 'local_run.py', 'public_run.py', 'power_agent.py'):
            (root / name).write_text('# offline fixture\n')
        source = root / 'AL-BackEnd/local_app/release_fixture.py'
        source.write_text('VERSION = 1\n')
        with patch('local_app.native.selection.read_build', side_effect=lambda current:
                   read_build(current, self.root / '.runtime/native-executor')):
            old = select_native(root, self.directory)
            settings = load_settings(root, {'data_dir': self.directory})
            self.assertEqual(verify_selection(settings).binary_sha256, self.build.binary_sha256)
            run = self.plan(due=math.ceil(time.time() + 6))
            self.runtime.start()
            armed = self.wait_arm(run)
            engine = self.runtime.native
            identity = (engine.process.identity, engine.process.epoch, engine.process.session)
            source.write_text('VERSION = 2\n')
            release, sources = release_identity(root)
            selector = self.directory / 'native-selection.json'
            prepared = prepare_refresh(selector,
                expected_selector_sha256=hashlib.sha256(selector.read_bytes()).hexdigest(),
                expected_source_sha256=release, source_sha256=release,
                sources=sources, binary_sha256=self.build.binary_sha256)
            def recheck():
                self.assertEqual(release_identity(root), (release, sources))
                self.assertEqual(read_build(root, self.root / '.runtime/native-executor').binary_sha256,
                                 self.build.binary_sha256)
            apply_refresh(selector, prepared, recheck=recheck)
            with self.assertRaises(AppError):
                verify_selection(settings)
            fresh = load_settings(root, {'data_dir': self.directory})
            self.assertEqual(verify_selection(fresh).binary_sha256, self.build.binary_sha256)
            row = self.wait(run.key, {'success', 'failed', 'skipped'}, 12)
            self.assertEqual(row['status'], 'success', row)
            self.assertIs(self.runtime.native, engine)
            self.assertEqual((engine.process.identity, engine.process.epoch, engine.process.session), identity)
            self.assertEqual(row['native_binding'], armed['native_binding'])
            self.assertEqual(len(self.fixture.posts()), 1)
