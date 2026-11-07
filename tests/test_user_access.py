"""End-to-end local access boundaries with real MongoDB and fake school authentication."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import time
import unittest
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AL-BackEnd'))
from local_app.models import RunSpec, Submission, ZONE
from support.mongo_fixture import MongoFixture
from support.account_fixture import School

A, B, C = '2310203422', '2310203999', '2310203888'
ADMIN_PASSWORD = 'fixture-root-password'


class AccessSchool(School):
    logins, posts = [], []
    def login(self):
        self.logins.append(self.student_id)
        return super().login()
    def prepare_booking(self):pass
    def submit(self, run, seat):
        self.posts.append((run.account_id, seat))
        return Submission('uncertain', sent=True)


class UserAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()
        cls.root_hash = generate_password_hash(ADMIN_PASSWORD)

    @classmethod
    def tearDownClass(cls):cls.mongo.close()

    def setUp(self):
        from local_app.config import load_settings
        from local_app.runtime import create_runtime
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-access-')
        self.db = self.mongo.database()
        self.db.campus_security.insert_one({'_id': 'root', 'password_hash': self.root_hash})
        for student in (A, B):
            self.db.campus_access.insert_one({'_id': student, 'enabled': True, 'revision': 1, 'note': ''})
        self.runtime = create_runtime(load_settings(ROOT, {'data_dir': Path(self.temp.name),
            'mongo_uri': self.mongo.uri, 'database_name': self.db.name, 'allow_live': True}), client_factory=AccessSchool)
        self.a = self.runtime.accounts.verify_and_save(A, 'pass', 'Alice', None)
        self.b = self.runtime.accounts.verify_and_save(B, 'pass', 'Bob', None)
        self.seat = next(iter(self.runtime.accounts.catalog))
        self.a = self.runtime.accounts.save_plan(self.a.id, {'mode': 'day', 'time_mode': 'full_day',
            'seat_ids': [self.seat], 'day_offset': 1, 'begin': '07:30', 'end': '22:00',
            'trigger': '07:00:00', 'enabled': True}, self.a.revision)
        for a in (self.a, self.b):
            run = RunSpec(a.id, a.revision, '2026-10-21', '09:00', '12:00', (self.seat,), 1)
            self.runtime.store.ensure_run(run)
            self.runtime.store.transition_run(run.key, ('queued',), 'failed', {'message': 'fixture'})
        AccessSchool.logins.clear();AccessSchool.posts.clear()
        self.origin = 'http://127.0.0.1:5003'
        self.client = self.runtime.app.test_client()
        self.csrf = self.client.get('/api/local/bootstrap', base_url=self.origin).json['csrf']

    def tearDown(self):
        self.runtime.close();self.mongo.client.drop_database(self.db.name);self.temp.cleanup()

    def request(self, path, data=None, method='POST', client=None):
        return (client or self.client).open('/api/local'+path, method=method, json=data,
            headers={'Origin': self.origin, 'X-CSRF-Token': self.csrf}, base_url=self.origin)

    def get(self, path):return self.request(path, method='GET')

    def login(self, student=A, password='pass', *, root=False):
        result = self.request('/auth/login', {'kind': 'root' if root else 'student',
            'username': 'root' if root else student, 'password': ADMIN_PASSWORD if root else password})
        self.assertEqual(result.status_code, 200)
        return result

    def test_anonymous_requests_cannot_read_accounts_records_runs_or_settings(self):
        for path in ('/accounts', '/runs', '/status', '/seats', '/policy', f'/accounts/{self.a.id}/records', '/admin/users'):
            with self.subTest(path=path):self.assertEqual(self.get(path).status_code, 401)
        self.assertEqual(self.request(f'/accounts/{self.a.id}/query', {}).status_code, 401)

    def test_school_password_is_checked_even_for_a_saved_account(self):
        result = self.request('/auth/login', {'kind': 'student', 'username': A, 'password': 'wrong'})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(AccessSchool.logins, [A])
        self.assertEqual(self.get('/auth/me').status_code, 401)

    def test_empty_password_cannot_reuse_saved_credentials_to_login(self):
        result = self.request('/auth/login', {'kind': 'student', 'username': A, 'password': ''})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(AccessSchool.logins, [])

    def test_unlisted_student_is_blocked_before_school_authentication(self):
        result = self.request('/auth/login', {'kind': 'student', 'username': C, 'password': 'pass'})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(AccessSchool.logins, [])
        self.assertIsNone(self.runtime.store.find_account_by_student(C))

    def test_student_can_only_list_their_own_account_and_runs(self):
        self.login()
        self.assertEqual([a['id'] for a in self.get('/accounts').json['accounts']], [self.a.id])
        runs = self.get('/runs').json
        self.assertEqual(runs['counts']['total'], 1)
        self.assertEqual([r['account_id'] for r in runs['items']], [self.a.id])
        self.assertEqual(self.get('/runs?account_id='+self.b.id).status_code, 404)

    def test_cross_account_read_write_query_preview_and_reconcile_are_rejected(self):
        self.login();AccessSchool.logins.clear()
        self.assertEqual(self.get(f'/accounts/{self.b.id}/records').status_code, 404)
        for suffix in ('query', 'check', 'today/preview', 'today/submit'):
            self.assertEqual(self.request(f'/accounts/{self.b.id}/{suffix}', {}).status_code, 404)
        self.assertEqual(self.request(f'/accounts/{self.b.id}/plan', {}, method='PUT').status_code, 404)
        self.assertEqual(self.request(f'/runs/{self.b.id}:2026-10-21/reconcile', {}).status_code, 404)
        self.assertEqual(AccessSchool.logins, []);self.assertEqual(AccessSchool.posts, [])

    def test_student_cannot_verify_a_different_account_or_use_admin_routes(self):
        self.login()
        self.assertEqual(self.request('/accounts/verify', {'student_id': B, 'password': 'pass'}).status_code, 403)
        self.assertEqual(self.get('/admin/users').status_code, 403)
        self.assertEqual(self.request('/admin/users/'+C, {'enabled': True, 'expected_revision': None}, method='PUT').status_code, 403)

    def test_root_is_local_auth_and_cookie_has_session_protections(self):
        result = self.login(root=True)
        self.assertEqual(AccessSchool.logins, [])
        self.assertEqual(result.json['user']['role'], 'root')
        self.assertIn('HttpOnly', result.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', result.headers['Set-Cookie'])
        self.assertEqual(self.get('/auth/me').json['user']['role'], 'root')

    def test_root_can_view_all_users_without_password_material(self):
        self.login(root=True)
        self.assertEqual(len(self.get('/accounts').json['accounts']), 2)
        response = self.get('/admin/users')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({u['student_id'] for u in response.json['users']}, {A, B})
        for forbidden in ('password_hash', 'secret', ADMIN_PASSWORD, self.root_hash):
            self.assertNotIn(forbidden, str(response.json))

    def test_root_can_authorize_a_new_student_who_then_authenticates_with_school(self):
        self.login(root=True)
        self.assertEqual(self.request('/admin/users/'+C, {'enabled': True, 'note': 'Fixture C', 'expected_revision': None}, method='PUT').status_code, 200)
        self.request('/auth/logout', {})
        self.login(C)
        self.assertEqual(AccessSchool.logins, [C])
        self.assertEqual(self.get('/auth/me').json['user']['student_id'], C)

    def test_revocation_expires_student_session_and_disables_future_plan(self):
        self.login();student_client = self.client
        self.client = self.runtime.app.test_client();self.login(root=True)
        result = self.request('/admin/users/'+A, {'enabled': False, 'note': 'Stopped', 'expected_revision': 1}, method='PUT')
        self.assertEqual(result.status_code, 200)
        self.assertIn(self.request('/auth/me', method='GET', client=student_client).status_code, (401, 403))
        self.assertFalse(self.runtime.store.get_account(self.a.id)['plan']['enabled'])
        self.assertEqual(AccessSchool.posts, [])
        self.assertEqual(self.runtime.store.runs.count_documents({}), 2)

    def test_disabled_access_blocks_the_execution_backend_even_with_cached_login(self):
        self.runtime.accounts.client(self.a.id)
        self.db.campus_access.update_one({'_id': A}, {'$set': {'enabled': False}})
        self.runtime.store.runs.delete_one({'_id': self.a.id+':2026-10-21'})
        target = (datetime.now(ZONE).date()+timedelta(days=1)).isoformat()
        run = RunSpec(self.a.id, self.a.revision, target, '09:00', '12:00', (self.seat,), time.time()-1)
        result = self.runtime.reservations.execute(run)
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(AccessSchool.posts, [])

    def test_root_can_stop_an_invalid_legacy_plan_without_revalidating_or_rewriting_it(self):
        self.db.campus_accounts.update_one({'_id':self.a.id},{'$set':{
            'plan.time_mode':'custom','plan.begin':'19:00','plan.end':'22:00'}})
        before=self.runtime.store.get_account(self.a.id)
        history=list(self.db.campus_runs.find().sort('_id',1))
        self.login(root=True)
        result=self.request('/admin/users/'+A,{'enabled':False,'note':'Stopped','expected_revision':1},method='PUT')
        self.assertEqual(result.status_code,200)
        after=self.runtime.store.get_account(self.a.id)
        self.assertEqual(after['plan'],before['plan']|{'enabled':False})
        self.assertEqual(after['revision'],before['revision']+1)
        self.assertFalse(self.runtime.auth.is_allowed(A))
        self.assertEqual(list(self.db.campus_runs.find().sort('_id',1)),history)
        self.assertEqual(AccessSchool.posts,[])

    def test_root_revocation_handles_truthy_legacy_enabled_types(self):
        self.login(root=True)
        for value in ('true',1):
            with self.subTest(value=value):
                self.db.campus_access.update_one({'_id':A},{'$set':{'enabled':True}})
                self.db.campus_accounts.update_one({'_id':self.a.id},{'$set':{'plan.enabled':value}})
                access=self.db.campus_access.find_one({'_id':A})
                result=self.request('/admin/users/'+A,{'enabled':False,'expected_revision':access['revision']},method='PUT')
                self.assertEqual(result.status_code,200)
                self.assertIs(self.runtime.store.get_account(self.a.id)['plan']['enabled'],False)
                self.assertFalse(self.runtime.auth.is_allowed(A))

    def test_login_preserves_plan_and_logout_revokes_the_session(self):
        before = self.runtime.store.get_account(self.a.id)['plan']
        self.login()
        self.assertEqual(self.runtime.store.get_account(self.a.id)['plan'], before)
        self.assertEqual(self.request('/auth/logout', {}).status_code, 200)
        self.assertEqual(self.get('/auth/me').status_code, 401)

    def test_expired_or_forged_session_cannot_access_private_data(self):
        self.client.set_cookie('njfu_autolib_session', 'forged-fixture-token', domain='127.0.0.1')
        self.assertEqual(self.get('/accounts').status_code, 401)
        self.login()
        self.runtime.auth.clock = lambda: time.time()+9*3600
        self.assertEqual(self.get('/accounts').status_code, 401)

    def test_login_failures_are_rate_limited(self):
        statuses = [self.request('/auth/login', {'kind': 'root', 'username': 'root', 'password': 'wrong'}).status_code for _ in range(6)]
        self.assertEqual(statuses[:5], [401]*5)
        self.assertEqual(statuses[-1], 429)

    def test_admin_updates_require_matching_revision_and_strict_types(self):
        self.login(root=True)
        self.assertEqual(self.request('/admin/users/'+A, {'enabled': False, 'expected_revision': 0}, method='PUT').status_code, 409)
        self.assertEqual(self.request('/admin/users/'+A, {'enabled': 'true', 'expected_revision': 1}, method='PUT').status_code, 400)
        self.assertEqual(self.request('/admin/users/root', {'enabled': True, 'expected_revision': None}, method='PUT').status_code, 400)
        self.assertTrue(self.db.campus_access.find_one({'_id': A})['enabled'])

    def test_bootstrap_exposes_only_current_session_identity(self):
        public = self.get('/bootstrap').json
        self.assertIsNone(public.get('user'))
        self.assertNotIn('accounts', public)
        self.login()
        bootstrap = self.get('/bootstrap').json
        self.assertEqual(bootstrap['user']['student_id'], A)
        self.assertNotIn('password', str(bootstrap))

    def test_root_initialization_never_overwrites_existing_admin(self):
        from local_app.models import AppError
        with self.assertRaises(AppError):self.runtime.auth.initialize_root('replacement-fixture',['2310203998'])
        self.assertEqual(self.db.campus_security.find_one({'_id':'root'})['password_hash'],self.root_hash)
        self.assertFalse(self.runtime.auth.is_allowed('2310203998'))

    def test_stored_sessions_are_digests_and_logout_does_not_stop_the_plan(self):
        self.login()
        cookie=self.client.get_cookie('njfu_autolib_session',domain='127.0.0.1').value
        row=self.db.campus_sessions.find_one()
        self.assertNotEqual(row['_id'],cookie)
        self.assertNotIn(cookie,str(row))
        self.request('/auth/logout',{})
        self.assertEqual(self.db.campus_sessions.count_documents({}),0)
        self.assertTrue(self.runtime.store.get_account(self.a.id)['plan']['enabled'])

    def test_login_during_prepared_booking_does_not_replace_school_session(self):
        self.runtime.accounts.busy.add(self.a.id)
        before=self.runtime.store.get_account(self.a.id)['credential_revision']
        response=self.request('/auth/login',{'kind':'student','username':A,'password':'pass'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(AccessSchool.logins,[])
        self.assertEqual(self.runtime.store.get_account(self.a.id)['credential_revision'],before)

    def test_reauthorization_does_not_silently_restart_the_plan(self):
        self.login(root=True)
        self.request('/admin/users/'+A,{'enabled':False,'expected_revision':1},method='PUT')
        self.assertEqual(self.request('/admin/users/'+A,{'enabled':True,'expected_revision':2},method='PUT').status_code,200)
        self.assertTrue(self.runtime.auth.is_allowed(A))
        self.assertFalse(self.runtime.store.get_account(self.a.id)['plan']['enabled'])

    def test_root_cannot_enable_a_plan_for_a_disabled_student(self):
        self.login(root=True)
        self.request('/admin/users/'+A,{'enabled':False,'expected_revision':1},method='PUT')
        account=self.runtime.store.get_account(self.a.id)
        response=self.request('/accounts/'+self.a.id+'/plan',{'plan':account['plan']|{'enabled':True},'expected_revision':account['revision']},method='PUT')
        self.assertEqual(response.status_code,403)
        self.runtime.scheduler.refresh()
        self.assertFalse(self.runtime.scheduler.jobs)

    def test_student_status_and_audit_cannot_disclose_other_accounts(self):
        self.login()
        status=self.get('/status').json
        self.assertNotIn('connection',status)
        self.assertNotIn('next_due',status)
        self.assertNotIn('queued',status)
        self.assertEqual(self.get('/admin/audit').status_code,403)
