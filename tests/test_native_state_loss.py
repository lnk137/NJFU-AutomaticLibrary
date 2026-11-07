"""Missing private files cannot erase persisted ownership or select a sender."""
from dataclasses import replace
from local_app.config import load_settings
from local_app.reservations import ReservationService
from local_app.runtime import create_runtime
from support.native_runtime import NativeRuntimeCase


class NativeStateLossTests(NativeRuntimeCase):
    def test_failed_installation_verification_does_not_poison_first_initialization(self):
        from unittest.mock import patch
        from local_app.models import AppError
        from local_app.native.engine import NativeEngine
        from local_app.native.protocol import NativeProcess, Endpoint
        data=self.directory/'fresh';data.mkdir(mode=0o700)
        settings=replace(self.settings,data_dir=data)
        with patch('local_app.native.engine.verify_selection',side_effect=AppError('native_build_missing','synthetic missing build')):
            with self.assertRaises(AppError):NativeEngine(settings,self.db)
        self.assertFalse((data/'native').exists())
        def isolated_process(directory, binary, **kwargs):
            return NativeProcess(directory, binary,
                endpoint=Endpoint.fixture(self.fixture.port, self.fixture.cert), **kwargs)
        # Exercise the real ledger initialization through this private TLS
        # fixture, including when a worktree shares its cached binary by symlink.
        with patch('local_app.native.engine.verify_selection',return_value=self.build), \
                patch('local_app.native.engine.NativeProcess',side_effect=isolated_process):
            engine=NativeEngine(settings,self.db)
            try:self.assertTrue((data/'native-state.json').is_file())
            finally:engine.close()
        self.assertEqual(self.fixture.posts(),[])

    def test_missing_state_directory_never_creates_empty_replacement_ledger(self):
        run=self.plan(delay=300);row=self.runtime.store.ensure_run(run)
        binding={'job':'claimed-before-crash','epoch':1,'session':1}
        self.db.campus_runs.update_one({'_id':run.key},{'$set':{'native_binding':binding,'status':'preparing'}})
        self.runtime.close()
        directory=self.directory/'native';directory.rename(self.directory/'held-original-state')
        runtime=create_runtime(self.settings,client_factory=self.client_factory,native_factory=self.native_factory)
        self.addCleanup(runtime.close);runtime.start()
        self.assertEqual(runtime.scheduler.status()['executor_state'],'blocked')
        self.assertFalse((directory/'ledger').exists())
        row=self.db.campus_runs.find_one({'_id':run.key})
        self.assertEqual(row['native_binding'],binding);self.assertEqual(row['status'],'preparing')
        self.assertEqual(self.fixture.posts(),[])

    def test_missing_selection_with_native_state_is_blocked_cpp(self):
        settings=load_settings(self.root,{'data_dir':self.directory,'mongo_uri':self.mongo.uri,'database_name':self.db.name})
        self.assertEqual(settings.executor,'cpp')
        self.assertEqual(settings.executor_release,'invalid')

    def test_explicit_python_rollback_cannot_take_native_owned_work(self):
        run=self.plan();self.runtime.store.ensure_run(run)
        self.db.campus_runs.update_one({'_id':run.key},{'$set':{'status':'preparing',
            'native_binding':{'job':'native-owner','epoch':1,'session':1}}})
        self.runtime.store.recover_interrupted()
        row=self.db.campus_runs.find_one({'_id':run.key})
        self.assertEqual(row['status'],'uncertain')
        # Also protect a queued record that predates the new startup classifier.
        self.db.campus_runs.update_one({'_id':run.key},{'$set':{'status':'queued'}})
        service=ReservationService(self.runtime.accounts,self.runtime.store,True,wall_clock=lambda:run.due_at+.1)
        result=service.execute(run)
        self.assertEqual(result.status,'uncertain')
        self.assertEqual(self.fixture.posts(),[])
