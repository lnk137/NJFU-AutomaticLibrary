"""Real local-ledger behavior; no school or production access."""
from concurrent.futures import ProcessPoolExecutor
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))


def modules():
    return (importlib.import_module('experimental.dual_node_rehearsal.ledger'),
            importlib.import_module('experimental.dual_node_rehearsal.contracts'))


def intent():
    return {'schema': 1, 'account_ref': 'fixture-user', 'day': '2026-11-06',
            'kind': 'S', 'operation': '-', 'plan_version': 1,
            'start_minute': 480, 'end_minute': 720, 'seat_ref': 'fixture-seat',
            'release_at': 1793919600.0}


def proposal(node='mac'):
    _, contract = modules()
    task, binding = contract.intent_identity(intent())
    return contract.envelope(status='proposed', selected_node=node,
        reason='fixture_preference', task_id=task, binding_sha256=binding,
        evidence_sha256={'mac': 'a' * 64, 'ubuntu': 'b' * 64},
        evaluated_at=1793919000.0, release_at=1793919600.0,
        expires_at=1793919630.0, comparison={}, eligible_nodes=['mac', 'ubuntu'],
        incumbent='mac', evidence_trust='operator_imported_unsigned',
        booking_readiness='not_checked', clock_accuracy='not_verified')


def competing_assignment(args):
    state, node = args
    ledger, _ = modules()
    with ledger.Authority(state) as authority:
        return authority.assign(intent(), proposal(node), now=1793919000.0)


def competing_consume(args):
    state, assignment = args
    ledger, _ = modules()
    with ledger.Authority(state) as authority:
        return authority.consume(assignment, node=assignment['owner'], now=1793919600.0)


