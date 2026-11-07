"""Public relay keeps the school executor local and authenticates the proxy boundary."""
from pathlib import Path
import sys, unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture
from support.account_fixture import services
from support.auth_fixture import grant, root_session
from local_app.auth import AuthService
from local_app.config import load_settings
from local_app.web import create_app

ORIGIN='https://library-fixture.example:45581'
RELAY='f'*64

class PublicRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.mongo=MongoFixture()
    @classmethod
    def tearDownClass(cls):cls.mongo.close()
    def setUp(self):
        self.db=self.mongo.database();self.temp,self.store,self.service,self.catalog=services(self.db)
        self.auth=AuthService(self.store,self.service);grant(self.auth,['fixture01'])
        settings=load_settings(Path(__file__).resolve().parents[1],{'public_origin':ORIGIN,'relay_token':RELAY})
        self.app=create_app(settings,self.service,self.store,self.catalog,lambda:{'allow_live':False},auth=self.auth)
        self.client=self.app.test_client();root_session(self.auth,self.client)
        self.csrf=self.client.get('/api/local/bootstrap',base_url='http://127.0.0.1:5003').json['csrf']
        self.client.delete_cookie('njfu_autolib_session',domain='127.0.0.1')
    def tearDown(self):
        self.service.close();self.temp.cleanup();self.mongo.client.drop_database(self.db.name)
    def public(self,path,data=None,method=None,**headers):
        return self.client.open('/api/local'+path,method=method or ('GET' if data is None else 'POST'),json=data,base_url=ORIGIN,
            headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf,'X-NJFU-Relay-Token':RELAY,'X-NJFU-Relay-HTTPS':'1','X-NJFU-Client-IP':'198.51.100.2'}|headers)
    def login(self):return self.public('/auth/login',{'kind':'student','username':'fixture01','password':'pass'})
    def test_public_bootstrap_has_no_admin_login_and_no_private_data(self):
        r=self.public('/bootstrap');self.assertEqual(r.status_code,200)
        self.assertFalse(r.json['admin_login']);self.assertTrue(r.json['public_access']);self.assertIsNone(r.json['user'])
    def test_missing_or_forged_proxy_token_is_rejected(self):
        for value in ('','wrong'):
            self.assertEqual(self.public('/bootstrap',**{'X-NJFU-Relay-Token':value}).status_code,403)
    def test_insecure_or_invalid_proxy_metadata_is_rejected(self):
        for headers in ({'X-NJFU-Relay-HTTPS':''},{'X-NJFU-Client-IP':'not-an-ip'}):
            self.assertEqual(self.public('/bootstrap',**headers).status_code,403)
    def test_forwarded_host_does_not_authorize_an_unknown_host(self):
        r=self.client.get('/health',base_url='https://evil.example',headers={'X-Forwarded-Host':'127.0.0.1:5003','X-NJFU-Relay-Token':RELAY})
        self.assertEqual(r.status_code,403)
    def test_public_post_requires_exact_https_origin(self):
        for origin in ('http://library-fixture.example:45581','https://evil.example','https://library-fixture.example'):
            self.assertEqual(self.public('/auth/login',{},Origin=origin).status_code,403)
    def test_public_login_uses_school_and_sets_secure_cookie(self):
        r=self.login();self.assertEqual(r.status_code,200)
        for flag in ('Secure','HttpOnly','SameSite=Strict'):self.assertIn(flag,r.headers['Set-Cookie'])
        self.assertEqual(self.public('/accounts').json['accounts'][0]['student_id'],'fixture01')
    def test_public_root_login_is_denied_before_password_validation(self):
        r=self.public('/auth/login',{'kind':'root','username':'root','password':'fixture-only-admin-password'})
        self.assertEqual(r.status_code,403);self.assertEqual(r.json['code'],'local_admin_only')
    def test_local_root_session_cannot_be_replayed_via_public_origin(self):
        token=self.auth._issue('root');self.client.set_cookie('njfu_autolib_session',token,domain='library-fixture.example')
        self.assertEqual(self.public('/admin/users').status_code,403)
        self.assertEqual(self.public('/accounts').status_code,403)
        self.assertIsNone(self.public('/bootstrap').json['user'])
    def test_local_admin_still_works_and_ignores_untrusted_forwarded_headers(self):
        root_session(self.auth,self.client)
        r=self.client.get('/api/local/admin/users',base_url='http://127.0.0.1:5003',headers={'X-Forwarded-Proto':'https','X-Forwarded-For':'fake'})
        self.assertEqual(r.status_code,200)
    def test_public_students_still_cannot_use_admin_api(self):
        self.login();self.assertEqual(self.public('/admin/users').status_code,403)
    def test_public_student_can_see_clock_lock_and_reason_without_private_diagnostics(self):
        runtime={'allow_live':True,'scheduler_running':True,'clock_ok':False,'ready_to_submit':False,
            'last_error':'本机时间发生跳变，请核对时间后重启后台','execution_message':'自动预约已暂停',
            'private_fixture':'do-not-expose'}
        settings=load_settings(Path(__file__).resolve().parents[1],{'public_origin':ORIGIN,'relay_token':RELAY})
        self.app=create_app(settings,self.service,self.store,self.catalog,lambda:runtime,auth=self.auth)
        self.client=self.app.test_client();self.csrf=self.public('/bootstrap').json['csrf']
        self.assertEqual(self.login().status_code,200)
        result=self.public('/status');self.assertEqual(result.status_code,200)
        self.assertFalse(result.json['clock_ok']);self.assertFalse(result.json['ready_to_submit'])
        self.assertEqual(result.json['last_error'],runtime['last_error'])
        self.assertNotIn('private_fixture',result.json);self.assertNotIn('connection',result.json)
    def test_public_logout_revokes_session(self):
        self.login();self.assertEqual(self.public('/auth/logout',{}).status_code,200)
        self.assertEqual(self.public('/accounts').status_code,401)

    def test_public_student_sees_safe_recovery_warning_without_account_diagnostics(self):
        runtime={'allow_live':True,'scheduler_running':True,'clock_ok':True,'last_error':None,
                 'scheduler_warning':'后台短暂异常，正在自动重试','private_fixture':'do-not-expose'}
        settings=load_settings(Path(__file__).resolve().parents[1],{'public_origin':ORIGIN,'relay_token':RELAY})
        self.app=create_app(settings,self.service,self.store,self.catalog,lambda:runtime,auth=self.auth)
        self.client=self.app.test_client();self.csrf=self.public('/bootstrap').json['csrf']
        self.assertEqual(self.login().status_code,200)
        result=self.public('/status')
        self.assertEqual(result.json['scheduler_warning'],runtime['scheduler_warning'])
        self.assertTrue(result.json['clock_ok']);self.assertIsNone(result.json['last_error'])
        self.assertNotIn('private_fixture',result.json)

    def test_public_plan_rejects_short_friday_without_changing_saved_account(self):
        self.assertEqual(self.login().status_code,200)
        account=self.public('/accounts').json['accounts'][0]
        before=self.store.get_account(account['id'])
        plan={'seat_ids':['1'],'day_offset':1,'mode':'day','begin':'19:00','end':'22:00',
              'trigger':'07:00:00','enabled':True}
        result=self.public('/accounts/'+account['id']+'/plan',
                           {'plan':plan,'expected_revision':account['revision']},method='PUT')
        self.assertEqual(result.status_code,400);self.assertEqual(result.json['code'],'school_time')
        self.assertIn('周五',result.json['error'])
        self.assertEqual(self.store.get_account(account['id']),before)
    def test_public_cookie_errors_do_not_downgrade_to_local_access(self):
        self.client.set_cookie('njfu_autolib_session','fake',domain='library-fixture.example')
        self.assertEqual(self.public('/accounts').status_code,401)

