"""Private Mongo proof adapters; fake sender, no school or SMTP connection."""
import copy
from datetime import datetime
import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock, patch

WORK = Path(__file__).resolve().parents[1]
PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'AL-BackEnd'), str(PROJECT/'tests')]
from support.mongo_fixture import MongoFixture
from local_app.booking_calendar import BookingCalendarService
from local_app.config import load_settings
from local_app.dual_node.contracts import canonical_hash, task_identity
from local_app.dual_node.result_bridge import ResultBridge, run_spec
from local_app.models import Seat, ZONE
from local_app.notifications import NotificationService
from local_app.store import LocalStore
from local_app.vault import CredentialVault


class DualReceiptViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()

    def setUp(self):
        self.db = self.mongo.database(); self.store = LocalStore(self.db)
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-dual-receipt-test-')
        self.now = datetime(2026, 11, 8, 8, tzinfo=ZONE).timestamp()
        self.due = datetime(2026, 11, 8, 7, tzinfo=ZONE).timestamp()
        self.plan = dict(seat_ids=['1','2'], day_offset=1, mode='day', begin='09:00', end='12:00',
                         week_times={}, trigger='07:00:00', enabled=True)
        self.account = dict(_id='fixture-local', student_id='fixture-student', alias='Fixture',
                            revision=2, credential_revision=1, plan=self.plan, date_exceptions={})
        self.store.accounts.insert_one(copy.deepcopy(self.account))
        self.catalog = {str(i): Seat(str(i), 'A00'+str(i), '2F') for i in (1,2)}
        self.accounts = SimpleNamespace(is_allowed=lambda _: True, catalog=self.catalog)
        self.settings = load_settings(PROJECT, {'data_dir': Path(self.temp.name)})
        self.vault = CredentialVault.open(Path(self.temp.name)/'fixture-key', has_encrypted_accounts=False)
        self.messages = []; self.service = self.mail()
        self.service.preferences.insert_one(dict(_id=self.account['_id'], revision=1, verified=True,
            address=self.vault.seal('notification-address:'+self.account['_id'], 'fixture@example.com'),
            binding_id='b'*32, results_enabled=True, reminders_enabled=False,
            results_since=self.now-600, reminder_minutes=30))
        self.task = dict(schema=1, account_ref='acct-'+'a'*32, day='2026-11-09', kind='S', operation='-',
                         revision=2, credential_revision=1, release_at=self.due, start_minute=540,
                         end_minute=720, seat_priorities=['1','2'])
        self.task_id, self.binding = task_identity(self.task)
        self.record = dict(id='actual-fixture-receipt-id', student_id=self.account['student_id'], seat_id='2',
                           begin='2026-11-09 09:00:00', end='2026-11-09 12:00:00', status=1027)
        self.receipt_ref = canonical_hash(dict(task_id=self.task_id, receipt_id=self.record['id'],
            **{key:self.record[key] for key in ('seat_id','begin','end','status')}))
        gate = Mock(); gate.is_managed.return_value = True
        self.bridge = ResultBridge(self.store, {self.account['_id']:dict(account_ref=self.task['account_ref'],
            credential_revision=1)}, gate, clock=lambda:self.now)
        self.save_result()

    def tearDown(self):
        self.service.close(); self.mongo.client.drop_database(self.db.name); self.temp.cleanup()

    def mail(self):
        # Even a configured user SMTP file must never be read by this fixture.
        with patch('local_app.notifications.load_mail_config', return_value=(None,None)):
            return NotificationService(self.settings, self.accounts, self.store, self.vault,
                                       sender=self.messages.append, clock=lambda:self.now)

    def save_result(self, phase='completed', attempts=2):
        result = dict(status=phase, verified=phase=='completed', attempt_count=attempts, result_at=self.due+2)
        if phase=='completed': result.update(receipt_ref=self.receipt_ref, seat_index=1)
        grant = dict(schema=1, domain='njfu-dual-node-production-v1', signature='fixture-not-used-for-authorization',
            payload=dict(authority_id='b'*32, grant_id='c'*32, task_id=self.task_id,
                binding_sha256=self.binding, node_id='ubuntu', release_at=self.due, credential_revision=1))
        response = dict(mode='production', authority_id='b'*32, task_id=self.task_id,
            binding_sha256=self.binding, phase=phase, owner='ubuntu', grant=grant, result=result)
        self.bridge.apply(self.task, response)
        self.row = self.store.get_run(run_spec(self.task, self.account['_id']).key)
        return self.row

    def entries(self):
        result = BookingCalendarService(self.store,self.catalog,wall_clock=lambda:self.now).get(
            self.account['_id'], '2026-11-09','2026-11-09')
        return result['days'][0]['entries']

    def test_completed_email_uses_actual_seat_owner_and_generic_link_without_faking_record(self):
        before = list(self.store.runs.find())
        self.assertEqual(self.service.run_once(),1)
        message, = self.messages
        self.assertEqual(message['subject'],'预约成功')
        for text in ('A002','Ubuntu','执行节点已查询核验有效回执',self.receipt_ref):
            self.assertIn(text,message['body'])
        self.assertNotIn('record_id=',message['body'])
        self.assertNotIn(self.record['id'],message['body'])
        self.assertEqual(list(self.store.runs.find()),before)
        another = self.mail()
        try: self.assertEqual(another.run_once(),0)
        finally: another.close()

    def test_completed_calendar_is_confirmed_without_uuid_and_deduplicates_ordinary_plan(self):
        entry, = self.entries()
        self.assertEqual((entry['state'],entry['source'],entry['seat_ids']),('confirmed','dual_node_verified',['2']))
        self.assertEqual(entry['receipt_ref'],self.receipt_ref)
        self.assertIn('执行节点已查询核验有效回执',entry['message'])
        self.assertIn('Ubuntu',entry['message'])
        self.assertNotIn('receipt_id',entry)
        self.assertNotIn('cancellation_id',entry)

    def test_invalid_binding_domain_types_or_proof_never_notify_or_confirm(self):
        changes = [lambda r:r['dual_binding'].update(mode='fixture'),
            lambda r:r['dual_binding'].update(authority_id='invalid'),
            lambda r:r['dual_result'].update(verified=False),
            lambda r:r['dual_result'].update(seat_index=True),
            lambda r:r['dual_result'].update(attempt_count=True),
            lambda r:r['dual_progress'].update(owner='other'),
            lambda r:r['dual_progress'].update(grant_id='d'*32),
            lambda r:r['dual_mirror'].update(result_sha256='e'*64),
            lambda r:r['spec'].update(end='13:00'),
            lambda r:r.update(native_binding={}), lambda r:r.pop('dual_mirror'),
            lambda r:r['dual_task'].update(revision=3)]
        prefs = self.service._preference(self.account['_id'])
        original = copy.deepcopy(self.row)
        for index,change in enumerate(changes):
            with self.subTest(index=index):
                row = copy.deepcopy(original); change(row)
                self.store.runs.replace_one({'_id':row['_id']},row)
                self.assertFalse(self.service._result_eligible(self.account,prefs,row))
                dual_entries = [e for e in self.entries() if e.get('run_id')==row['_id']]
                self.assertEqual([e['state'] for e in dual_entries],['uncertain'])
        self.assertEqual(self.messages,[])

    def test_failed_and_uncertain_need_bound_positive_attempts_and_use_node_reported_language(self):
        # Each phase uses an independent immutable mirror, as production does.
        for phase in ('failed','uncertain'):
            with self.subTest(phase=phase):
                self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
                self.save_result(phase)
                self.assertEqual(self.service.run_once(),1)
                body = self.messages[-1]['body']
                self.assertIn('Ubuntu',body)
                self.assertIn('执行节点报告',body)
                self.assertNotIn('学校明确拒绝',body)
                self.assertNotIn('record_id=',body)
                row = self.store.get_run(self.row['_id']); row['dual_result']['attempt_count']=0
                row['dual_mirror']['result_sha256']=canonical_hash(row['dual_result'])
                self.assertFalse(self.service._result_eligible(self.account,
                    self.service._preference(self.account['_id']),row))

    def test_matching_actual_cancellation_suppresses_success_and_marks_calendar_cancelled(self):
        self.db.campus_cancellations.insert_one(dict(_id='fixture-cancel',account_id=self.account['_id'],
            record=self.record,state='success',ack=True,requested_at=self.now-10,completed_at=self.now-5))
        self.assertEqual(self.service.run_once(),1)  # Existing cancellation mail only.
        self.assertEqual([m['subject'] for m in self.messages],['预约已取消'])
        entries = self.entries()
        dual = [entry for entry in entries if entry.get('run_id')==self.row['_id']]
        self.assertEqual([entry['state'] for entry in dual],['cancelled'])
        self.assertNotIn('receipt_id',dual[0])

    def test_other_receipt_cancellation_does_not_suppress_valid_dual_result(self):
        self.db.campus_cancellations.insert_one(dict(_id='other-cancel',account_id=self.account['_id'],
            record=self.record|{'id':'different-actual-receipt'},state='success',ack=True,
            requested_at=self.now-10,completed_at=self.now-5))
        self.service.run_once()
        self.assertIn('预约成功',[message['subject'] for message in self.messages])
        dual = [entry for entry in self.entries() if entry.get('run_id')==self.row['_id']]
        self.assertEqual([entry['state'] for entry in dual],['confirmed'])

    def test_real_school_snapshot_receipt_keeps_existing_uuid_and_merges_dual_run(self):
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat())})
        entry, = self.entries()
        self.assertEqual((entry['state'],entry['source'],entry['receipt_id']),
                         ('confirmed','school_snapshot',self.record['id']))
        self.assertIn(self.row['_id'],entry['run_ids'])

    def test_receipt_view_is_typed_and_never_exposes_a_school_record_id(self):
        module = importlib.import_module('local_app.dual_node.receipt_view')
        view = module.receipt_view(self.account,self.row)
        self.assertIsInstance(view,module.DualReceiptView)
        self.assertEqual((view.seat_id,view.owner,view.receipt_ref),('2','ubuntu',self.receipt_ref))
        self.assertFalse(hasattr(view,'id'))
        self.assertFalse(hasattr(view,'receipt_id'))

    def test_new_actual_snapshot_conflict_never_gets_repromoted_by_older_dual_proof(self):
        for changes in ({'status':0}, {'status':'unknown'}, {'seat_id':'1'}, {'begin':'2026-11-09 10:00:00'}):
            with self.subTest(changes=changes):
                self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(
                    snapshot=[self.record|changes], snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat())})
                self.assertFalse(any(entry['state']=='confirmed' for entry in self.entries()))
                self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.messages,[])

    def test_malformed_cancellation_does_not_suppress_a_valid_success(self):
        self.db.campus_cancellations.insert_one(dict(_id='incomplete-cancel',account_id=self.account['_id'],
            record=self.record,state='success',ack=True,requested_at=self.now-10))
        self.assertEqual(self.service.run_once(),1)
        self.assertEqual([m['subject'] for m in self.messages],['预约成功'])

    def reminder_time(self):
        self.now = datetime(2026,11,9,8,30,tzinfo=ZONE).timestamp()
        self.service.preferences.update_one({'_id':self.account['_id']},{'$set':dict(
            results_enabled=False, reminders_enabled=True, reminders_since=self.now-600)})

    def test_dual_reminder_without_snapshot_uses_proof_and_later_snapshot_never_duplicates(self):
        self.reminder_time()
        self.assertEqual(self.service.run_once(),1)
        message, = self.messages
        for text in ('使用提醒','A002','Ubuntu','执行节点已查询核验有效回执',self.receipt_ref):
            self.assertIn(text,message['subject']+message['body'])
        self.assertNotIn('record_id=',message['body'])
        self.assertNotIn('action', message)
        self.assertNotIn('#ticket=', message['body'])
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
            identity={'student_id':self.account['student_id']})})
        another = self.mail()
        try: self.assertEqual(another.run_once(),0)
        finally: another.close()
        self.assertEqual(len(self.messages),1)

    def test_dual_reminder_freshness_preferences_grace_and_unverified_proof_fail_closed(self):
        self.reminder_time()
        prefs = self.service._preference(self.account['_id'])
        for values in ({'verified':False},{'reminders_enabled':False},{'reminders_since':self.now+1}):
            with self.subTest(values=values):
                self.service.preferences.replace_one({'_id':prefs['_id']},prefs|values)
                self.assertEqual(self.service.run_once(),0)
        self.service.preferences.replace_one({'_id':prefs['_id']},prefs)
        self.now += 301
        self.assertEqual(self.service.run_once(),0)
        self.now -= 301
        row=copy.deepcopy(self.row); row['dual_result']['result_at']=self.now-36*3600-1
        row['dual_mirror']['result_sha256']=canonical_hash(row['dual_result'])
        row['result']['verified_at']=datetime.fromtimestamp(row['dual_result']['result_at'],ZONE).isoformat(timespec='seconds')
        self.store.runs.replace_one({'_id':row['_id']},row)
        self.assertEqual(self.service.run_once(),0)
        row=copy.deepcopy(self.row);row['dual_binding']['mode']='fixture'
        self.store.runs.replace_one({'_id':row['_id']},row)
        self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.messages,[])

    def test_queued_dual_reminder_revalidates_cancellation_and_snapshot_conflict_at_delivery(self):
        self.reminder_time()
        self.service._discover()
        self.assertEqual(self.service.outbox.count_documents({'status':'pending'}),1)
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record|{'status':0}])})
        self.assertEqual(self.service.run_once(),0)
        self.service.outbox.delete_many({})
        self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
        self.service._discover()
        self.assertEqual(self.service.outbox.count_documents({'status':'pending'}),1)
        self.db.campus_cancellations.insert_one(dict(_id='reminder-cancel',account_id=self.account['_id'],
            record=self.record,state='success',ack=True,requested_at=self.now-1,completed_at=self.now))
        self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.messages,[])

    def test_existing_snapshot_reminder_is_not_repeated_when_dual_mirror_arrives_later(self):
        self.reminder_time()
        self.store.runs.delete_many({})
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
            identity={'student_id':self.account['student_id']})})
        self.assertEqual(self.service.run_once(),1)
        self.save_result()
        self.assertEqual(self.service.run_once(),0)
        self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
        self.assertEqual(self.service.run_once(),0)
        self.assertEqual(len(self.messages),1)

    def test_queued_dual_reminder_rechecks_proof_freshness_and_preferences(self):
        self.reminder_time()
        prefs = self.service._preference(self.account['_id'])
        original = copy.deepcopy(self.row)
        for field,value in (('verified',False),('reminders_enabled',False),
                            ('reminders_since',self.now+1),('reminder_minutes',20),
                            ('fixture_domain',None),('invalid_digest',None),('expired_proof',None)):
            with self.subTest(field=field):
                self.service.outbox.delete_many({})
                self.service.preferences.replace_one({'_id':prefs['_id']},prefs)
                self.store.runs.replace_one({'_id':original['_id']},copy.deepcopy(original))
                self.service._discover()
                self.assertEqual(self.service.outbox.count_documents({'status':'pending'}),1)
                row = copy.deepcopy(original)
                if field=='fixture_domain': row['dual_binding']['mode']='fixture'
                elif field=='invalid_digest': row['dual_mirror']['result_sha256']='f'*64
                elif field=='expired_proof':
                    row['dual_result']['result_at']=self.now-36*3600-1
                    row['dual_mirror']['result_sha256']=canonical_hash(row['dual_result'])
                    row['result']['verified_at']=datetime.fromtimestamp(row['dual_result']['result_at'],ZONE).isoformat(timespec='seconds')
                else: self.service.preferences.update_one({'_id':prefs['_id']},{'$set':{field:value}})
                self.store.runs.replace_one({'_id':row['_id']},row)
                self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.messages,[])

    def test_snapshot_reminder_dedup_survives_snapshot_removal_before_mirror_arrives(self):
        self.reminder_time(); self.store.runs.delete_many({})
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
            identity={'student_id':self.account['student_id']})})
        self.assertEqual(self.service.run_once(),1)
        self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
        self.save_result()
        self.assertEqual(self.service.run_once(),0)
        self.assertEqual(len(self.messages),1)

    def late_reminder(self):
        self.now=self.due+36*3600
        self.task.update(start_minute=1170,end_minute=1290)
        self.task_id,self.binding=task_identity(self.task)
        self.record.update(begin='2026-11-09 19:30:00',end='2026-11-09 21:30:00')
        self.receipt_ref=canonical_hash(dict(task_id=self.task_id,receipt_id=self.record['id'],
            **{key:self.record[key] for key in ('seat_id','begin','end','status')}))
        self.store.runs.delete_many({}); self.db.campus_dual_task_scope.delete_many({})
        self.save_result()
        self.service.preferences.update_one({'_id':self.account['_id']},{'$set':dict(
            results_enabled=False,reminders_enabled=True,reminders_since=self.now-600)})

    def test_dual_reminder_dedup_survives_36h_expiry_before_later_snapshot(self):
        self.late_reminder()
        self.assertEqual(self.service.run_once(),1)
        self.now+=3
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
            identity={'student_id':self.account['student_id']})})
        self.assertEqual(self.service.run_once(),0)
        self.assertEqual(len(self.messages),1)

    def assert_snapshot_ticket(self, message):
        from local_app.mail_cancellation import read_ticket
        self.assertEqual(message['action']['label'], '取消预约')
        url = urlsplit(message['action']['url'])
        self.assertEqual((url.path, url.query), ('/static/mail-cancel.html', ''))
        claims = read_ticket(self.vault.key, parse_qs(url.fragment)['ticket'][0], self.now)
        self.assertEqual(claims['account_id'], self.account['_id'])
        self.assertEqual(claims['record'], self.record)
        self.assertIn(message['action']['url'], message['body'])

    def save_reminder_snapshot(self):
        self.store.accounts.update_one({'_id': self.account['_id']}, {'$set': dict(
            snapshot=[self.record], snapshot_at=datetime.fromtimestamp(self.now, ZONE).isoformat(),
            identity={'student_id': self.account['student_id']})})

    def test_fresh_dual_proof_and_actual_snapshot_use_one_cancellable_reminder(self):
        self.reminder_time(); self.save_reminder_snapshot()
        before = copy.deepcopy(self.store.get_account(self.account['_id']))
        self.assertEqual(self.service.run_once(), 1)
        self.assert_snapshot_ticket(self.messages[0])
        self.assertEqual(self.service.outbox.count_documents({'kind': 'dual_reminder'}), 0)
        self.assertEqual(self.service.run_once(), 0)
        self.assertEqual(self.store.get_account(self.account['_id']), before)

    def test_fresh_unattempted_dual_reminder_hands_off_to_actual_snapshot(self):
        self.reminder_time(); self.service._discover()
        donor = self.service.outbox.find_one({'kind': 'dual_reminder', 'status': 'pending'})
        self.save_reminder_snapshot()
        self.assertEqual(self.service.run_once(), 1)
        self.assert_snapshot_ticket(self.messages[0])
        retired = self.service.outbox.find_one({'_id': donor['_id']})
        self.assertEqual(retired['status'], 'cancelled')
        self.assertEqual(retired['reminder_alias_released_to'],
                         self.service._reminder_key(self.account['_id'], self.record))
        self.assertNotIn('attempted_at', retired)
        self.assertEqual(self.service.run_once(), 0)

    def test_snapshot_appearing_after_discovery_prevents_uncancellable_delivery(self):
        self.reminder_time(); self.service._discover()
        donor = self.service.outbox.find_one({'kind': 'dual_reminder', 'status': 'pending'})
        self.save_reminder_snapshot()
        self.assertIsNone(self.service._payload(donor))
        self.assertEqual(self.service.run_once(), 1)
        self.assert_snapshot_ticket(self.messages[0])

    def test_fresh_attempted_dual_reminder_blocks_duplicate_snapshot_capability(self):
        self.reminder_time()
        for status in ('dispatching', 'unknown', 'sent'):
            with self.subTest(status=status):
                self.service.outbox.delete_many({})
                self.store.accounts.update_one({'_id': self.account['_id']}, {'$unset': {'snapshot': ''}})
                self.service._discover()
                donor = self.service.outbox.find_one({'kind': 'dual_reminder'})
                self.service.outbox.update_one({'_id': donor['_id']},
                    {'$set': {'status': status, 'attempted_at': self.now}})
                self.save_reminder_snapshot()
                self.assertEqual(self.service.run_once(), 0)
                self.assertEqual(self.service.outbox.count_documents({'kind': 'reminder'}), 0)
        self.assertEqual(self.messages, [])

    def test_expired_dual_proof_without_previous_event_keeps_fresh_snapshot_reminder(self):
        self.late_reminder(); self.now+=3
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
            identity={'student_id':self.account['student_id']})})
        self.assertEqual(self.service.run_once(),1)
        self.assert_snapshot_ticket(self.messages[0])

    def test_expired_unattempted_dual_reminder_releases_alias_for_fresh_snapshot(self):
        self.late_reminder(); self.service._discover()
        pending=self.service.outbox.find_one({'kind':'dual_reminder','status':'pending'})
        self.assertIsNotNone(pending)
        self.now+=3
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
            identity={'student_id':self.account['student_id']})})
        self.assertEqual(self.service.run_once(),1)
        event=self.service.outbox.find_one({'_id':pending['_id']})
        self.assertEqual(event['status'],'cancelled')
        self.assertEqual(event.get('reminder_alias_released_to'),self.service._reminder_key(self.account['_id'],self.record))
        self.assertNotIn('attempted_at',event)
        self.assert_snapshot_ticket(self.messages[0])

    def test_expired_attempted_or_unknown_dual_event_never_falls_back_to_snapshot(self):
        for status in ('dispatching','unknown','sent'):
            with self.subTest(status=status):
                self.service.outbox.delete_many({}); self.late_reminder()
                self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
                self.service._discover()
                event=self.service.outbox.find_one({'kind':'dual_reminder'})
                self.service.outbox.update_one({'_id':event['_id']},{'$set':dict(status=status,attempted_at=self.now)})
                self.now+=3
                self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
                    snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),
                    identity={'student_id':self.account['student_id']})})
                self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.messages,[])

    def test_unattempted_snapshot_reminder_hands_off_when_snapshot_is_missing(self):
        self.reminder_time(); self.store.runs.delete_many({})
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),identity={'student_id':self.account['student_id']})})
        self.service._discover()
        donor=self.service.outbox.find_one({'kind':'reminder','status':'pending'})
        self.assertIsNotNone(donor)
        self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
        self.save_result()
        self.assertEqual(self.service.run_once(),1)
        self.assertNotIn('record_id=',self.messages[0]['body'])
        retired=self.service.outbox.find_one({'_id':donor['_id']})
        self.assertEqual(retired['status'],'cancelled'); self.assertNotIn('attempted_at',retired)
        self.assertTrue(retired['reminder_alias_released_to'].startswith('reminder:dual:'))
        self.assertEqual(self.service.run_once(),0)

    def test_unattempted_snapshot_to_dual_to_snapshot_cycle_recovers_after_expiry(self):
        self.late_reminder(); self.store.runs.delete_many({})
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),identity={'student_id':self.account['student_id']})})
        self.service._discover()
        self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
        self.save_result(); self.service._discover()
        self.assertEqual(self.service.outbox.count_documents({'status':'pending'}),1)
        self.now+=3
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),identity={'student_id':self.account['student_id']})})
        self.assertEqual(self.service.run_once(),1)
        self.assert_snapshot_ticket(self.messages[0])
        self.assertEqual(self.service.run_once(),0)

    def test_lost_donor_cas_never_falls_back_in_that_round(self):
        self.late_reminder(); self.service._discover(); self.now+=3
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),identity={'student_id':self.account['student_id']})})
        original=self.service.outbox.update_one
        def lose(query,update,*args,**kwargs):
            if 'reminder_alias_released_to' in update.get('$set',{}):
                return SimpleNamespace(acknowledged=True,modified_count=0)
            return original(query,update,*args,**kwargs)
        with patch.object(self.service.outbox,'update_one',side_effect=lose):
            self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.service.outbox.count_documents({'kind':'reminder'}),0)

    def test_restart_after_donor_release_finishes_same_event_once(self):
        self.late_reminder(); self.service._discover(); self.now+=3
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),identity={'student_id':self.account['student_id']})})
        donor=self.service.outbox.find_one({'kind':'dual_reminder'})
        destination=self.service._reminder_key(self.account['_id'],self.record)
        self.assertFalse(self.service._reminder_donor_blocks(donor,destination))
        restarted=self.mail()
        try:
            self.assertEqual(restarted.run_once(),1)
            self.assertEqual(restarted.run_once(),0)
        finally: restarted.close()

    def test_snapshot_donor_cas_loss_holds_then_recovers_without_cancelled_target(self):
        self.reminder_time(); self.store.runs.delete_many({})
        self.store.accounts.update_one({'_id':self.account['_id']},{'$set':dict(snapshot=[self.record],
            snapshot_at=datetime.fromtimestamp(self.now,ZONE).isoformat(),identity={'student_id':self.account['student_id']})})
        self.service._discover()
        self.store.accounts.update_one({'_id':self.account['_id']},{'$unset':{'snapshot':''}})
        self.save_result()
        original=self.service.outbox.update_one
        def lose(query,update,*args,**kwargs):
            if 'reminder_alias_released_to' in update.get('$set',{}):
                return SimpleNamespace(acknowledged=True,modified_count=0)
            return original(query,update,*args,**kwargs)
        with patch.object(self.service.outbox,'update_one',side_effect=lose):
            self.assertEqual(self.service.run_once(),0)
        self.assertEqual(self.service.outbox.count_documents({'kind':'dual_reminder'}),0)
        self.assertEqual(self.service.run_once(),1)
        self.assertEqual(self.service.run_once(),0)


if __name__=='__main__': unittest.main()
