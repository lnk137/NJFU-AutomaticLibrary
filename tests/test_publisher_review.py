from pathlib import Path as _Path
import sys as _sys
_candidate_backend = _Path(__file__).resolve().parents[1] / 'AL-BackEnd'
_sys.path.insert(0, str(_candidate_backend))
import local_app as _loaded_app
assert _Path(_loaded_app.__file__).resolve().is_relative_to(_candidate_backend.resolve())
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from local_app.dual_node.publisher import PlanPublisherLoop, plan_tasks, PublishError
from test_agent import task

class PublisherReviewTests(unittest.TestCase):
    def test_removed_registration_and_postrelease_disabled_plan_cancel_server_task(self):
        for registrations, now in (({},1990),({'local':{'account_ref':'fixture-one','credential_revision':1}},2001)):
            with self.subTest(registrations=registrations), tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'publisher.json'
                p.write_text(json.dumps({'schema':1,'enabled':True,'ssh_config':'/private/ssh',
                       'ssh_host':'qa','registrations':registrations,'days':7}));p.chmod(0o600)
                runtime=SimpleNamespace(store=Mock(),accounts=SimpleNamespace(catalog={}),managed_execution=Mock())
                runtime.store.list_accounts.return_value=[]
                runtime.managed_execution.is_managed.return_value=True
                loop=PlanPublisherLoop(runtime,p)
                client=Mock();client.call.side_effect=lambda op,**body: {'tasks':[task()]} if op=='tasks' else {}
                loop.client=client;loop.client_identity=('/private/ssh','qa')
                with patch('time.time',return_value=now):loop.sync_once()
                self.assertIn('cancel',[call.args[0] for call in client.call.call_args_list])

    def test_max_horizon_is_consistent_seven_days(self):
        with self.assertRaisesRegex(PublishError,'invalid_publish_horizon'):
            plan_tasks([],{}, {},now=2000,days=8)

if __name__=='__main__':unittest.main()
