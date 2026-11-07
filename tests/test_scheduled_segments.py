"""Scheduled multi-window contracts and durable fake-school execution; no school network."""
import copy
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.models import AppError, Plan, RunSpec, SchoolRecord, Seat, Submission, ZONE, resolve_run, resolve_runs, validate_plan
from local_app.recovery_policy import clear_busy_failure
from local_app.school import CampusClient
from support.account_fixture import School, services
from support.mongo_fixture import MongoFixture

CATALOG = {str(i): Seat(str(i), 'A'+str(i), '2F') for i in range(1, 7)}
RELEASE = date(2026, 10, 24)
DUE = datetime(2026, 10, 24, 7, tzinfo=ZONE).timestamp()


def raw_plan(**fields):
    return dict(seat_ids=['1'], day_offset=1, mode='day', begin='09:00', end='12:00',
        week_times={}, trigger='07:00:00', enabled=True) | fields


def segments():
    return [dict(id='morning', begin='09:00', end='12:00', seat_ids=['1', '2']),
            dict(id='afternoon', begin='12:00', end='15:00', seat_ids=['2', '1']),
            dict(id='evening', begin='16:00', end='19:00', seat_ids=['1'])]


class ScheduledSegmentContractTests(unittest.TestCase):
    def test_scalar_shape_keys_and_resolution_remain_exact(self):
        plan = validate_plan(raw_plan(), CATALOG)
        self.assertIs(type(plan), Plan)
        self.assertNotIn('segments', asdict(plan))
        runs = resolve_runs('a', 3, plan, RELEASE)
        self.assertEqual(runs, (resolve_run('a', 3, plan, RELEASE),))
        self.assertEqual(runs[0].key, 'a:2026-10-25')
        self.assertEqual(set(asdict(runs[0])), {'account_id','revision','target_date','begin','end','seat_ids','due_at','kind','operation_id','expires_at'})

    def test_chronological_adjacent_independent_seats_and_stable_keys(self):
        plan = validate_plan(raw_plan(segments=list(reversed(segments()))), CATALOG)
        runs = resolve_runs('a', 3, plan, RELEASE)
        self.assertEqual([r.begin for r in runs], ['09:00', '12:00', '16:00'])
        self.assertEqual([r.seat_ids for r in runs], [('1','2'), ('2','1'), ('1',)])
        self.assertEqual(len({r.key for r in runs}), 3)
        self.assertTrue(all(r.kind == 'scheduled' and ':segment:' in r.key for r in runs))
        self.assertEqual(resolve_runs('a', 3, validate_plan(asdict(plan), CATALOG), RELEASE), runs)
        self.assertEqual(resolve_run('a', 3, plan, RELEASE), runs[0])

    def test_missing_ids_normalize_stably_and_seat_edits_keep_identity(self):
        value = segments()[0]; del value['id']
        first = validate_plan(raw_plan(segments=[value]), CATALOG)
        value['seat_ids'] = ['2']
        second = validate_plan(raw_plan(segments=[value]), CATALOG)
        self.assertEqual(first.segments[0]['id'], second.segments[0]['id'])

    def test_invalid_segment_shape_overlap_duplicate_ids_and_friday_fail_closed(self):
        invalid = [[], segments()*3, [None], [segments()[0] | {'seat_ids': []}],
            [segments()[0], segments()[1] | {'begin':'11:00'}],
            [segments()[0], segments()[1] | {'id':'morning'}],
            [segments()[0] | {'begin':'19:00','end':'22:00'}],
            [segments()[0] | {'id':'override-pretend'}]]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(AppError):
                validate_plan(raw_plan(segments=values), CATALOG)

    def test_weekly_days_actual_dates_skip_and_override_replace_all(self):
        plan = validate_plan(raw_plan(mode='week', week_segments={'1':segments(), '5':[segments()[0]]}), CATALOG)
        self.assertEqual(resolve_runs('a',1,plan,RELEASE), ())
        runs = resolve_runs('a',1,plan,date(2026,10,25))
        self.assertEqual(len(runs),3)
        self.assertEqual({r.target_date for r in runs},{'2026-10-26'})
        self.assertEqual(resolve_runs('a',1,plan,date(2026,10,25),{'2026-10-26':{'action':'skip'}}),())
        override={'2026-10-25':dict(action='override',begin='14:00',end='17:00',seat_ids=['2'])}
        run, = resolve_runs('a',1,plan,RELEASE,override)
        self.assertEqual((run.begin,run.end,run.seat_ids,run.operation_id),('14:00','17:00',('2',),'override-2026-10-25'))

    def test_seven_intervals_supported_on_selected_non_friday_weekday(self):
        values=[dict(id='s'+str(i),begin=f'{7+i*2:02d}:30',end=f'{9+i*2:02d}:30',seat_ids=['1']) for i in range(7)]
        plan=validate_plan(raw_plan(mode='week',week_segments={'1':values}),CATALOG)
        self.assertEqual(len(resolve_runs('a',1,plan,date(2026,10,25))),7)

    def test_conflicts_include_later_segment(self):
        from local_app.seat_conflicts import detect_seat_conflicts
        def account(key, plan):
            return dict(account_id=key,revision=1,student_id=key,name=key,plan=plan,date_exceptions={})
        first = validate_plan(raw_plan(segments=segments()),CATALOG)
        second = validate_plan(raw_plan(begin='13:00',end='16:00',seat_ids=['2']),CATALOG)
        result = detect_seat_conflicts([account('a',first),account('b',second)],CATALOG,datetime(2026,10,24,6,tzinfo=ZONE))
        self.assertTrue(any(r['begin']=='13:00' and r['end']=='15:00' and r['kind']=='preferred' for r in result['items']))


class ScheduledSegmentFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.mongo = MongoFixture()
    @classmethod
    def tearDownClass(cls): cls.mongo.close()
    def setUp(self):
        from local_app.reservations import ReservationService
        from local_app.scheduler import LocalScheduler
        self.db=self.mongo.database(); self.temp,self.store,self.accounts,_=services(self.db)
        self.now=DUE-60; self.calls=[]; self.records=[]; self.behavior={}; self.after=None
        outer=self
        class FakeSchool(School):
            active_statuses=frozenset({1027})
            def query(self,*args): return copy.copy(outer.records)
            def submit(self,run,seat):
                outer.calls.append((run.begin,seat))
                outcome=outer.behavior.get(run.begin,'accepted')
                if outcome=='accepted':
                    outer.records.append(SchoolRecord('receipt-'+run.begin,self.student_id,seat,
                        run.target_date+' '+run.begin+':00',run.target_date+' '+run.end+':00',1027))
                    if outer.after: outer.after(run)
                    return Submission('accepted',False,0,'ok',True)
                if outcome=='busy': return Submission('rejected',True,*CampusClient.busy_rejection,True)
                if outcome=='unknown': return Submission('rejected',False,999,'unknown',True)
                if outcome=='rate': return Submission('rejected',False,429,'limited',True)
                if outcome=='crash': raise RuntimeError('fake interruption')
                return Submission('uncertain',False,None,'unknown transport',True)
        self.accounts.client_factory=FakeSchool; self.accounts.wall_clock=lambda:self.now
        account=self.accounts.verify_and_save('fixture01','pass','Fixture',None)
        self.account=self.accounts.save_plan(account.id,raw_plan(segments=segments()),account.revision)
        self.reservations=ReservationService(self.accounts,self.store,True,wall_clock=lambda:self.now)
        self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:self.now,monotonic=lambda:self.now)
        self.scheduler.refresh()
        self.runs=resolve_runs(self.account.id,self.account.revision,validate_plan(self.account.plan,self.accounts.catalog),RELEASE)
    def tearDown(self):
        self.scheduler.stop(); self.accounts.close(); self.temp.cleanup(); self.mongo.client.drop_database(self.db.name)
    def execute(self):
        self.now=DUE; self.scheduler._submit(self.runs[0]); return [self.store.get_run(r.key)['status'] for r in self.runs]

    def test_all_receipts_independent_chronological_and_duplicate_never_submits(self):
        self.assertEqual(self.execute(),['success']*3)
        self.assertEqual([begin for begin,_ in self.calls],['09:00','12:00','16:00'])
        self.assertEqual(len(self.records),3)
        for run in self.runs:
            row=self.store.get_run(run.key)
            self.assertEqual(row['result']['status'],'success')
            self.assertEqual(row['spec']['operation_id'],run.operation_id)
        before=list(self.calls); self.scheduler._submit(self.runs[0]); self.scheduler.refresh()
        self.assertEqual(self.calls,before)

    def test_tick_dispatches_one_leader_job_and_executes_all_segments_in_order(self):
        self.assertEqual(len(self.scheduler.jobs),1)
        self.now=DUE-1
        until=time.monotonic()+2
        while time.monotonic()<until:
            self.scheduler.tick()
            job=next(iter(self.scheduler.jobs.values()))
            if job.get('warm') and job['warm'].done():break
            time.sleep(.001)
        self.assertEqual(self.calls,[])
        self.now=DUE
        # Three accepted windows now include two account-wide cooldowns.
        until=time.monotonic()+5
        while time.monotonic()<until and self.store.get_run(self.runs[-1].key)['status']!='success':
            self.scheduler.tick();time.sleep(.001)
        self.assertEqual([self.store.get_run(r.key)['status'] for r in self.runs],['success']*3)
        self.assertEqual([begin for begin,_ in self.calls],['09:00','12:00','16:00'])

    def test_duplicate_dispatch_after_prior_terminal_result_cannot_start_remaining_segments(self):
        self.store.transition_run(self.runs[0].key,('queued',),'success',{},revision=self.runs[0].revision)
        self.now=DUE;self.scheduler._submit(self.runs[0])
        self.assertEqual([self.store.get_run(r.key)['status'] for r in self.runs],['success','skipped','skipped'])
        self.assertEqual(self.calls,[])

    def test_pre_release_segment_id_edit_remains_executable_after_old_jobs_are_skipped(self):
        self.now=DUE-60
        saved=self.accounts.save_plan(self.account.id,self.account.plan|{'segments':[
            segment|{'id':'edited-'+segment['id']} for segment in segments()]},self.account.revision)
        self.scheduler.refresh()
        self.assertEqual([self.store.get_run(r.key)['status'] for r in self.runs],['skipped']*3)
        runs=resolve_runs(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE;self.scheduler.refresh();self.scheduler._submit(runs[0])
        self.assertEqual([self.store.get_run(r.key)['status'] for r in runs],['success']*3)
        self.assertEqual(len(self.calls),3)

    def test_late_start_skips_remain_durable_when_trigger_and_ids_are_changed(self):
        from local_app.scheduler import LocalScheduler
        self.scheduler.stop();self.store.runs.delete_many({});self.now=DUE+1
        self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:self.now,monotonic=lambda:self.now)
        self.scheduler.refresh()
        self.assertTrue(all(self.store.get_run(r.key)['result']['scheduled_cohort_handled'] for r in self.runs))
        self.now=DUE+60
        saved=self.accounts.save_plan(self.account.id,self.account.plan|{'trigger':'07:02:00',
            'segments':[segment|{'id':'later-'+segment['id']} for segment in segments()]},self.account.revision)
        self.scheduler.refresh()
        runs=resolve_runs(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE+120;self.scheduler._submit(runs[0])
        self.assertEqual([self.store.get_run(r.key)['status'] for r in runs],['skipped']*3)
        self.assertEqual(self.calls,[])

    def test_completed_date_cannot_reopen_with_new_trigger_revision_and_segment_ids(self):
        self.assertEqual(self.execute(),['success']*3)
        self.now=DUE+60
        edited=self.account.plan|{'trigger':'07:02:00','segments':[s|{'id':'changed-'+s['id']} for s in segments()]}
        saved=self.accounts.save_plan(self.account.id,edited,self.account.revision)
        self.scheduler.refresh()
        runs=resolve_runs(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.assertEqual([self.store.get_run(r.key)['status'] for r in runs],['skipped']*3)
        self.now=DUE+120;self.scheduler._submit(runs[0])
        self.assertEqual(len(self.calls),3)
        self.assertTrue(all('该使用日期已有' in self.store.get_run(r.key)['message'] for r in runs))
        self.now=DUE+86460;self.scheduler.refresh()
        future=resolve_runs(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),date(2026,10,25))
        self.now=DUE+86520;self.scheduler._submit(future[0])
        self.assertEqual([self.store.get_run(r.key)['status'] for r in future],['success']*3)
        self.assertEqual(len(self.calls),6)

    def test_uncertain_stopped_date_cannot_reopen_after_restarting_with_new_ids(self):
        from local_app.scheduler import LocalScheduler
        self.behavior['09:00']='uncertain';self.assertEqual(self.execute(),['uncertain','skipped','skipped'])
        self.now=DUE+60
        saved=self.accounts.save_plan(self.account.id,self.account.plan|{'trigger':'07:02:00',
            'segments':[s|{'id':'retry-'+s['id']} for s in segments()]},self.account.revision)
        self.scheduler.stop()
        self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:self.now,monotonic=lambda:self.now)
        self.scheduler.refresh()
        runs=resolve_runs(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.assertEqual([self.store.get_run(r.key)['status'] for r in runs],['skipped']*3)
        self.now=DUE+120;self.scheduler._submit(runs[0]);self.assertEqual(len(self.calls),1)

    def test_dispatched_but_unsent_cohort_claim_survives_result_replacement(self):
        self.reservations.guard=lambda:False
        self.assertEqual(self.execute(),['skipped']*3)
        self.assertEqual(self.calls,[])
        claim=self.db.campus_scheduled_cohorts.find_one({'account_id':self.account.id})
        self.assertEqual(claim['target_date'],'2026-10-25')
        self.now=DUE+60;self.reservations.guard=lambda:True
        saved=self.accounts.save_plan(self.account.id,self.account.plan|{'trigger':'07:02:00',
            'segments':[segment|{'id':'again-'+segment['id']} for segment in segments()]},self.account.revision)
        self.scheduler.refresh()
        runs=resolve_runs(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE+120;self.scheduler._submit(runs[0]);self.assertEqual(self.calls,[])
        self.assertEqual([self.store.get_run(r.key)['status'] for r in runs],['skipped']*3)

    def _change_to_scalar(self, trigger='07:02:00'):
        return self.accounts.save_plan(self.account.id,raw_plan(trigger=trigger,begin='07:30',end='09:30'),self.account.revision)

    def test_multi_success_cannot_reopen_as_scalar_same_date_but_future_scalar_still_runs(self):
        self.assertEqual(self.execute(),['success']*3)
        self.now=DUE+60;saved=self._change_to_scalar();self.scheduler.refresh()
        run=resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE+120;self.scheduler._submit(run)
        self.assertEqual(self.store.get_run(run.key)['status'],'skipped');self.assertEqual(len(self.calls),3)
        self.now=DUE+86460;self.scheduler.refresh()
        future=resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),date(2026,10,25))
        self.now=DUE+86520;self.scheduler._submit(future)
        self.assertEqual(self.store.get_run(future.key)['status'],'success');self.assertEqual(len(self.calls),4)
        self.assertEqual(self.db.campus_scheduled_cohorts.count_documents({}),1)

    def test_multi_uncertain_cannot_reopen_as_scalar_even_without_refresh(self):
        self.behavior['09:00']='uncertain';self.assertEqual(self.execute(),['uncertain','skipped','skipped'])
        self.now=DUE+60;saved=self._change_to_scalar()
        run=resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE+120;self.scheduler._submit(run)
        self.assertEqual(self.store.get_run(run.key)['status'],'skipped');self.assertEqual(len(self.calls),1)

    def test_multi_late_start_without_claim_cannot_reopen_as_scalar(self):
        from local_app.scheduler import LocalScheduler
        self.scheduler.stop();self.store.runs.delete_many({});self.now=DUE+1
        self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:self.now,monotonic=lambda:self.now)
        self.scheduler.refresh();self.assertEqual(self.db.campus_scheduled_cohorts.count_documents({}),0)
        self.now=DUE+60;saved=self._change_to_scalar();self.scheduler.refresh()
        run=resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE+120;self.scheduler._submit(run)
        self.assertEqual(self.store.get_run(run.key)['status'],'skipped');self.assertEqual(self.calls,[])

    def test_unstarted_multi_can_change_to_scalar_before_release(self):
        self.now=DUE-60;saved=self._change_to_scalar(trigger='07:00:00');self.scheduler.refresh()
        run=resolve_run(saved.id,saved.revision,validate_plan(saved.plan,self.accounts.catalog),RELEASE)
        self.now=DUE;self.scheduler.refresh();self.scheduler._submit(run)
        self.assertEqual(self.store.get_run(run.key)['status'],'success');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.db.campus_scheduled_cohorts.count_documents({}),0)

    def test_exact_exhausted_busy_may_continue_with_prior_sibling_receipt(self):
        self.behavior['12:00']='busy'
        self.assertEqual(self.execute(),['success','failed','success'])
        self.assertEqual(sum(begin=='12:00' for begin,_ in self.calls),4)
        self.assertTrue(clear_busy_failure(self.store.get_run(self.runs[1].key)))
        self.assertEqual(len(self.records),2)

    def test_uncertain_unknown_rate_and_crash_stop_later_without_rollback(self):
        for outcome in ('uncertain','unknown','rate','crash'):
            with self.subTest(outcome=outcome):
                self.store.runs.delete_many({}); self.db.campus_scheduled_cohorts.delete_many({}); self.records.clear(); self.calls.clear()
                for run in self.runs:self.store.ensure_run(run)
                self.behavior['12:00']=outcome
                states=self.execute()
                self.assertEqual(states[0],'success')
                self.assertEqual(states[1],'failed' if outcome in ('unknown','rate') else 'uncertain')
                self.assertEqual(states[2],'skipped')
                self.assertIn('本段未提交',self.store.get_run(self.runs[2].key)['message'])
                self.assertEqual(len(self.records),1)
                self.assertFalse(any(begin=='16:00' for begin,_ in self.calls))

    def test_revision_change_or_access_revocation_stops_siblings(self):
        self.after=lambda run:self.store.accounts.update_one({'_id':self.account.id},{'$inc':{'revision':1}})
        self.assertEqual(self.execute(),['success','skipped','skipped'])
        self.assertEqual(len(self.calls),1)

    def test_access_revocation_between_segments_stops_siblings(self):
        allowed=[True]; self.accounts.is_allowed=lambda student:allowed[0]
        execute=self.reservations.execute
        def revoke_after_receipt(run):
            result=execute(run)
            if run.begin=='09:00':allowed[0]=False
            return result
        self.reservations.execute=revoke_after_receipt
        self.assertEqual(self.execute(),['success','skipped','skipped'])
        self.assertEqual(len(self.calls),1)

    def test_restart_does_not_replay_unsubmitted_or_interrupted_siblings(self):
        from local_app.scheduler import LocalScheduler
        first=self.runs[0]
        self.store.transition_run(first.key,('queued',),'submitting',{'attempted_seat_id':'1'},revision=first.revision)
        self.store.recover_interrupted(); self.scheduler.stop(); self.now=DUE+1
        self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:self.now)
        self.scheduler.refresh(); self.scheduler.tick()
        self.assertEqual([self.store.get_run(r.key)['status'] for r in self.runs],['uncertain','skipped','skipped'])
        self.assertEqual(self.calls,[])

    def test_starting_after_release_marks_fresh_segments_unsubmitted(self):
        from local_app.scheduler import LocalScheduler
        self.scheduler.stop(); self.store.runs.delete_many({}); self.now=DUE+1
        self.scheduler=LocalScheduler(self.accounts,self.reservations,self.store,wall_clock=lambda:self.now)
        self.scheduler.refresh(); self.scheduler.tick()
        self.assertEqual([self.store.get_run(r.key)['status'] for r in self.runs],['skipped']*3)
        self.assertEqual(self.calls,[])

    def test_plan_save_release_guard_and_cas_do_not_change_existing_plan(self):
        self.now=DUE
        with self.assertRaises(AppError) as raised:
            self.accounts.save_plan(self.account.id,self.account.plan,self.account.revision)
        self.assertEqual(raised.exception.code,'account_busy')
        self.assertEqual(self.store.get_account(self.account.id)['revision'],self.account.revision)
        self.now=DUE+60
        with self.assertRaises(AppError) as raised:
            self.accounts.save_plan(self.account.id,self.account.plan,self.account.revision-1)
        self.assertEqual(raised.exception.code,'conflict')

    def test_pause_stays_available_near_release_and_marks_all_unsent_segments(self):
        self.now=DUE
        saved=self.accounts.save_plan(self.account.id,self.account.plan|{'enabled':False},self.account.revision)
        self.assertFalse(saved.plan['enabled'])
        self.scheduler.refresh()
        self.assertEqual([self.store.get_run(r.key)['status'] for r in self.runs],['skipped']*3)
        self.assertEqual(self.calls,[])

    def test_power_and_dashboard_include_every_effective_segment_without_school_io(self):
        from local_app.config import load_settings
        from local_app.power import PowerService
        from local_app.dashboard import build_dashboard
        self.db.campus_access.insert_one({'_id':'fixture01','enabled':True})
        before=list(self.store.accounts.find())
        result=build_dashboard(self.store,self.accounts.catalog,{'date':'2026-10-24'},
            now=datetime.fromtimestamp(DUE-60,ZONE))
        self.assertEqual(len(result['accounts'][0]['next_runs']),3)
        self.assertEqual(len(result['accounts'][0]['next_run']['segments']),3)
        settings=load_settings(Path(__file__).resolve().parents[1],{'data_dir':Path(self.temp.name).resolve()/'power-data'})
        power=PowerService(settings,self.accounts,self.store,self.scheduler,self.accounts.catalog)
        try:
            result=power.snapshot(DUE+1,100.)
            self.assertEqual(len(result['expected']),3)
            self.assertEqual(len(result['details']),3)
            self.assertIsNone(result['error'])
        finally:power.close()
        self.assertEqual(list(self.store.accounts.find()),before)
        self.assertEqual(self.calls,[])

    def test_unknown_sibling_record_stops_busy_fallback(self):
        self.behavior['12:00']='busy'
        def extra(run):
            if run.begin=='09:00':self.records.append(SchoolRecord('foreign',self.account.student_id,'2','2026-10-25 19:00:00','2026-10-25 21:00:00',1027))
        self.after=extra
        self.assertEqual(self.execute(),['success','uncertain','skipped'])
        self.assertEqual(sum(begin=='12:00' for begin,_ in self.calls),1)