class PublicConfigurationTests(unittest.TestCase):
    def test_http_or_ambiguous_origin_and_missing_secret_fail_closed(self):
        from local_app.models import AppError
        for origin in ['http://example.com:45581','https://example.com','https://example.com:45581/path','https://user@example.com:45581','https://localhost:45581']:
            with self.subTest(origin=origin),self.assertRaises(AppError):load_settings(Path('.'),{'public_origin':origin,'relay_token':RELAY})
        with self.assertRaises(AppError):load_settings(Path('.'),{'public_origin':ORIGIN})
    def test_relay_secret_is_not_in_settings_repr(self):
        settings=load_settings(Path('.'),{'public_origin':ORIGIN,'relay_token':RELAY})
        self.assertNotIn(RELAY,repr(settings))
    def test_private_config_and_key_permissions_are_enforced(self):
        import tempfile,json
        from local_app.config import public_overrides
        from local_app.models import AppError
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config';token=Path(tmp)/'token'
            token.write_text(RELAY);token.chmod(0o600)
            config.write_text(json.dumps({'origin':ORIGIN,'relay_token_file':str(token)}));config.chmod(0o600)
            self.assertEqual(public_overrides(config)['relay_token'],RELAY)
            token.chmod(0o644)
            with self.assertRaises(AppError):public_overrides(config)
