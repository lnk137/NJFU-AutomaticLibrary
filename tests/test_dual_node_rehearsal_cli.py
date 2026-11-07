"""Offline CLI and process rehearsal acceptance."""
from pathlib import Path
import contextlib
from datetime import datetime
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from experimental.dual_node_rehearsal import __main__ as cli
except ImportError:
    cli = None

ROOT = Path(__file__).resolve().parents[1]


class AvailabilityTests(unittest.TestCase):
    def test_cli_is_available(self):
        self.assertIsNotNone(cli, "the independent rehearsal CLI is not implemented")


@unittest.skipIf(cli is None, "CLI not implemented")
class CliTests(unittest.TestCase):
    def boundary_inputs(self, root):
        from experimental.dual_node_rehearsal.demo import synthetic_case
        from experimental.dual_node_rehearsal.ledger import Authority
        from diagnostics import dual_node_probe as probe
        intent, reports, now = synthetic_case()
        intent['release_at'] = now + 306
        trigger = datetime.fromtimestamp(intent['release_at'], probe.ZONE).strftime('%H:%M:%S')
        for report in reports:
            report['parameters']['release_times'] = sorted({'07:00:00', trigger})
            (root / (report['node'] + '.json')).write_text(json.dumps(report))
        (root / 'intent.json').write_text(json.dumps(intent))
        with Authority.initialize(root / 'authority'): pass
        return intent, now, ['freeze', '--state', str(root / 'authority'), '--intent', str(root / 'intent.json'),
                             '--mac-report', str(root / 'mac.json'), '--ubuntu-report', str(root / 'ubuntu.json'),
                             '--eligible', 'mac', 'ubuntu', '--incumbent', 'mac']

    def test_slow_report_read_cannot_freeze_after_the_selection_window(self):
        from experimental.dual_node_rehearsal.contracts import safe_read_json, intent_identity
        from experimental.dual_node_rehearsal.ledger import Authority
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); intent, now, argv = self.boundary_inputs(root); clock = [now]
            def read(path):
                value = safe_read_json(path)
                if Path(path).name == 'ubuntu.json': clock[0] += 2
                return value
            with patch.object(cli, 'safe_read_json', side_effect=read), patch.object(cli.time, 'time', side_effect=lambda: clock[0]):
                with contextlib.redirect_stdout(io.StringIO()) as output: code = cli.main(argv)
            self.assertIn(code, (2, 3), output.getvalue())
            with Authority(root / 'authority') as authority:
                self.assertIsNone(authority.inspect(intent_identity(intent)[0]))

    def test_slow_ledger_open_cannot_reuse_old_proposal_time(self):
        from experimental.dual_node_rehearsal.contracts import intent_identity
        from experimental.dual_node_rehearsal import ledger
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); intent, now, argv = self.boundary_inputs(root); clock = [now]
            original = ledger.Authority
            class DelayedAuthority(original):
                def __init__(self, path):
                    super().__init__(path); clock[0] += 2
            with patch.object(ledger, 'Authority', DelayedAuthority), patch.object(cli.time, 'time', side_effect=lambda: clock[0]):
                with contextlib.redirect_stdout(io.StringIO()) as output: code = cli.main(argv)
            self.assertEqual(code, 2, output.getvalue())
            with original(root / 'authority') as authority:
                self.assertIsNone(authority.inspect(intent_identity(intent)[0]))

    def assigned_inputs(self, root, *, consumed=False):
        from experimental.dual_node_rehearsal.demo import synthetic_case
        from experimental.dual_node_rehearsal.ledger import Authority
        from experimental.dual_node_rehearsal.selection import propose
        intent, reports, now = synthetic_case()
        proposal = propose(reports, intent, eligible_nodes=['mac', 'ubuntu'], incumbent='mac', now=now)
        with Authority.initialize(root / 'authority') as authority:
            assignment = authority.assign(intent, proposal, now=now)
            if consumed:
                assignment = authority.consume(assignment, node=assignment['owner'], now=intent['release_at'] + 1)
        (root / 'assignment.json').write_text(json.dumps(assignment))
        return intent, proposal, assignment

    def delayed_operation(self, root, argv, clock, source):
        from experimental.dual_node_rehearsal.contracts import safe_read_json
        from experimental.dual_node_rehearsal import ledger
        original = ledger.Authority
        class DelayedAuthority(original):
            def __init__(self, path):
                super().__init__(path); clock[0] += 2
        def read(path):
            value = safe_read_json(path)
            if Path(path).name == 'assignment.json': clock[0] += 2
            return value
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli.time, 'time', side_effect=lambda: clock[0]))
            if source == 'open':
                stack.enter_context(patch.object(ledger, 'Authority', DelayedAuthority))
            else:
                stack.enter_context(patch.object(cli, 'safe_read_json', side_effect=read))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_slow_consume_io_cannot_consume_an_expired_assignment(self):
        from experimental.dual_node_rehearsal.ledger import Authority
        for source in ('open', 'read'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, _, assignment = self.assigned_inputs(root)
                clock = [assignment['expires_at'] - 1]
                argv = ['consume', '--state', str(root / 'authority'),
                        '--assignment', str(root / 'assignment.json'), '--node', assignment['owner']]
                code, result = self.delayed_operation(root, argv, clock, source)
                self.assertEqual(code, 0, result)
                self.assertFalse(result['simulation_consumed'])
                self.assertEqual(result['state'], 'expired')
                self.assertFalse(result['live_execution_authorized'])
                with Authority(root / 'authority') as authority:
                    self.assertEqual(authority.inspect(assignment['task_id'])['state'], 'expired')

    def test_slow_finish_io_preserves_the_current_clock_highwater(self):
        from experimental.dual_node_rehearsal.contracts import RehearsalError
        from experimental.dual_node_rehearsal.ledger import Authority
        for source in ('open', 'read'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                intent, proposal, assignment = self.assigned_inputs(root, consumed=True)
                initial = assignment['release_at'] + 2
                clock = [initial]
                argv = ['finish', '--state', str(root / 'authority'),
                        '--assignment', str(root / 'assignment.json'), '--node', assignment['owner'],
                        '--status', 'uncertain']
                code, result = self.delayed_operation(root, argv, clock, source)
                self.assertEqual(code, 0, result)
                self.assertEqual(result['state'], 'uncertain')
                self.assertFalse(result['live_execution_authorized'])
                with Authority(root / 'authority') as authority:
                    with self.assertRaises(RehearsalError) as caught:
                        authority.assign(intent, proposal, now=initial + 1)
                self.assertEqual(caught.exception.code, 'clock_rollback')

    def assert_argument_error_redacted(self, argv, sentinel):
        result = subprocess.run([sys.executable, '-B', '-m', 'experimental.dual_node_rehearsal', *argv],
                                cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'error')
        self.assertEqual(report['reason'], 'invalid_arguments')
        self.assertTrue(report['rehearsal_only'])
        self.assertFalse(report['live_execution_authorized'])

    def test_invalid_subcommand_is_redacted(self):
        sentinel = 'PRIVATE_ARGUMENT_SENTINEL'
        self.assert_argument_error_redacted([sentinel], sentinel)

    def test_missing_argument_value_is_redacted(self):
        self.assert_argument_error_redacted(['init', '--state'], 'PRIVATE_ARGUMENT_SENTINEL')

    def test_unknown_arguments_are_redacted_before_state_creation(self):
        sentinel = 'PRIVATE_ARGUMENT_SENTINEL'
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve() / 'state'
            self.assert_argument_error_redacted(['init', '--state', str(state), '--unknown', sentinel], sentinel)
            self.assertFalse(state.exists())

    def test_invalid_choices_are_redacted(self):
        sentinel = 'PRIVATE_ARGUMENT_SENTINEL'
        common = ['--intent', '/unused/intent.json', '--mac-report', '/unused/mac.json',
                  '--ubuntu-report', '/unused/ubuntu.json']
        cases = [
            ['evaluate', *common, '--eligible', sentinel, '--incumbent', 'mac'],
            ['evaluate', *common, '--eligible', 'mac', '--incumbent', sentinel],
            ['consume', '--state', '/unused/state', '--assignment', '/unused/assignment.json', '--node', sentinel],
            ['finish', '--state', '/unused/state', '--assignment', '/unused/assignment.json',
             '--node', 'mac', '--status', sentinel],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                self.assert_argument_error_redacted(argv, sentinel)

    def test_help_states_the_rehearsal_boundary(self):
        result = subprocess.run([sys.executable, "-B", "-m", "experimental.dual_node_rehearsal", "--help"],
                                cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Never authorizes real booking", result.stdout)

    def test_init_does_not_open_network_and_refuses_reinitialization(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory).resolve() / "state"
            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(["init", "--state", str(state)]), 0)
                self.assertFalse(json.loads(output.getvalue())["live_execution_authorized"])
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(["init", "--state", str(state)]), 2)
                self.assertFalse(json.loads(output.getvalue())["live_execution_authorized"])

    def test_existing_output_is_refused_before_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); target = root / "existing.json"; target.write_text("keep")
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["init", "--state", str(root / "new-state"), "--output", str(target)])
            self.assertEqual(code, 2); self.assertEqual(target.read_text(), "keep")
            self.assertFalse((root / "new-state").exists())

    def test_real_child_process_demo_has_one_owner_and_one_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / "demo"
            result = subprocess.run([sys.executable, "-B", "-m", "experimental.dual_node_rehearsal",
                                     "demo", "--directory", str(target)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads(result.stdout)
            self.assertFalse(report["live_execution_authorized"])
            self.assertEqual(report["physical_hosts_used"], 1)
            self.assertEqual(report["school_requests"], 0)
            self.assertEqual(report["assignments_created"], 1)
            self.assertEqual(report["successful_consumptions"], 1)
            self.assertGreaterEqual(report.get("assignment_process_count", 0), 4)
            self.assertGreaterEqual(report.get("consumption_process_count", 0), 4)
            self.assertTrue(report["owner_retained_after_uncertain"])
            self.assertTrue(report["restart_preserved"])
            self.assertEqual(report["state"], "uncertain")
            self.assertTrue((target / "acceptance.json").is_file())


if __name__ == "__main__": unittest.main()
