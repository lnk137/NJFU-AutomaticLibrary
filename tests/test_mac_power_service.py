"""Power observability uses isolated storage and cannot perform a school request."""
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.auth import AuthService
from local_app.config import load_settings
from local_app.models import ZONE, resolve_run, validate_plan
from local_app.power import PowerService, decide
from local_app.web import create_app
from support.account_fixture import services
from support.auth_fixture import root_session, grant
from support.mongo_fixture import MongoFixture

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 25, 7, 6, tzinfo=ZONE).timestamp()


class PowerServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mongo = MongoFixture()

    @classmethod
    def tearDownClass(cls):
        cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database()
        self.temp, self.store, self.accounts, self.catalog = services(self.db)
        self.account = self.accounts.verify_and_save('fixture01', 'pass', 'Fixture', None)
        self.account = self.accounts.save_plan(self.account.id, {
            'seat_ids': ['1'], 'mode': 'day', 'time_mode': 'full_day',
            'trigger': '07:00:00', 'enabled': True}, self.account.revision)
        self.auth = AuthService(self.store, self.accounts)
        grant(self.auth, ['fixture01'])
        self.settings = load_settings(ROOT, {'data_dir': Path(self.temp.name).resolve() / 'data'})
        self.scheduler = Mock()
        self.scheduler.status.return_value = {
            'scheduler_running': True, 'clock_ok': True, 'allow_live': True,
            'last_error': None, 'scheduler_warning': None, 'capacity': 50}
        self.power = PowerService(self.settings, self.accounts, self.store, self.scheduler, self.catalog)
        self.app = create_app(self.settings, self.accounts, self.store, self.catalog,
                              self.scheduler.status, auth=self.auth, power=self.power)
        self.client = self.app.test_client()
        root_session(self.auth, self.client)

    def tearDown(self):
        self.power.close()
        self.accounts.close()
        self.temp.cleanup()
        self.mongo.client.drop_database(self.db.name)

    def protected(self):
        return {name: json.dumps(list(self.db[name].find().sort('_id', 1)), sort_keys=True, default=str)
                for name in ('campus_accounts', 'campus_runs', 'campus_access', 'campus_audit')}

    def test_power_snapshot_uses_projected_execution_status(self):
        self.power.status_provider = lambda: {'ready_to_submit': False,
            'execution_scope': 'dual_node', 'dual_node_status': {'state': 'probing'}}
        before = self.protected()
        evidence = self.power.snapshot(NOW, 100.)
        self.assertFalse(evidence['ready_to_submit'])
        self.assertEqual(evidence['execution_scope'], 'dual_node')
        self.assertEqual(evidence['dual_node_status']['state'], 'probing')
        self.assertEqual(before, self.protected())

    def test_status_projection_failure_cannot_leave_ready_heartbeat(self):
        def unavailable(): raise RuntimeError('private diagnostic sentinel')
        self.power.status_provider = unavailable
        evidence = self.power.snapshot(NOW, 100.)
        self.assertFalse(evidence['ready_to_submit'])
        self.assertTrue(evidence['error'])
        self.assertNotIn('private diagnostic sentinel', str(evidence))
        self.assertTrue(decide(NOW, 100., {'enabled': True}, evidence).hold)

    def seed(self, state):
        run = resolve_run(self.account.id, self.account.revision,
                          validate_plan(self.account.plan, self.catalog), datetime.fromtimestamp(NOW, ZONE).date())
        self.store.ensure_run(run)
        self.store.runs.update_one({'_id': run.key}, {'$set': {'status': state}})
        return run

    def test_missing_expected_receipt_is_not_success_and_read_does_not_mutate(self):
        before = self.protected()
        with patch.object(self.accounts, 'client', side_effect=AssertionError('No school request')):
            evidence = self.power.snapshot(NOW, 100.)
        self.assertEqual(evidence['expected'], ['missing'])
        self.assertTrue(decide(NOW, 100., {'enabled': True}, evidence).hold)
        self.assertEqual(self.protected(), before)
        self.assertNotIn('secret', json.dumps(evidence))

    def test_skipped_date_does_not_invent_a_missing_booking(self):
        self.store.accounts.update_one({'_id':self.account.id},{'$set':{
            'date_exceptions':{'2026-10-26':{'action':'skip'}}}})
        before=self.protected();evidence=self.power.snapshot(NOW,100.)
        self.assertEqual(evidence['expected'],[])
        self.assertFalse(decide(NOW,100.,{'enabled':True},evidence).hold)
        self.assertEqual(self.protected(),before)

    def test_added_single_day_is_expected_even_when_regular_weekday_is_off(self):
        self.account=self.accounts.save_plan(self.account.id,{
            'seat_ids':['1'],'mode':'week','time_mode':'custom','begin':'09:00','end':'12:00',
            'week_times':{'2':['09:00','12:00']},'trigger':'07:00:00','enabled':True},self.account.revision)
        self.store.accounts.update_one({'_id':self.account.id},{'$set':{
            'date_exceptions':{'2026-10-26':{'action':'override','begin':'14:00','end':'16:00','seat_ids':['2']}}}})
        before=self.protected();evidence=self.power.snapshot(NOW,100.)
        self.assertEqual(evidence['expected'],['missing'])
        self.assertTrue(decide(NOW,100.,{'enabled':True},evidence).hold)
        self.assertEqual(self.protected(),before)

    def test_matching_success_releases_without_school_or_plan_writes(self):
        self.seed('success')
        before = self.protected()
        evidence = self.power.snapshot(NOW, 100.)
        self.assertFalse(decide(NOW, 100., {'enabled': True}, evidence).hold)
        self.assertEqual(evidence['capacity'], 50)
        self.assertEqual(self.protected(), before)

    def test_old_revision_cannot_prove_current_plan_completed(self):
        run = self.seed('success')
        self.store.runs.update_one({'_id': run.key}, {'$set': {'spec.revision': 0}})
        self.assertEqual(self.power.snapshot(NOW, 100.)['expected'], ['missing'])

    def test_failed_or_uncertain_outcomes_and_busy_account_keep_protection(self):
        for state in ('failed', 'uncertain', 'queued', 'preparing', 'submitting', 'skipped'):
            with self.subTest(state=state):
                self.seed(state)
                self.assertTrue(decide(NOW, 100., {'enabled': True}, self.power.snapshot(NOW, 100.)).hold)

    def test_database_error_is_explicit_and_cannot_change_scheduler_state(self):
        before = self.protected()
        with patch.object(self.store.runs, 'find', side_effect=RuntimeError('fixture')):
            evidence = self.power.snapshot(NOW, 100.)
        self.assertTrue(evidence['error'])
        self.assertTrue(decide(NOW, 100., {'enabled': True}, evidence).hold)
        self.assertEqual(self.protected(), before)
        self.scheduler.stop.assert_not_called()

    def test_evidence_started_before_quiet_does_not_write_after_boundary(self):
        self.power.files.update({'enabled':True},0)
        before=datetime(2026,10,25,6,59,49,tzinfo=ZONE).timestamp()
        after=datetime(2026,10,25,7,0,0,tzinfo=ZONE).timestamp()
        with (patch.object(self.power,'snapshot',return_value={}),
              patch.object(self.power.files,'write') as writer,
              patch('local_app.power.time.time',return_value=after)):
            self.power.sample(before)
        writer.assert_not_called()

    def test_default_status_does_not_install_or_create_private_state(self):
        result = self.client.get('/api/local/admin/power', base_url='http://127.0.0.1:5003')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['config']['enabled'])
        self.assertFalse(self.power.files.base.exists())

    def test_student_and_public_root_cannot_read_or_control_power(self):
        self.client.set_cookie('njfu_autolib_session', self.auth._issue('student',
            student_id='fixture01', account_id=self.account.id), domain='127.0.0.1')
        self.assertEqual(self.client.get('/api/local/admin/power', base_url='http://127.0.0.1:5003').status_code, 403)
        origin, relay = 'https://library-fixture.example:45581', 'f' * 64
        app = create_app(load_settings(ROOT, {'public_origin': origin, 'relay_token': relay}),
            self.accounts, self.store, self.catalog, self.scheduler.status, auth=self.auth, power=self.power)
        client = app.test_client()
        client.set_cookie('njfu_autolib_session', self.auth._issue('root'), domain='library-fixture.example')
        self.assertEqual(client.get('/api/local/admin/power', base_url=origin, headers={
            'X-NJFU-Relay-Token': relay, 'X-NJFU-Relay-HTTPS': '1', 'X-NJFU-Client-IP': '198.51.100.2'}).status_code, 403)

    def test_config_change_requires_csrf_and_fixed_data_only(self):
        base = 'http://127.0.0.1:5003'
        self.assertEqual(self.client.put('/api/local/admin/power', base_url=base, json={}).status_code, 403)
        csrf = self.client.get('/api/local/bootstrap', base_url=base).json['csrf']
        with patch('local_app.power.native_installation', return_value=True):
            response = self.client.put('/api/local/admin/power', base_url=base,
                headers={'Origin': base, 'X-CSRF-Token': csrf}, json={'enabled': True, 'expected_revision': 0, 'command': 'sleepnow'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.power.files.base.exists())


if __name__ == '__main__':
    unittest.main()
