"""Source-pin maintenance is constrained to the approved source and same binary."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('merge_identity',Path(__file__).resolve().parents[1]/'deploy/source_identity.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


def digest(data):return hashlib.sha256(data).hexdigest()
def identity(sources):return digest(json.dumps(sources,sort_keys=True).encode())


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='njfu-pin-guard-');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'native-selection.json'
        self.sources={'AL-BackEnd/native_executor/'+name:'a'*64 for name in ('executor.cpp','authority.hpp','profile.hpp','peer_threads.hpp','wire.hpp')}
        self.sources['AL-BackEnd/local_app/runtime.py']='b'*64
        self.old={'schema':1,'executor':'cpp','release_sha256':identity(self.sources),'sources':self.sources,'binary_sha256':'c'*64}
        self.old_bytes=json.dumps(self.old).encode();self.path.write_bytes(self.old_bytes);self.path.chmod(0o600)
        self.next=dict(self.sources,**{'AL-BackEnd/local_app/runtime.py':'d'*64})
    def prepare(self,**changes):
        return guard.prepare_refresh(self.path,**(dict(expected_selector_sha256=digest(self.old_bytes),expected_source_sha256=identity(self.next),source_sha256=identity(self.next),sources=self.next,binary_sha256='c'*64)|changes))
    def test_valid_refresh_preserves_executor_binary_and_file_permissions(self):
        prepared=self.prepare();checks=[]
        result=guard.apply_refresh(self.path,prepared,recheck=lambda:checks.append(True))
        value=json.loads(self.path.read_bytes())
        self.assertEqual((value['executor'],value['binary_sha256']),('cpp','c'*64));self.assertEqual(value['sources'],self.next)
        self.assertEqual(result,digest(self.path.read_bytes()));self.assertEqual(self.path.stat().st_mode&0o777,0o600);self.assertEqual(len(checks),2)
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])
    def test_selector_source_and_binary_mismatches_rejected(self):
        for change in ({'expected_selector_sha256':'e'*64},{'expected_source_sha256':'e'*64},{'binary_sha256':'e'*64}):
            with self.subTest(change=change),self.assertRaises(guard.IdentityConflict):self.prepare(**change)
        self.assertEqual(self.path.read_bytes(),self.old_bytes)
    def test_current_platform_header_allows_python_only_refresh_but_stays_pinned(self):
        header = 'AL-BackEnd/native_executor/platform.hpp'
        self.sources[header] = 'e'*64
        self.old['sources'] = dict(self.sources)
        self.old['release_sha256'] = identity(self.sources)
        self.old_bytes = json.dumps(self.old).encode(); self.path.write_bytes(self.old_bytes)
        self.next = dict(self.sources, **{'AL-BackEnd/local_app/runtime.py': 'd'*64})
        prepared = self.prepare()
        self.assertEqual(json.loads(prepared['new_bytes'])['sources'][header], 'e'*64)
        self.next[header] = 'f'*64
        with self.assertRaises(guard.IdentityConflict): self.prepare()
        del self.next[header]
        with self.assertRaises(guard.IdentityConflict): self.prepare()
        self.assertEqual(self.path.read_bytes(), self.old_bytes)

    def test_native_source_change_rejected(self):
        self.next['AL-BackEnd/native_executor/executor.cpp']='f'*64
        with self.assertRaises(guard.IdentityConflict):self.prepare()
    def test_private_selector_symlink_and_hardlink_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(guard.IdentityConflict):self.prepare()
        self.path.chmod(0o600);link=self.path.parent/'linked';os.link(self.path,link)
        with self.assertRaises(guard.IdentityConflict):self.prepare()
        link.unlink();self.path.rename(link);self.path.symlink_to(link)
        with self.assertRaises(guard.IdentityConflict):self.prepare()
    def test_mid_apply_concurrent_change_preserved_and_temp_cleaned(self):
        prepared=self.prepare();count=0
        def recheck():
            nonlocal count
            count+=1
            if count==2:self.path.write_bytes(b'concurrent edit')
        with self.assertRaises(guard.IdentityConflict):guard.apply_refresh(self.path,prepared,recheck=recheck)
        self.assertEqual(self.path.read_bytes(),b'concurrent edit');self.assertEqual(list(self.path.parent.glob('*.tmp')),[])
    def test_source_recheck_failure_does_not_replace_selector(self):
        prepared=self.prepare()
        def recheck():raise guard.IdentityConflict('Source changed')
        with self.assertRaises(guard.IdentityConflict):guard.apply_refresh(self.path,prepared,recheck=recheck)
        self.assertEqual(self.path.read_bytes(),self.old_bytes)
    def test_tampered_prepared_executor_cannot_be_applied(self):
        prepared=self.prepare();value=json.loads(prepared['new_bytes']);value['executor']='python';prepared['new_bytes']=json.dumps(value).encode()
        with self.assertRaises(guard.IdentityConflict):guard.apply_refresh(self.path,prepared,recheck=lambda:None)
        self.assertEqual(self.path.read_bytes(),self.old_bytes)
    def test_corrupt_original_pin_not_repaired_implicitly(self):
        self.old['release_sha256']='f'*64;data=json.dumps(self.old).encode();self.path.write_bytes(data)
        with self.assertRaises(guard.IdentityConflict):self.prepare(expected_selector_sha256=digest(data))
    def test_fifo_selector_is_rejected_without_waiting_for_writer(self):
        import time
        self.path.unlink();os.mkfifo(self.path,0o600)
        started=time.monotonic()
        with self.assertRaises(guard.IdentityConflict):self.prepare()
        self.assertLess(time.monotonic()-started,1)
    def test_concurrent_maintainer_cannot_overwrite_newer_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        prepared=self.prepare();entered=threading.Event();release=threading.Event()
        def paused_check():
            entered.set()
            if not release.wait(5):raise RuntimeError('Fixture release timeout')
        with ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(guard.apply_refresh,self.path,prepared,recheck=paused_check)
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(guard.IdentityConflict):guard.apply_refresh(self.path,prepared,recheck=lambda:None)
            finally:release.set()
            first.result(timeout=2)
        self.assertEqual(json.loads(self.path.read_bytes())['release_sha256'],identity(self.next))
        with self.assertRaises(guard.IdentityConflict):guard.apply_refresh(self.path,prepared,recheck=lambda:None)
