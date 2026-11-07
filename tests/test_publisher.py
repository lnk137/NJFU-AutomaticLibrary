from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
from datetime import datetime
import unittest
from local_app.models import ZONE, Seat
from local_app.dual_node.publisher import plan_tasks, PublishError


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,7,8,tzinfo=ZONE).timestamp()
        self.row={'_id':'local-account','revision':2,'credential_revision':1,
                  'student_id':'never-published','secret':'never-published',
                  'plan':{'seat_ids':['1'],'day_offset':1,'mode':'day','begin':'09:00','end':'12:00',
                          'trigger':'07:00:00','enabled':True}}
        self.reg={'local-account':{'account_ref':'acct-'+'a'*32,'credential_revision':1}}
        self.catalog={'1':Seat('1','A1','2F')}

    def test_next_day_only_and_opaque_reference(self):
        tasks=plan_tasks([self.row], self.reg, self.catalog, now=self.now, days=3)
        self.assertEqual(len(tasks),2)
        self.assertEqual(tasks[0]['day'],'2026-11-09')
        self.assertEqual(tasks[0]['start_minute'],540)
        self.assertNotIn('never-published',repr(tasks))
        self.assertEqual(tasks[0]['account_ref'],'acct-'+'a'*32)

    def test_disabled_and_exceptions_respected(self):
        self.row['plan']['enabled']=False
        self.assertEqual(plan_tasks([self.row],self.reg,self.catalog,now=self.now),[])
        self.row['plan']['enabled']=True
        self.row['date_exceptions']={'2026-11-09':{'action':'skip'}}
        tasks=plan_tasks([self.row],self.reg,self.catalog,now=self.now,days=3)
        self.assertEqual([x['day'] for x in tasks],['2026-11-10'])

    def test_revision_rotation_requires_enrollment(self):
        self.row['credential_revision']=2
        with self.assertRaisesRegex(PublishError,'credential_enrollment_mismatch'):
            plan_tasks([self.row],self.reg,self.catalog,now=self.now)

    def test_unmanaged_accounts_ignored(self):
        self.assertEqual(plan_tasks([self.row],{},self.catalog,now=self.now),[])


if __name__=='__main__':unittest.main()