def assign_without_reply(state):
    ledger, _ = modules()
    with ledger.Authority(state) as authority:
        authority.assign(intent(), proposal('mac'), now=1793919000.0)
        os._exit(37)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.ledger, self.contract = modules()
        except ModuleNotFoundError:
            self.fail('single-owner ledger implementation is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-rehearsal-ledger-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'authority'

    def make(self):
        value = self.ledger.Authority.initialize(self.state)
        self.addCleanup(value.close)
        return value

    def test_initialize_is_explicit_private_and_never_resets_existing_state(self):
        old = os.umask(0o002)
        try:
            authority = self.make()
        finally:
            os.umask(old)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for name in ('identity.json', 'owner.lock', 'state.db'):
            self.assertEqual((self.state / name).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(self.contract.RehearsalError):
            self.ledger.Authority.initialize(self.state)
        missing = self.state.parent / 'missing'
        with self.assertRaises(self.contract.RehearsalError):
            self.ledger.Authority(missing)
        self.assertFalse(missing.exists())
        self.assertIsNone(authority.inspect('c' * 64))

    def test_intent_owner_binding_and_evidence_are_immutable_on_reopen(self):
        authority = self.make()
        first = authority.assign(intent(), proposal(), now=1793919000.0)
        self.assertTrue(first['created'])
        self.assertFalse(first['live_execution_authorized'])
        authority.close()
        with self.ledger.Authority(self.state) as reopened:
            again = reopened.assign(intent(), proposal('ubuntu'), now=1793919001.0)
            for key in ('ledger_id', 'task_id', 'binding_sha256', 'decision_id',
                        'owner', 'created_at', 'evidence_sha256'):
                self.assertEqual(first[key], again[key])
            self.assertFalse(again['created'])
            changed = intent() | {'seat_ref': 'fixture-other'}
            with self.assertRaises(self.contract.RehearsalError):
                reopened.assign(changed, proposal('ubuntu'), now=1793919002.0)

    def test_consume_is_once_due_only_and_terminals_do_not_transfer(self):
        authority = self.make()
        assigned = authority.assign(intent(), proposal(), now=1793919000.0)
        with self.assertRaises(self.contract.RehearsalError):
            authority.consume(assigned, node='ubuntu', now=1793919001.0)
        with self.assertRaises(self.contract.RehearsalError):
            authority.consume(assigned | {'decision_id': '0' * 32}, node='mac', now=1793919001.0)
        early = authority.consume(assigned, node='mac', now=1793919599.0)
        self.assertFalse(early['simulation_consumed'])
        consumed = authority.consume(assigned, node='mac', now=1793919600.0)
        self.assertTrue(consumed['simulation_consumed'])
        self.assertFalse(authority.consume(assigned, node='mac', now=1793919600.0)['simulation_consumed'])
        finished = authority.finish(assigned, node='mac', status='uncertain', now=1793919601.0)
        self.assertEqual(finished['state'], 'uncertain')
        self.assertEqual(authority.finish(assigned, node='mac', status='completed', now=1793919602.0)['state'], 'uncertain')
        self.assertEqual(authority.assign(intent(), proposal('ubuntu'), now=1793919603.0)['owner'], 'mac')

    def test_expiry_revocation_and_clock_rollback_fail_closed(self):
        authority = self.make()
        assigned = authority.assign(intent(), proposal(), now=1793919000.0)
        with self.assertRaises(self.contract.RehearsalError):
            authority.finish(assigned, node='mac', status='completed', now=1793919001.0)
        expired = authority.consume(assigned, node='mac', now=1793919631.0)
        self.assertEqual(expired['state'], 'expired')
        self.assertFalse(expired['simulation_consumed'])
        with self.assertRaises(self.contract.RehearsalError):
            authority.assign(intent(), proposal(), now=1793919000.0)
        self.assertEqual(authority.assign(intent(), proposal('ubuntu'), now=1793919632.0)['owner'], 'mac')
        other = intent() | {'account_ref': 'fixture-revoked'}
        p = proposal(); p['task_id'], p['binding_sha256'] = self.contract.intent_identity(other)
        # A second intent has its own release window and is not a reset path.
        other['release_at'] += 900
        p['task_id'], p['binding_sha256'] = self.contract.intent_identity(other)
        p.update(evaluated_at=1793919640.0, release_at=other['release_at'], expires_at=other['release_at']+30)
        revoked = authority.assign(other, p, now=1793919640.0)
        self.assertEqual(authority.finish(revoked, node='mac', status='revoked', now=1793919641.0)['state'], 'revoked')

    def test_refused_late_finish_preserves_time_and_blocks_earlier_consumption(self):
        authority = self.make(); release = intent()['release_at']
        assigned = authority.assign(intent(), proposal(), now=release-600)
        with self.assertRaisesRegex(self.contract.RehearsalError, 'invalid_transition'):
            authority.finish(assigned, node='mac', status='completed', now=release+40)
        unchanged = authority.inspect(assigned['task_id'])
        for key in ('owner', 'state', 'decision_id', 'binding_sha256'):
            self.assertEqual(unchanged[key], assigned[key])
        with self.assertRaisesRegex(self.contract.RehearsalError, 'clock_rollback'):
            authority.consume(assigned, node='mac', now=release)
        self.assertEqual(authority.inspect(assigned['task_id'])['state'], 'selected')

    def test_refused_owner_ticket_and_outcome_observations_survive_reopen(self):
        release = intent()['release_at']; parent = self.state.parent
        for reason in ('wrong_owner', 'wrong_assignment', 'invalid_outcome'):
            with self.subTest(reason=reason):
                self.state = parent/reason
                authority = self.make()
                assigned = authority.assign(intent(), proposal(), now=release-600)
                with self.assertRaisesRegex(self.contract.RehearsalError, reason):
                    if reason == 'wrong_owner':
                        authority.consume(assigned, node='ubuntu', now=release+40)
                    elif reason == 'wrong_assignment':
                        authority.consume(assigned | {'decision_id': '0'*32}, node='mac', now=release+40)
                    else:
                        authority.finish(assigned, node='mac', status='invalid', now=release+40)
                authority.close()
                with self.ledger.Authority(self.state) as reopened:
                    unchanged = reopened.inspect(assigned['task_id'])
                    for key in ('owner', 'state', 'decision_id', 'binding_sha256'):
                        self.assertEqual(unchanged[key], assigned[key])
                    last = float(reopened.db.execute("SELECT value FROM meta WHERE key='last_wall'").fetchone()[0])
                    self.assertEqual(last, release+40)
                    with self.assertRaisesRegex(self.contract.RehearsalError, 'clock_rollback'):
                        reopened.consume(assigned, node='mac', now=release)

    def test_sqlite_refusal_rolls_back_business_write_but_keeps_observed_time(self):
        authority = self.make(); release = intent()['release_at']
        assigned = authority.assign(intent(), proposal(), now=release-600)
        # RAISE(FAIL) leaves an executed update to the enclosing transaction's
        # rollback, exercising a real SQLite error after the business write.
        authority.db.execute("CREATE TEMP TRIGGER refuse_consumed AFTER UPDATE OF state ON tasks "
                             "WHEN NEW.state='consumed' BEGIN SELECT RAISE(FAIL,'fixture_refusal'); END")
        with self.assertRaises(self.contract.RehearsalError):
            authority.consume(assigned, node='mac', now=release)
        unchanged = authority.inspect(assigned['task_id'])
        self.assertEqual(unchanged['state'], 'selected')
        self.assertEqual(unchanged['owner'], assigned['owner'])
        self.assertEqual(float(authority.db.execute("SELECT value FROM meta WHERE key='last_wall'").fetchone()[0]), release)
        with self.assertRaisesRegex(self.contract.RehearsalError, 'clock_rollback'):
            authority.consume(assigned, node='mac', now=release-1)

    def test_highwater_and_business_commit_failures_both_refuse_business_success(self):
        class CommitFailure:
            def __init__(self, connection, fail_on):
                self.connection = connection; self.fail_on = fail_on; self.calls = 0
            def __getattr__(self, name): return getattr(self.connection, name)
            def commit(self):
                self.calls += 1
                if self.calls == self.fail_on: raise sqlite3.OperationalError('fixture_commit_failure')
                return self.connection.commit()
        release = intent()['release_at']; parent = self.state.parent
        for fail_on in (1, 2):
            with self.subTest(commit=fail_on):
                self.state = parent/('commit-'+str(fail_on)); authority = self.make()
                assigned = authority.assign(intent(), proposal(), now=release-600)
                with patch.object(authority, 'db', CommitFailure(authority.db, fail_on)):
                    with self.assertRaises(self.contract.RehearsalError):
                        authority.consume(assigned, node='mac', now=release)
                unchanged = authority.inspect(assigned['task_id'])
                self.assertEqual(unchanged['state'], 'selected')
                self.assertEqual(unchanged['owner'], assigned['owner'])
                last = float(authority.db.execute("SELECT value FROM meta WHERE key='last_wall'").fetchone()[0])
                self.assertEqual(last, release-600 if fail_on == 1 else release)

    def test_invalid_initial_proposal_and_late_freeze_are_refused(self):
        authority = self.make()
        for changed in ({'status': 'blocked'}, {'selected_node': 'other'},
                        {'live_execution_authorized': True}, {'binding_sha256': 'c'*64},
                        {'evidence_sha256': {'mac': 'a'*64}}):
            with self.subTest(changed=changed), self.assertRaises(self.contract.RehearsalError):
                authority.assign(intent(), proposal() | changed, now=1793919000.0)
        with self.assertRaises(self.contract.RehearsalError):
            authority.assign(intent(), proposal(), now=1793919599.0)

    def test_real_processes_compete_for_one_owner_and_one_consumption(self):
        self.make().close()
        context = multiprocessing.get_context('spawn')
        with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
            rows = list(pool.map(competing_assignment, [(str(self.state), node) for node in ('mac', 'ubuntu') * 4]))
        self.assertEqual(sum(row['created'] for row in rows), 1)
        self.assertEqual(len({row['owner'] for row in rows}), 1)
        with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
            spent = list(pool.map(competing_consume, [(str(self.state), rows[0])] * 8))
        self.assertEqual(sum(row['simulation_consumed'] for row in spent), 1)

    def test_committed_decision_survives_process_exit_before_reply(self):
        self.make().close()
        child = multiprocessing.get_context('spawn').Process(target=assign_without_reply, args=(str(self.state),))
        child.start(); child.join(10)
        self.assertFalse(child.is_alive()); self.assertEqual(child.exitcode, 37)
        with self.ledger.Authority(self.state) as authority:
            result = authority.assign(intent(), proposal('ubuntu'), now=1793919001.0)
            self.assertEqual(result['owner'], 'mac'); self.assertFalse(result['created'])

    def test_missing_corrupt_replaced_hardlinked_and_copied_state_refuse(self):
        authority = self.make()
        authority.assign(intent(), proposal(), now=1793919000.0)
        copy = self.state.parent / 'copied'
        shutil.copytree(self.state, copy)
        with self.assertRaises(self.contract.RehearsalError):
            self.ledger.Authority(copy)
        db = self.state / 'state.db'
        hardlink = self.state.parent / 'linked.db'; os.link(db, hardlink)
        with self.assertRaises(self.contract.RehearsalError):
            authority.inspect(self.contract.intent_identity(intent())[0])
        hardlink.unlink()
        replacement = self.state.parent / 'replacement.db'; replacement.write_bytes(db.read_bytes()); replacement.chmod(0o600)
        os.replace(replacement, db)
        with self.assertRaises(self.contract.RehearsalError):
            authority.consume(authority.inspect('c'*64) or {}, node='mac', now=1793919600.0)
        authority.close()
        with self.assertRaises(self.contract.RehearsalError):
            self.ledger.Authority(self.state)

    def test_corrupt_marker_missing_database_and_unsafe_mode_refuse(self):
        authority = self.make(); authority.close()
        marker = self.state / 'identity.json'; raw = marker.read_bytes()
        marker.write_text('{broken')
        with self.assertRaises(self.contract.RehearsalError):self.ledger.Authority(self.state)
        marker.write_bytes(raw); marker.chmod(0o640)
        with self.assertRaises(self.contract.RehearsalError):self.ledger.Authority(self.state)
        marker.chmod(0o600); (self.state/'state.db').unlink()
        with self.assertRaises(self.contract.RehearsalError):self.ledger.Authority(self.state)
        self.assertFalse((self.state/'state.db').exists())

    def test_in_place_database_header_corruption_is_rejected_while_open(self):
        authority = self.make()
        assigned = authority.assign(intent(), proposal(), now=1793919000.0)
        with (self.state/'state.db').open('r+b') as stream:
            stream.write(b'not sqlite data!'); stream.flush(); os.fsync(stream.fileno())
        with self.assertRaises(self.contract.RehearsalError):
            authority.inspect(assigned['task_id'])

    def test_symlink_and_same_bytes_marker_replacement_are_rejected(self):
        authority = self.make()
        marker = self.state/'identity.json'
        replacement = self.state.parent/'marker-copy'; replacement.write_bytes(marker.read_bytes())
        replacement.chmod(0o600); os.replace(replacement, marker)
        with self.assertRaises(self.contract.RehearsalError):authority.inspect('c'*64)
        with self.assertRaises(self.contract.RehearsalError):self.ledger.Authority(self.state)
        authority.close()
        db = self.state/'state.db'; kept = self.state.parent/'kept.db'; db.rename(kept); db.symlink_to(kept)
        with self.assertRaises(self.contract.RehearsalError):self.ledger.Authority(self.state)

    def test_sqlite_contention_is_bounded_and_does_not_change_owner(self):
        authority = self.make()
        assigned = authority.assign(intent(), proposal(), now=1793919000.0)
        connection = sqlite3.connect(self.state/'state.db', isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE'); started=time.monotonic()
            with self.assertRaises(self.contract.RehearsalError):
                authority.consume(assigned, node='mac', now=1793919600.0)
            self.assertLess(time.monotonic()-started, 3)
        finally:connection.rollback(); connection.close()
        self.assertEqual(authority.inspect(assigned['task_id'])['state'], 'selected')


if __name__ == '__main__':
    unittest.main()
