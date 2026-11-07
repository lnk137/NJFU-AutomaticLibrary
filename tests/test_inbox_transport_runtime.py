"""Native Runtime + cloud queue over real loopback TLS with poisoned DNS."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

import requests

from deploy.inbox_gateway import create_gateway
from deploy.inbox_store import InboxStore
from local_app.inbox import InboxWorker
from support.native_runtime import NativeRuntimeCase


class InboxTransportRuntimeTests(NativeRuntimeCase):
    def test_two_configs_commit_and_ack_once_preserving_other_plan_and_no_booking(self):
        b=self.runtime.accounts.verify_and_save(self.students[1],'fixture-password')
        control=self.runtime.accounts.verify_and_save(self.students[2],'fixture-password')
        due=time.time()+600
        for account in (self.account,b,control):self.plan(account,due=due)
        self.runtime.start()
        cloud_root=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='njfu-cloud-store-')))
        cloud=InboxStore(cloud_root/'private/inbox.sqlite')
        worker_token='b'*64
        holder={};leased=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(handler):
                data=json.loads(handler.rfile.read(int(handler.headers.get('Content-Length','0'))))
                with holder['app'].test_client() as client:
                    response=client.open(handler.path,method='POST',base_url=holder['origin'],
                        json=data,headers={'Authorization':handler.headers.get('Authorization','')})
                    raw=response.data
                    if handler.path=='/_inbox/poll' and response.status_code==200:
                        leased.extend(response.json['jobs'])
                handler.send_response(response.status_code)
                handler.send_header('Content-Type','application/json')
                handler.send_header('Content-Length',str(len(raw)));handler.end_headers()
                handler.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.fixture.cert,self.fixture.private)
        server.socket=context.wrap_socket(server.socket,server_side=True)
        origin='https://libseat.njfu.edu.cn:'+str(server.server_address[1])
        holder.update(origin=origin,app=create_gateway({'origin':origin,'worker_token':worker_token,
            'relay_token':'fixture-relay'},cloud))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def stop():server.shutdown();server.server_close();thread.join(2)
        self.addCleanup(stop)
        worker=InboxWorker(self.settings,self.runtime.accounts,self.runtime.store,
            self.runtime.auth,self.runtime.scheduler,{'origin':origin,'token':worker_token,
                'connect_route':{'origin':origin,'connect_ip':'127.0.0.1'}})
        self.addCleanup(worker.close)
        packet=worker.engine.snapshot(self.runtime.scheduler.status());cloud.publish(packet)
        old={}
        expected={}
        for account,seat in ((self.account,'4'),(b,'5')):
            row=self.runtime.store.get_account(account.id);old[account.id]=row['revision']
            plan=row['plan']|{'seat_ids':[seat]}
            token=cloud.issue(cloud.authenticate(account.student_id,'fixture-password','fixture'))
            job=cloud.enqueue(account.student_id,{'request_id':str(uuid.uuid4()),
                'expected_revision':row['revision'],'plan':plan},token)
            expected[account.id]=(account.student_id,job['request_id'],plan)
        control_before=self.runtime.store.get_account(control.id)
        session_class=requests.Session
        def session():
            value=session_class();value.verify=str(self.fixture.cert);return value
        original_dns=socket.getaddrinfo;lookups=[]
        def poisoned(host,port,*args,**kwargs):
            if host=='libseat.njfu.edu.cn':
                lookups.append(host);return original_dns('127.0.0.2',port,*args,**kwargs)
            return original_dns(host,port,*args,**kwargs)
        with patch('local_app.inbox.requests.Session',side_effect=session),patch('socket.getaddrinfo',side_effect=poisoned):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                worker.tick()
                if all(cloud.receipt(student,key)['state']=='applied' for student,key,_ in expected.values()):break
                time.sleep(.05)
            else:self.fail('Cloud ACK was not confirmed')
            for account_id,(student,key,plan) in expected.items():
                row=self.runtime.store.get_account(account_id)
                self.assertEqual(row['revision'],old[account_id]+1)
                self.assertEqual(row['plan'],plan)
                self.assertEqual(row['last_inbox']['request_id'],key)
                self.assertEqual(cloud.receipt(student,key)['applied_revision'],row['revision'])
                self.assertEqual(self.runtime.native.bridge.outbox.projection(account_id)['plan'],plan)
            replay=next(job for job in leased if job['student_id']==self.account.student_id)
            before=self.runtime.store.get_account(self.account.id)
            self.assertEqual(worker.engine.apply(replay)['state'],'applied')
            self.assertEqual(before,self.runtime.store.get_account(self.account.id))
        self.assertEqual(lookups,[])
        self.assertEqual(control_before,self.runtime.store.get_account(control.id))
        self.assertEqual(self.runtime.store.runs.count_documents({}),0)
        self.assertEqual(self.runtime.scheduler.status()['capacity'],20)
        self.assertTrue(self.runtime.scheduler.status()['ready_to_submit'])
        self.assertEqual(self.fixture.posts(),[])

    def test_malformed_route_keeps_actual_native_runtime_ready(self):
        from local_app.runtime import create_runtime
        self.runtime.close()
        folder=self.directory/'inbox';folder.mkdir(mode=0o700)
        token=folder/'worker-token';token.write_text('c'*64);token.chmod(0o600)
        origin='https://njfuautolib.duckdns.org:45581'
        config=folder/'config.json';config.write_text(json.dumps({'origin':origin,'token_file':str(token)}));config.chmod(0o600)
        route=folder/'route.json';route.write_text('['*1500+'0'+']'*1500);route.chmod(0o600)
        runtime=create_runtime(self.settings,client_factory=self.client_factory,native_factory=self.native_factory)
        self.addCleanup(runtime.close);runtime.start()
        self.assertTrue(runtime.scheduler.status()['ready_to_submit'])
        self.assertEqual(runtime.scheduler.status()['capacity'],20)
        self.assertFalse(runtime.inbox.status()['connected'])
        self.assertEqual(self.fixture.posts(),[])
