"""Production native transport with real loopback TLS, no production sockets."""
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'AL-BackEnd'))
from local_app.models import AppError,RunSpec,ZONE
from local_app.elapsed_clock import elapsed_time
from support.native_school import CampusFixture
try:
    from local_app.native.protocol import NativeProcess,Endpoint,ProtocolError,make_state,clock_ns
    from local_app.native.build import build_native
    from local_app.native.request import prepare_payload,capture_request
except ModuleNotFoundError as exc:
    if not exc.name.startswith('local_app.native'):raise
    NativeProcess=Endpoint=ProtocolError=build_native=None


class NativeTransportTests(unittest.TestCase):
    build=None
    build_temp=None
    @classmethod
    def tearDownClass(cls):
        if cls.build_temp:cls.build_temp.cleanup()
    def setUp(self):
        self.assertIsNotNone(NativeProcess,'Pure production native transport is not implemented')
        if self.__class__.build is None:
            self.__class__.build_temp=tempfile.TemporaryDirectory(prefix='njfu-native-build-')
            self.__class__.build=build_native(ROOT,Path(self.build_temp.name))
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.directory=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='njfu-n-')))
        self.fixture=self.stack.enter_context(CampusFixture());self.fixture.register('2312000001',123)
        self.native=None
    def start(self,count=2,*,cert=None):
        endpoint=Endpoint.fixture(self.fixture.port,cert or self.fixture.cert)
        self.native=NativeProcess(self.directory,self.build.executable,endpoint=endpoint,count=count)
        self.addCleanup(self.native.close)
        return self.native

    def test_import_has_no_socket_environment_or_path_side_effects(self):
        program='''import socket,sys,os,json
connect=socket.socket.connect;dns=socket.getaddrinfo;paths=list(sys.path);env=dict(os.environ)
import local_app.native.protocol,local_app.native.request,local_app.native.build
assert socket.socket.connect is connect and socket.getaddrinfo is dns
assert list(sys.path)==paths and dict(os.environ)==env
print("isolated_import_ok")
'''
        result=subprocess.run([sys.executable,'-B','-c',program],cwd=ROOT/'AL-BackEnd',
            capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('isolated_import_ok',result.stdout)

    def test_startup_is_network_idle_and_warm_is_explicit_readonly_tls(self):
        native=self.start();self.assertEqual(self.fixture.events,[])
        result=native.workers[0].warm(1000)
        self.assertGreater(result['warmed_ns'],0)
        self.assertEqual(self.fixture.posts(),[])
        self.assertEqual(self.fixture.warm_count,1)
        config=native.config()
        self.assertEqual(config['wire_protocol'],'campus-v1')
        self.assertEqual(config['endpoint_mode'],'fixture')
        self.assertEqual(config['variant'],'wal-raw-t')

    def test_unsafe_capacity_and_fixture_endpoint_are_rejected_before_launch(self):
        with self.assertRaises((AppError,ValueError)):
            NativeProcess(self.directory,self.build.executable,endpoint=Endpoint.fixture(self.fixture.port,self.fixture.cert),count=25)
        for port in (443,5003,27019,65536):
            with self.subTest(port=port),self.assertRaises((AppError,ValueError)):
                Endpoint.fixture(port,self.fixture.cert)
        production=Endpoint.production()
        self.assertEqual(production.origin,'https://libseat.njfu.edu.cn')

    def test_invalid_ca_cannot_warm_or_send(self):
        cert=self.directory/'wrong.pem';cert.write_text('not a trusted certificate')
        native=self.start(cert=cert)
        with self.assertRaises(ProtocolError):native.workers[0].warm(500)
        self.assertEqual(self.fixture.posts(),[])

    def test_production_debug_and_variant_overrides_are_unavailable(self):
        native=self.start()
        with self.assertRaises(ProtocolError):native.rpc('FAULT 7')
        self.assertEqual(native.config()['variant'],'wal-raw-t')
        self.assertEqual(self.fixture.posts(),[])

    def test_readonly_job_status_survives_control_revocation(self):
        native=self.start();native.close_control()
        self.assertEqual(native.job_status('absent'),'NOTFOUND')
        self.assertEqual(self.fixture.posts(),[])

    def test_idle_close_acknowledges_quit_and_exits_normally(self):
        native=self.start();child=native.process
        native.close()
        self.assertEqual(child.returncode,0)
        self.assertEqual(self.fixture.posts(),[])

    def test_reopening_reuses_identity_and_never_resets_the_ledger(self):
        first=self.start();identity=first.identity;epoch=first.epoch
        first.state_update('global',make_state('global',1,credential=0))
        first.close()
        second=NativeProcess(self.directory,self.build.executable,
            endpoint=Endpoint.fixture(self.fixture.port,self.fixture.cert),count=2)
        self.addCleanup(second.close)
        self.assertEqual(second.identity,identity);self.assertGreater(second.epoch,epoch)
        with self.assertRaises((AppError,ValueError,ProtocolError)):
            NativeProcess(self.directory,self.build.executable,
                endpoint=Endpoint.fixture(self.fixture.port,self.fixture.cert),count=2,initialize=True)

    def test_parent_death_exits_orphan_and_releases_state(self):
        target=self.directory/'child';target.mkdir(mode=0o700)
        program='''import os,sys,json
from pathlib import Path
from local_app.native.protocol import NativeProcess,Endpoint
n=NativeProcess(Path(sys.argv[1]),Path(sys.argv[2]),endpoint=Endpoint.fixture(int(sys.argv[3]),Path(sys.argv[4])),count=1)
print(json.dumps({"pid":n.process.pid,"identity":n.identity}),flush=True)
os._exit(0)
'''
        result=subprocess.run([sys.executable,'-B','-c',program,str(target),str(self.build.executable),
            str(self.fixture.port),str(self.fixture.cert)],cwd=ROOT/'AL-BackEnd',capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr);info=json.loads(result.stdout)
        until=time.monotonic()+4
        while time.monotonic()<until:
            try:os.kill(info['pid'],0)
            except ProcessLookupError:break
            time.sleep(.02)
        else:self.fail('Orphan native sender remained alive after parent death')
        reopened=NativeProcess(target,self.build.executable,endpoint=Endpoint.fixture(self.fixture.port,self.fixture.cert),count=1)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.identity,info['identity'])

    def test_actual_canonical_encrypted_first_send_uses_fixed_school_path(self):
        native=self.start(count=1);native.workers[0].warm(1000)
        client=self.fixture.client('2312000001');self.addCleanup(client.close);client.login()
        day=datetime.now(ZONE).date().isoformat()
        run=RunSpec('account1',1,day,'09:00','12:00',('1',),time.time()+.3)
        prepare_payload(client,run,'1');cap=capture_request(client,run,'1')
        native.state_update('global',make_state('global',1,credential=0))
        native.state_update('account1',make_state('account1',1))
        due=clock_ns()+300_000_000
        job={'id':'actual-packet','worker':0,'account':'account1','day':day,'kind':'S','operation':'-',
            'account_version':1,'global_version':1,'credential':1,'due_ns':due,
            'created_ns':cap.created_ns,'expires_ns':min(cap.expires_ns,due+1_000_000_000),'body':cap.encode()}
        native.lease(500);native.arm([job],'D');native.workers[0].arm_wait(job)
        result=native.workers[0].result()
        self.assertEqual(result['curl_code'],0);self.assertEqual(result['http_status'],200)
        self.assertEqual(result['journal_ok'],1);self.assertGreaterEqual(result['dispatch_ns'],due)
        self.assertEqual(len(self.fixture.posts()),1)
        self.assertEqual(self.fixture.posts()[0]['path'],'/ic-web/reserve/seat')
        self.assertEqual(self.fixture.posts()[0]['seat_id'],'1')
        for path in native.state.iterdir():
            if path.is_file():
                raw=path.read_bytes()
                self.assertNotIn(self.fixture.accounts['2312000001']['token'].encode(),raw)
                self.assertNotIn(b'fixture-password',raw)


if __name__=='__main__':unittest.main()
