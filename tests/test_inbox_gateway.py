"""Cloud authentication/inbox tests with no production credentials or school traffic."""
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from deploy.inbox_store import InboxStore
from deploy.inbox_gateway import create_gateway, Reply
from local_app.models import AppError

VERIFIER = generate_password_hash('fixture-password')


def snapshot(generation=1):
    return {'generation': generation, 'runtime': {'allow_live': True}, 'policy': {}, 'seats': [{'id':'1','name':'A001','location':'2F'}],
            'accounts': [{'student_id': '2310000001', 'enabled': True,
                'auth': {'generation': '1:1:1', 'password_hash': VERIFIER, 'blocked': None,'success_id':'initial-success'},
                'account': {'id': 'account-a', 'student_id': '2310000001', 'revision': 3,
                            'credential_revision': 1, 'alias': 'Fixture', 'plan': None},
                'records': {'records': [], 'checked_at': None}, 'runs': {'items': [], 'counts': {}, 'next_cursor': None}}]}


class InboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-inbox-test-')
        self.now = 1792872000.
        self.store = InboxStore(Path(self.temp.name) / 'private' / 'inbox.sqlite', clock=lambda: self.now)
        self.store.publish(snapshot())
        self.user = self.store.authenticate('2310000001', 'fixture-password', '198.51.100.1')
        self.token = self.store.issue(self.user)

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, request_id=None, **changes):
        return self.store.enqueue(self.user['student_id'], {'request_id': request_id or str(uuid.uuid4()),
            'expected_revision': 3, 'plan': {'enabled': True}, **changes}, self.token)

    def test_only_verified_saved_allowed_users_can_authenticate(self):
        for index, change in enumerate(({'enabled': False}, {'auth': None}, {'account': None},
                       {'auth': {'generation': '2', 'password_hash': VERIFIER, 'blocked': 'login_failed'}})):
            packet = snapshot(index*2+2); packet['accounts'][0].update(change)
            self.store.publish(packet)
            with self.assertRaises(AppError):
                self.store.authenticate('2310000001', 'fixture-password', '198.51.100.2')
            self.store.publish(snapshot(index*2+3))
        with self.assertRaises(AppError):
            self.store.authenticate('2310000002', 'fixture-password', '198.51.100.3')

    def test_old_snapshot_or_wrong_password_never_grants_offline_access(self):
        with self.assertRaises(AppError):
            self.store.authenticate('2310000001', 'wrong', '198.51.100.2')
        self.now += 86401
        with self.assertRaises(AppError):
            self.store.authenticate('2310000001', 'fixture-password', '198.51.100.3')

    def test_session_is_revoked_by_generation_change(self):
        token = self.store.issue(self.user)
        self.assertEqual(self.store.current(token)['student_id'], '2310000001')
        packet = snapshot(2); packet['accounts'][0]['auth']['generation'] = '1:2:1'
        self.store.publish(packet)
        self.assertIsNone(self.store.current(token))

    def test_identical_retry_has_one_version_and_pending_is_not_effective(self):
        request_id = str(uuid.uuid4())
        first = self.enqueue(request_id); second = self.enqueue(request_id)
        self.assertEqual(first['version'], 1); self.assertEqual(first, second)
        self.assertEqual(first['state'], 'pending'); self.assertIsNone(first.get('applied_revision'))
        with self.assertRaises(AppError):
            self.enqueue(request_id, plan={'enabled': False})
        with self.assertRaises(AppError): self.enqueue()
        self.assertEqual(self.store.summary()['pending'], 1)

    def test_acknowledgement_requires_matching_lease_and_local_commit_revision(self):
        receipt = self.enqueue(); jobs = self.store.poll()
        self.assertEqual(len(jobs), 1); self.assertEqual(self.store.poll(), [])
        job = jobs[0]
        with self.assertRaises(AppError):
            self.store.ack(job['request_id'], 'forged', {'state': 'applied', 'revision': 4})
        with self.assertRaises(AppError):
            self.store.ack(job['request_id'], job['lease'], {'state': 'applied'})
        self.store.ack(job['request_id'], job['lease'], {'state': 'applied', 'revision': 4})
        self.assertEqual(self.store.receipt('2310000001', receipt['request_id'])['state'], 'applied')
        self.assertEqual(self.store.summary()['pending'], 0)
        self.assertEqual(self.store.public_account('2310000001')['revision'],4)
        self.store.publish(snapshot(2))
        self.assertEqual(self.store.public_account('2310000001')['revision'],4)

    def test_expired_lease_replays_same_job_and_stale_ack_cannot_win(self):
        self.enqueue(); first = self.store.poll()[0]; self.now += 61
        second = self.store.poll()[0]
        self.assertEqual(first['request_id'], second['request_id']); self.assertNotEqual(first['lease'], second['lease'])
        with self.assertRaises(AppError):
            self.store.ack(first['request_id'], first['lease'], {'state': 'failed', 'code': 'fixture'})

    def test_expired_or_changed_generation_queue_is_not_applied(self):
        self.enqueue(); self.now += 2*86400+1
        self.assertEqual(self.store.poll(), []); self.assertEqual(self.store.summary()['failed'], 1)

    def test_revoked_account_queue_is_terminal_and_other_accounts_remain_independent(self):
        self.enqueue(); packet = snapshot(2); packet['accounts'][0]['enabled'] = False
        self.store.publish(packet)
        self.assertEqual(self.store.poll(), []); self.assertEqual(self.store.summary()['failed'], 1)

    def test_private_storage_and_public_summary_never_expose_verifier(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(VERIFIER, str(self.store.summary()))
        self.assertNotIn('password_hash', str(self.store.public_account('2310000001')))

    def test_older_snapshot_cannot_replace_newer_auth_or_plan_state(self):
        packet = snapshot(5); packet['accounts'][0]['account']['revision'] = 8
        self.store.publish(packet); self.store.publish(snapshot(4))
        self.assertEqual(self.store.public_account('2310000001')['revision'], 8)

    def test_concurrent_old_auth_snapshot_cannot_clear_explicit_school_rejection(self):
        self.store.block('2310000001','login_failed')
        self.store.publish(snapshot(2))
        with self.assertRaises(AppError):
            self.store.authenticate('2310000001','fixture-password','198.51.100.3')
        permission_change=snapshot(3);permission_change['accounts'][0]['auth']['generation']='1:2:1'
        self.store.publish(permission_change)
        with self.assertRaises(AppError):
            self.store.authenticate('2310000001','fixture-password','198.51.100.5')
        confirmed=snapshot(4);confirmed['accounts'][0]['auth']['generation']='2:1:3'
        confirmed['accounts'][0]['auth']['success_id']='new-school-success'
        self.store.publish(confirmed)
        self.assertEqual(self.store.authenticate('2310000001','fixture-password','198.51.100.4')['role'],'student')

    def test_resolved_old_failure_does_not_inflate_current_failed_account_count(self):
        first=self.enqueue();job=self.store.poll()[0]
        self.store.ack(job['request_id'],job['lease'],{'state':'failed','code':'fixture','message':'old failure'})
        second=self.enqueue();job=self.store.poll()[0]
        self.store.ack(job['request_id'],job['lease'],{'state':'applied','revision':4})
        self.assertEqual(self.store.summary()['failed'],0)
        self.assertEqual(self.store.receipt('2310000001',first['request_id'])['state'],'failed')


class FakeUpstream:
    online = False
    result = Reply(401, b'{"error":"school rejected","code":"login_failed"}')
    def probe(self, source): return self.online
    def forward(self, *args): return self.result


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-gateway-test-')
        self.store = InboxStore(Path(self.temp.name)/'private/inbox.sqlite')
        self.store.publish(snapshot()); self.upstream = FakeUpstream()
        self.config = {'origin':'https://inbox-fixture.example:45581','relay_token':'r'*64,
                       'worker_token':'w'*64,'static_root':self.temp.name}
        self.app = create_gateway(self.config, self.store, self.upstream)
        self.client = self.app.test_client(); self.origin = self.config['origin']
        self.headers = {'X-NJFU-Relay-HTTPS':'1','X-NJFU-Relay-Token':'r'*64,'X-NJFU-Client-IP':'198.51.100.2'}
        self.csrf = self.get('/bootstrap').json['csrf']

    def tearDown(self): self.temp.cleanup()
    def get(self, path): return self.client.get('/api/local'+path,base_url=self.origin,headers=self.headers)
    def post(self, path, data, **changes):
        return self.client.post('/api/local'+path,json=data,base_url=self.origin,
            headers=self.headers|{'Origin':self.origin,'X-CSRF-Token':self.csrf}|changes)
    def login(self): return self.post('/auth/login',{'kind':'student','username':'2310000001','password':'fixture-password'})

    def test_offline_success_has_restricted_session_and_no_school_operations(self):
        result = self.login(); self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['user']['auth_mode'],'offline')
        for flag in ('Secure','HttpOnly','SameSite=Strict'): self.assertIn(flag,result.headers['Set-Cookie'])
        self.assertEqual(len(self.get('/accounts').json['accounts']),1)
        for path in ('/accounts/account-a/check','/accounts/account-a/query','/accounts/account-a/today/submit','/accounts/verify'):
            self.assertEqual(self.post(path,{}).status_code,403)

    def test_online_saved_credential_refusal_is_never_an_offline_success(self):
        self.upstream.online=True
        self.upstream.result=Reply(401,b'{"code":"login_failed"}',saved_credential_rejected=True)
        response=self.login(); self.assertEqual(response.status_code,401)
        self.assertNotIn('njfu_inbox_session=',response.headers.get('Set-Cookie',''))
        self.upstream.online=False
        self.assertEqual(self.login().status_code,403)

    def test_online_timeout_or_network_error_is_returned_without_fallback(self):
        self.upstream.online=True
        self.upstream.result=Reply(503,b'{"code":"network_error","error":"campus network unavailable"}')
        response=self.login(); self.assertEqual(response.status_code,503)
        self.assertNotIn('njfu_inbox_session=',response.headers.get('Set-Cookie',''))

    def test_public_root_and_forged_origins_are_rejected(self):
        self.assertEqual(self.post('/auth/login',{'kind':'root'}).status_code,403)
        self.assertEqual(self.login().status_code,200)
        self.assertEqual(self.get('/admin/users').status_code,403)
        self.assertEqual(self.post('/auth/logout',{},Origin='https://evil.example').status_code,403)
        self.assertEqual(self.post('/auth/logout',{},**{'X-CSRF-Token':'wrong'}).status_code,403)

    def test_worker_api_rejects_browser_and_redacts_hashes_from_public_data(self):
        result=self.client.post('/_inbox/poll',json={},base_url=self.origin)
        self.assertEqual(result.status_code,403)
        self.login()
        for path in ('/auth/me','/bootstrap','/accounts','/status','/inbox'):
            result=self.get(path); self.assertNotIn('password_hash',result.get_data(as_text=True))
            self.assertNotIn(VERIFIER,result.get_data(as_text=True))

    def test_cloud_health_is_separate_from_executor_booking_readiness(self):
        result=self.client.get('/health',base_url=self.origin,headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['application'],'NJFU-AutomaticLibrary-inbox-gateway')
        self.assertNotIn('ready_to_submit',result.json)

    def test_plan_submission_remains_pending_until_local_ack(self):
        self.login(); request_id=str(uuid.uuid4())
        response=self.client.put('/api/local/accounts/account-a/plan',json={'request_id':request_id,
            'expected_revision':3,'plan':{'seat_ids':['1'],'mode':'day','time_mode':'full_day','day_offset':1,
            'trigger':'07:00:00','begin':'07:30','end':'22:00','enabled':True}},base_url=self.origin,
            headers=self.headers|{'Origin':self.origin,'X-CSRF-Token':self.csrf})
        self.assertEqual(response.status_code,202); self.assertTrue(response.json['pending'])
        self.assertIsNone(response.json['account']['plan'])

    def test_permission_generation_changed_after_session_check_rejects_enqueue(self):
        self.login(); original=self.store.current
        def race(token):
            user=original(token)
            packet=snapshot(2);packet['accounts'][0]['auth']['generation']='2:1:1';self.store.publish(packet)
            return user
        self.store.current=race
        result=self.client.put('/api/local/accounts/account-a/plan',json={'request_id':str(uuid.uuid4()),
            'expected_revision':3,'plan':{'seat_ids':['1'],'mode':'day','time_mode':'full_day','day_offset':1,
            'trigger':'07:00:00','begin':'07:30','end':'22:00','enabled':True}},base_url=self.origin,
            headers=self.headers|{'Origin':self.origin,'X-CSRF-Token':self.csrf})
        self.assertEqual(result.status_code,401);self.assertEqual(self.store.summary()['pending'],0)

    def test_expired_offline_session_never_falls_back_to_an_old_online_account(self):
        self.client.set_cookie('njfu_autolib_session','old-online-account-a',domain='inbox-fixture.example')
        self.assertEqual(self.login().status_code,200)
        self.client.set_cookie('njfu_autolib_session','old-online-account-a',domain='inbox-fixture.example')
        packet=snapshot(2);packet['accounts'][0]['auth']['generation']='2:1:1';self.store.publish(packet)
        self.upstream.online=True;self.upstream.result=Reply(200,b'{"ok":true}')
        self.assertEqual(self.post('/accounts/account-a/check',{}).status_code,401)
        self.assertIsNone(self.get('/bootstrap').json['user'])


class ProbeTests(unittest.TestCase):
    def test_response_body_timeout_is_not_a_signal_for_offline_login(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import requests
        from deploy.inbox_gateway import Upstream
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','1000');self.end_headers();self.wfile.flush();release.wait(2)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        upstream=Upstream('https://fixture.example:45581','fixture')
        upstream._send=lambda *args,**kwargs:requests.get(f'http://127.0.0.1:{server.server_port}/',timeout=.05)
        try:
            with self.assertRaises(AppError):upstream.probe('198.51.100.1')
        finally:release.set();server.shutdown();server.server_close();thread.join()


if __name__ == '__main__': unittest.main()
