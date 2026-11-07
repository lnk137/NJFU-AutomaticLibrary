import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('public_runner_under_test',ROOT/'public_run.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class PublicRunnerTests(unittest.TestCase):
    def test_managed_runner_falls_back_on_agent_failure_and_releases_on_fresh_delegation(self):
        files=Mock();files.delegated.return_value=False
        child=Mock();child.poll.return_value=None
        spawn=Mock(return_value=child)
        guard=module.AwakeGuard(True,files=files,spawn=spawn)
        with patch.object(module.sys,'platform','darwin'):
            guard.step(100.,100.)
            self.assertEqual(spawn.call_count,1)
            files.delegated.return_value=True
            guard.step(101.,101.)
            child.terminate.assert_called_once()
            files.delegated.side_effect=OSError('stale or unreadable agent')
            guard.step(102.,102.)
            self.assertEqual(spawn.call_count,2)
        guard.close()

    def test_ssh_does_not_expose_a_public_listener_or_skip_host_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            files=[]
            for name in ('key','known_hosts'):
                p=Path(directory)/name;p.write_text('fixture');p.chmod(0o600);files.append(str(p))
            config={'host':'183.66.27.20','port':23486,'user':'njfu-relay','key':files[0],'known_hosts':files[1],'remote_port':15003}
            command=module.ssh_command(config)
            self.assertIn('StrictHostKeyChecking=yes',command)
            self.assertIn('BatchMode=yes',command)
            self.assertIn('127.0.0.1:15003:127.0.0.1:5003',command)
            self.assertIn('127.0.0.1:15004:127.0.0.1:5027',command)
            self.assertNotIn('-g',command)
            with self.assertRaises(ValueError):module.ssh_command(config|{'user':'root'})
            Path(files[0]).chmod(0o644)
            with self.assertRaises(ValueError):module.ssh_command(config)
