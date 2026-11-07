"""Poisoned DNS regression against real private TLS; no school booking."""
from contextlib import ExitStack
import socket
from unittest.mock import patch
import unittest
import requests

from local_app.inbox import InboxWorker
from support.native_school import CampusFixture


class InboxRouteRegressionTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.fixture=self.stack.enter_context(CampusFixture())
        self.origin='https://libseat.njfu.edu.cn:'+str(self.fixture.port)
        self.received=[]
        fixture=self.fixture;received=self.received
        def post(handler):
            handler.rfile.read(int(handler.headers.get('Content-Length','0')))
            received.append({'host':handler.headers.get('Host'),'path':handler.path,
                             'authorization':handler.headers.get('Authorization')})
            handler.reply({'ok':True})
        self.stack.enter_context(patch.object(fixture.server.RequestHandlerClass,'do_POST',post))
        original_session=requests.Session
        def session():
            result=original_session();result.verify=str(fixture.cert);return result
        self.stack.enter_context(patch('local_app.inbox.requests.Session',side_effect=session))
        original_dns=socket.getaddrinfo;self.domain_queries=[]
        def poisoned(host,port,*args,**kwargs):
            if host=='libseat.njfu.edu.cn':
                self.domain_queries.append(host)
                return original_dns('127.0.0.2',port,*args,**kwargs)
            return original_dns(host,port,*args,**kwargs)
        self.stack.enter_context(patch('socket.getaddrinfo',side_effect=poisoned))
        self.worker=InboxWorker.__new__(InboxWorker)
        self.worker.config={'origin':self.origin,'token':'fixture-only-token',
                            'connect_route':{'origin':self.origin,'connect_ip':'127.0.0.1'}}

    def test_sync_connects_despite_fake_dns_with_correct_host_and_one_post(self):
        try:result=self.worker.request('poll',{'quiet_until':None})
        except requests.RequestException:result=None
        self.assertEqual(result,{'ok':True})
        self.assertEqual(self.domain_queries,[])
        self.assertEqual(self.received,[{'host':'libseat.njfu.edu.cn:'+str(self.fixture.port),
             'path':'/_inbox/poll','authorization':'Bearer fixture-only-token'}])
        self.assertEqual(self.fixture.posts(),[])

    def test_route_keeps_sni_and_rejects_a_wrong_hostname_certificate(self):
        observed=[]
        self.fixture.server.socket.context.set_servername_callback(lambda sock,name,ctx:observed.append(name))
        self.worker.config['origin']='https://wrong-host.invalid:'+str(self.fixture.port)
        self.worker.config['connect_route']['origin']=self.worker.config['origin']
        with self.assertRaises(requests.exceptions.SSLError):
            self.worker.request('poll',{})
        self.assertEqual(observed,['wrong-host.invalid'])
        self.assertEqual(self.received,[])

    def test_original_hostname_is_used_for_tls_sni(self):
        observed=[]
        self.fixture.server.socket.context.set_servername_callback(lambda sock,name,ctx:observed.append(name))
        self.assertEqual(self.worker.request('poll',{}),{'ok':True})
        self.assertEqual(observed,['libseat.njfu.edu.cn'])

    def test_http_error_does_not_replay_post(self):
        calls=[]
        def unavailable(handler):
            handler.rfile.read(int(handler.headers.get('Content-Length','0')))
            calls.append(handler.path);handler.reply({'error':'fixture'},503)
        with patch.object(self.fixture.server.RequestHandlerClass,'do_POST',unavailable):
            from local_app.models import AppError
            with self.assertRaises(AppError):self.worker.request('ack',{})
        self.assertEqual(calls,['/_inbox/ack'])

    def test_connection_drop_after_receiving_body_does_not_replay_post(self):
        calls=[]
        def dropped(handler):
            handler.rfile.read(int(handler.headers.get('Content-Length','0')))
            calls.append(handler.path);handler.close_connection=True
            handler.connection.shutdown(socket.SHUT_RDWR);handler.connection.close()
        with patch.object(self.fixture.server.RequestHandlerClass,'do_POST',dropped):
            with self.assertRaises(requests.RequestException):self.worker.request('ack',{})
        self.assertEqual(calls,['/_inbox/ack'])

    def test_redirect_is_not_followed_and_does_not_leak_authorization(self):
        calls=[]
        def redirected(handler):
            handler.rfile.read(int(handler.headers.get('Content-Length','0')))
            calls.append(handler.path);handler.reply({},307,{'Location':'https://wrong-host.invalid/_inbox/ack'})
        with patch.object(self.fixture.server.RequestHandlerClass,'do_POST',redirected):
            from local_app.models import AppError
            with self.assertRaises(AppError):self.worker.request('ack',{})
        self.assertEqual(calls,['/_inbox/ack'])

    def test_disabled_tls_verification_is_rejected_before_connection(self):
        from local_app.inbox_transport import configure_session
        original=requests.sessions.Session
        with original() as session:
            session.trust_env=False
            configure_session(session,self.origin,self.worker.config['connect_route'])
            with self.assertRaises(requests.exceptions.SSLError):
                session.post(self.origin+'/_inbox/ack',json={},verify=False)
        self.assertEqual(self.received,[])

    def test_proxy_override_is_rejected_before_connection(self):
        from local_app.inbox_transport import configure_session
        with requests.sessions.Session() as session:
            session.trust_env=False
            configure_session(session,self.origin,self.worker.config['connect_route'])
            with self.assertRaises(requests.exceptions.InvalidURL):
                session.post(self.origin+'/_inbox/ack',json={},proxies={'https':'http://127.0.0.1:9'})
        self.assertEqual(self.received,[])

    def test_untrusted_certificate_is_rejected_before_body_is_sent(self):
        from local_app.inbox_transport import configure_session
        with requests.sessions.Session() as session:
            session.trust_env=False
            configure_session(session,self.origin,self.worker.config['connect_route'])
            with self.assertRaises(requests.exceptions.SSLError):
                session.post(self.origin+'/_inbox/ack',json={})
        self.assertEqual(self.received,[])

    def test_adapter_rejects_a_different_origin_before_connection(self):
        from local_app.inbox_transport import OriginIPAdapter
        adapter=OriginIPAdapter(self.origin,'127.0.0.1')
        try:
            request=requests.Request('POST','https://wrong-host.invalid/_inbox/ack',
                                     headers={'Authorization':'Bearer fixture'}).prepare()
            with self.assertRaises(requests.exceptions.InvalidURL):adapter.send(request)
        finally:adapter.close()
        self.assertEqual(self.received,[])

    def test_absent_route_preserves_the_original_dns_path(self):
        self.worker.config.pop('connect_route')
        with self.assertRaises(requests.RequestException):self.worker.request('poll',{})
        self.assertEqual(self.domain_queries,['libseat.njfu.edu.cn'])
        self.assertEqual(self.received,[])


class InboxRouteConfigTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        import tempfile
        self.temp=tempfile.TemporaryDirectory(prefix='njfu-inbox-route-');self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name);self.folder=self.data/'inbox';self.folder.mkdir(mode=0o700)
        self.path=self.folder/'route.json';self.origin='https://njfuautolib.duckdns.org:45581'

    def write(self,**changes):
        import json
        value={'schema':1,'origin':self.origin,'connect_ip':'183.66.27.20'}|changes
        self.path.write_text(json.dumps(value));self.path.chmod(0o600)

    def test_private_exact_route_loads_without_password_fields(self):
        from local_app.inbox_transport import read_route
        self.write()
        self.assertEqual(read_route(self.data,self.origin),
                         {'origin':self.origin,'connect_ip':'183.66.27.20'})

    def test_missing_route_keeps_legacy_behavior(self):
        from local_app.inbox_transport import read_route
        self.assertIsNone(read_route(self.data,self.origin))

    def test_world_readable_route_is_rejected(self):
        from local_app.inbox_transport import read_route
        from local_app.models import AppError
        self.write();self.path.chmod(0o644)
        with self.assertRaises(AppError):read_route(self.data,self.origin)

    def test_symlink_route_is_rejected(self):
        from local_app.inbox_transport import read_route
        from local_app.models import AppError
        self.write();target=self.folder/'target.json';self.path.rename(target);self.path.symlink_to(target)
        with self.assertRaises(AppError):read_route(self.data,self.origin)

    def test_wrong_origin_and_nonpublic_or_fake_ips_are_rejected(self):
        from local_app.inbox_transport import read_route
        for values in ({'origin':'https://other.invalid:45581'},{'connect_ip':'198.18.0.29'},
                       {'connect_ip':'127.0.0.1'},{'connect_ip':'10.0.0.1'},
                       {'connect_ip':'183.66.27.20:45581'},{'connect_ip':134744072}):
            with self.subTest(values=values):
                self.write(**values)
                with self.assertRaises(ValueError):read_route(self.data,self.origin)

    def unavailable(self):
        import json
        from types import SimpleNamespace
        from local_app.inbox import configured_worker
        token=self.folder/'worker-token';token.write_text('a'*64);token.chmod(0o600)
        config=self.folder/'config.json';config.write_text(json.dumps({'origin':self.origin,'token_file':str(token)}));config.chmod(0o600)
        try:worker=configured_worker(SimpleNamespace(data_dir=self.data),None,None,None,None)
        except RecursionError:worker=None
        self.assertIsNotNone(worker,'Invalid optional route must not abort Runtime initialization')
        self.assertTrue(worker.status()['configured'])
        self.assertFalse(worker.status()['connected'])
        worker.start();worker.close()

    def test_invalid_route_is_only_an_unavailable_optional_inbox(self):
        self.write(schema=2);self.unavailable()

    def test_deeply_nested_route_does_not_abort_the_reservation_runtime(self):
        self.path.write_text('['*1500+'0'+']'*1500);self.path.chmod(0o600)
        self.unavailable()
