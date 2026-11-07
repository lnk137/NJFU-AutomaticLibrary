"""Real Runtime/private Mongo/TLS fixtures with explicit native endpoint injection."""
from contextlib import ExitStack
from datetime import datetime
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from local_app.config import load_settings
from local_app.models import Seat,ZONE,resolve_run,validate_plan
from support.auth_fixture import root_session
from support.mongo_fixture import MongoFixture
from support.native_school import CampusFixture
try:
    from local_app.native.engine import NativeEngine
except ModuleNotFoundError as exc:
    if exc.name!='local_app.native.engine':raise
    NativeEngine=None


class NativeRuntimeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        self.assertIsNotNone(NativeEngine,'Native engine is not integrated into existing Runtime')
        from local_app.native.build import build_native
        from local_app.native.protocol import NativeProcess,Endpoint
        from local_app.runtime import create_runtime
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.directory=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='njfu-r-'))).resolve()
        self.fixture=self.stack.enter_context(CampusFixture())
        self.students=[str(2312000001+index) for index in range(20)]
        for index,student in enumerate(self.students):self.fixture.register(student,1000+index)
        self.db=self.mongo.database();self.addCleanup(self.mongo.client.drop_database,self.db.name)
        self.root=Path(__file__).resolve().parents[2]
        self.build=build_native(self.root)
        self.catalog={str(index):Seat(str(index),f'A{index:03}','2F') for index in range(1,27)}
        self.stack.enter_context(patch('local_app.runtime.load_catalog',return_value=self.catalog))
        self.settings=load_settings(self.root,{'data_dir':self.directory,'mongo_uri':self.mongo.uri,
            'database_name':self.db.name,'allow_live':True,'executor':'cpp'})
        def factory(student,password,**kwargs):return self.fixture.client(student,password,**kwargs)
        factory.isolated_fixture=True;self.client_factory=factory
        def native_factory(directory):
            return NativeProcess(directory,self.build.executable,
                endpoint=Endpoint.fixture(self.fixture.port,self.fixture.cert),count=20)
        native_factory.isolated_fixture=True;self.native_factory=native_factory
        self.runtime=create_runtime(self.settings,client_factory=factory,native_factory=native_factory)
        self.addCleanup(self.runtime.close)
        self.runtime.auth.initialize_root('isolated-root-password',self.students)
        self.account=self.runtime.accounts.verify_and_save(self.students[0],'fixture-password')
        self.client=self.runtime.app.test_client();root_session(self.runtime.auth,self.client)
        self.origin='http://127.0.0.1:5003';self.csrf=self.client.get('/api/local/bootstrap',base_url=self.origin).json['csrf']
    def request(self,method,path,data):
        return self.client.open(path,method=method,json=data,base_url=self.origin,
            headers={'Origin':self.origin,'X-CSRF-Token':self.csrf})
    def plan(self,account=None,delay=6,due=None):
        account=account or self.account;due=due or math.ceil(time.time()+delay)
        primary=str(int(account.student_id)-2312000000)
        saved=self.runtime.accounts.save_plan(account.id,{'seat_ids':[primary,'26'],'enabled':True,
            'day_offset':1,'mode':'day','begin':'09:00','end':'12:00',
            'trigger':datetime.fromtimestamp(due,ZONE).strftime('%H:%M:%S')},account.revision)
        if saved.id==self.account.id:self.account=saved
        return resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.catalog),datetime.fromtimestamp(due,ZONE).date())
    def wait(self,key,states,timeout=12):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            row=self.db.campus_runs.find_one({'_id':key})
            if row and row['status'] in states:return row
            time.sleep(.01)
        self.fail('Task did not finish: '+str(self.db.campus_runs.find_one({'_id':key})))
    def wait_arm(self,run):
        until=time.monotonic()+8
        while time.monotonic()<until:
            row=self.db.campus_runs.find_one({'_id':run.key})
            if row and row.get('native_binding'):
                status=self.runtime.native.process.job_status(row['native_binding']['job'])
                if status!='NOTFOUND':return row
            time.sleep(.005)
        self.fail('Actual native ARM not observed')
