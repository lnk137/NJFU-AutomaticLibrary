"""Native scheduling contract against a fake pmset; never changes system events."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'AL-BackEnd')]
from deploy import mac_power_install as native
from local_app.models import ZONE
from local_app.power import PowerFiles


class NativePowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-native-power-')
        self.directory = Path(self.temp.name).resolve()
        self.files = PowerFiles(self.directory / 'private')
        self.files.update({'enabled': True, 'allow_live': True}, 0)
        self.system = self.directory / 'system'; self.system.mkdir(mode=0o700)
        self.state = self.directory / 'events.json'
        self.unrelated = {'type': 'wake', 'date': '10/27/2026 08:00:00', 'owner': 'other.application'}
        self.state.write_text(json.dumps([self.unrelated]))
        self.fake = self.directory / 'fake-pmset'
        self.fake.write_text('#!' + str(ROOT / '.venv/bin/python') + '''
import json,os,sys
from pathlib import Path
from datetime import datetime
p=Path(os.environ['NJFU_POWER_TEST_EVENTS']);rows=json.loads(p.read_text());args=sys.argv[1:]
def date(value):
 for fmt in ('%m/%d/%Y %H:%M:%S','%m/%d/%y %H:%M:%S'):
  try:return datetime.strptime(value,fmt)
  except ValueError:pass
 raise ValueError(value)
if args==['-g','sched']:
 print('Scheduled power events:')
 for i,row in enumerate(rows):print(f" [{i}]  {row['type']} at {row['date']} by '{row['owner']}'")
elif args[:2]==['schedule','cancel']:
 assert len(args[3].split()[0].split('/')[-1])==2, 'pmset input contract requires MM/dd/yy'
 rows=[r for r in rows if not (r['type']==args[2] and date(r['date'])==date(args[3]) and r['owner']==args[4])]
 p.write_text(json.dumps(rows))
elif args[0]=='schedule' and args[1]=='wake':
 rows.append({'type':'wake','date':date(args[2]).strftime('%m/%d/%Y %H:%M:%S'),'owner':args[3]});p.write_text(json.dumps(rows))
else:raise AssertionError('Forbidden power operation: '+str(args))
''')
        self.fake.chmod(0o700)
        self.helper = self.directory / 'helper.sh'
        self.helper.write_text(native.render_helper(self.files.base / 'config.plist', os.getuid(),
            system_dir=self.system, pmset=self.fake, run_uid=os.getuid()))
        self.helper.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self):
        result = subprocess.run(['/bin/sh', str(self.helper)], capture_output=True, text=True,
            env=os.environ | {'NJFU_POWER_TEST_EVENTS': str(self.state)}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.state.read_text())

    def test_shell_syntax_and_next_beijing_wake_without_touching_other_events(self):
        subprocess.run(['/bin/sh', '-n', str(self.helper)], check=True)
        rows = self.run_helper()
        self.assertIn(self.unrelated, rows)
        owned = [row for row in rows if row['owner'] == native.owner(os.getuid())]
        self.assertEqual(len(owned), 1)
        expected = datetime.now(ZONE).replace(hour=6, minute=55, second=0, microsecond=0)
        if expected.timestamp() <= datetime.now(ZONE).timestamp():
            expected += timedelta(days=1)
        actual = datetime.strptime(owned[0]['date'], '%m/%d/%Y %H:%M:%S').astimezone()
        self.assertEqual(actual.timestamp(), expected.timestamp())
        status = plistlib.loads((self.system / 'status.plist').read_bytes())
        self.assertTrue(status['armed'])
        self.assertEqual(status['next_wake'], int(expected.timestamp()))

    def test_repeated_tick_is_idempotent_and_pause_cancels_only_owned_event(self):
        first = self.run_helper()
        self.assertEqual(self.run_helper(), first)
        self.files.update({'paused': True}, 1)
        self.assertEqual(self.run_helper(), [self.unrelated])
        self.assertFalse(plistlib.loads((self.system / 'status.plist').read_bytes())['armed'])

    def test_invalid_or_symlink_config_cannot_arm_an_event(self):
        (self.files.base / 'config.plist').chmod(0o644)
        self.assertEqual(self.run_helper(), [self.unrelated])
        target = self.files.base / 'config.plist'
        target.chmod(0o600); target.rename(self.files.base / 'old.plist')
        target.symlink_to(self.files.base / 'old.plist')
        self.assertEqual(self.run_helper(), [self.unrelated])

    def test_generated_plists_and_privileged_script_have_fixed_scope(self):
        agent = native.agent_plist(ROOT)
        daemon = native.daemon_plist()
        self.assertEqual(agent['ProgramArguments'][2], str(ROOT / 'power_agent.py'))
        self.assertTrue(agent['KeepAlive'])
        self.assertEqual(daemon['ProgramArguments'], ['/bin/sh', '/Library/Application Support/NJFUAutoLib/wake-helper.sh'])
        script = native.install_script(ROOT, os.getuid())
        self.assertNotIn('sleepnow', script)
        self.assertNotIn('disablesleep', script)
        self.assertNotIn('pmset repeat', script)
        self.assertNotIn('.venv/bin/python', script)
        subprocess.run(['/bin/sh', '-n'], input=script, text=True, check=True)

    def test_quiet_interval_has_no_native_event_or_status_file_writes(self):
        clock = self.directory / 'fake-date'
        clock.write_text("#!/bin/sh\nif [ \"$1\" = '+%H%M%S' ]; then echo 070010; else exec /bin/date \"$@\"; fi\n")
        clock.chmod(0o700)
        self.helper.write_text(self.helper.read_text().replace('/bin/date', str(clock)))
        self.assertEqual(self.run_helper(), [self.unrelated])
        self.assertFalse((self.system / 'status.plist').exists())

    def installed_fixture(self):
        daemon_dir = self.directory / 'daemons'; daemon_dir.mkdir(mode=0o700)
        daemon = daemon_dir / 'wake.plist'
        marker = {'installation': native.INSTALL_ID, 'root': str(ROOT), 'uid': os.getuid(), 'generation': 'a'*32}
        (self.system / 'installation.plist').write_bytes(plistlib.dumps(marker))
        (self.system / 'installation.plist').chmod(0o600)
        (self.system / 'wake-helper.sh').write_text(self.helper.read_text())
        (self.system / 'wake-helper.sh').chmod(0o700)
        daemon.write_bytes(plistlib.dumps(native.daemon_plist(system_dir=self.system)))
        daemon.chmod(0o600)
        return daemon

    def test_root_trust_refuses_writable_paths_and_foreign_objects_before_mutation(self):
        daemon = self.installed_fixture()
        for target, mode in ((self.system, 0o777), (self.system, 0o775),
                             (self.system/'wake-helper.sh', 0o776), (self.system/'installation.plist', 0o666)):
            old = target.stat().st_mode & 0o777
            target.chmod(mode)
            with self.subTest(target=target,mode=mode), self.assertRaises(RuntimeError):
                native.validate_paths(ROOT, os.getuid(), system_dir=self.system, daemon_path=daemon, uid=os.getuid())
            before = sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob('*'))
            result=subprocess.run(['/bin/sh'],input=native.install_script(ROOT,os.getuid(),system_dir=self.system,
                daemon_path=daemon,run_uid=os.getuid()),text=True,capture_output=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob('*')),before)
            self.assertEqual(target.stat().st_mode & 0o777, mode)
            target.chmod(old)
        external = self.directory/'external';external.mkdir(mode=0o700)
        protected=external/'protected';protected.write_text('unchanged');protected.chmod(0o600)
        (self.system/'previous').symlink_to(external,target_is_directory=True)
        with self.assertRaises(RuntimeError):native.validate_paths(ROOT,os.getuid(),system_dir=self.system,daemon_path=daemon,uid=os.getuid())
        self.assertEqual(protected.read_text(),'unchanged')
        self.assertEqual(protected.stat().st_mode & 0o777,0o600)
        (self.system/'previous').unlink()
        daemon.write_bytes(plistlib.dumps({'Label':'foreign','ProgramArguments':['/foreign']}))
        with self.assertRaises(RuntimeError):native.validate_paths(ROOT,os.getuid(),system_dir=self.system,daemon_path=daemon,uid=os.getuid())

    def fake_launchctl(self, loaded):
        state = self.directory/'launch.json'
        state.write_text(json.dumps({'loaded':loaded}))
        binary = self.directory/'fake-launchctl'
        binary.write_text('#!'+str(ROOT/'.venv/bin/python')+'''\nimport os,sys,json\nfrom pathlib import Path\np=Path(os.environ['NJFU_POWER_TEST_LAUNCH']);data=json.loads(p.read_text());args=sys.argv[1:]\nif args[0]=='print':sys.exit(0 if data['loaded'] else 113)\nif args[0]=='bootout':data['loaded']=False\nelif args[0]=='bootstrap':data['loaded']=True\nelif args[0] not in ('enable','kickstart'):raise AssertionError(args)\np.write_text(json.dumps(data))\n''')
        binary.chmod(0o700)
        return binary,state

    def test_upgrade_interruption_restores_validated_old_files_and_registration(self):
        daemon = self.installed_fixture()
        binary,state = self.fake_launchctl(True)
        old_helper=(self.system/'wake-helper.sh').read_bytes()
        old_marker=(self.system/'installation.plist').read_bytes()
        old_daemon=daemon.read_bytes()
        for phase in ('staged','bootout','system-published','daemon-published','bootstrapped'):
            script=native.install_script(ROOT,os.getuid(),system_dir=self.system,daemon_path=daemon,
                run_uid=os.getuid(),launchctl=binary,generation='b'*32)
            script=script.replace('# phase: '+phase+'\n','# phase: '+phase+'\nfalse\n')
            result=subprocess.run(['/bin/sh'],input=script,text=True,capture_output=True,
                env=os.environ|{'NJFU_POWER_TEST_LAUNCH':str(state)},timeout=10)
            with self.subTest(phase=phase):
                self.assertNotEqual(result.returncode,0)
                self.assertEqual((self.system/'wake-helper.sh').read_bytes(),old_helper)
                self.assertEqual((self.system/'installation.plist').read_bytes(),old_marker)
                self.assertEqual(daemon.read_bytes(),old_daemon)
                self.assertTrue(json.loads(state.read_text())['loaded'])

    def test_first_publication_is_complete_and_partial_install_can_be_retried(self):
        self.system.rmdir()
        daemon_dir=self.directory/'daemons';daemon_dir.mkdir(mode=0o700)
        daemon=daemon_dir/'wake.plist'
        binary,state=self.fake_launchctl(False)
        for phase in ('staged','system-published','daemon-published'):
            script=native.install_script(ROOT,os.getuid(),system_dir=self.system,daemon_path=daemon,
                run_uid=os.getuid(),launchctl=binary,generation='c'*32)
            result=subprocess.run(['/bin/sh'],input=script.replace('# phase: '+phase+'\n','# phase: '+phase+'\nfalse\n'),
                text=True,capture_output=True,env=os.environ|{'NJFU_POWER_TEST_LAUNCH':str(state)},timeout=10)
            self.assertNotEqual(result.returncode,0)
            native.validate_paths(ROOT,os.getuid(),system_dir=self.system,daemon_path=daemon,uid=os.getuid())
        result=subprocess.run(['/bin/sh'],input=native.install_script(ROOT,os.getuid(),system_dir=self.system,
            daemon_path=daemon,run_uid=os.getuid(),launchctl=binary,generation='d'*32),text=True,capture_output=True,
            env=os.environ|{'NJFU_POWER_TEST_LAUNCH':str(state)},timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(state.read_text())['loaded'])

    def test_user_agent_bootstrap_failure_remains_private_and_repairable(self):
        home=self.directory/'home';(home/'Library/LaunchAgents').mkdir(parents=True,mode=0o700)
        with patch.object(native.subprocess,'run',side_effect=[subprocess.CompletedProcess([],113),
              subprocess.CalledProcessError(1,[]) ]):
            with self.assertRaises(subprocess.CalledProcessError):native.register_agent(ROOT,home=home)
        path=home/'Library/LaunchAgents/org.njfu.autolib.power.plist'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(plistlib.loads(path.read_bytes()),native.agent_plist(ROOT))
        with patch.object(native.subprocess,'run',side_effect=[subprocess.CompletedProcess([],113),subprocess.CompletedProcess([],0)]):
            native.register_agent(ROOT,home=home)
        self.assertEqual(plistlib.loads(path.read_bytes()),native.agent_plist(ROOT))


if __name__ == '__main__':
    unittest.main()
