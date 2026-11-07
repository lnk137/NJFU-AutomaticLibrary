"""Scheduled imports must use the same admission as an online plan save."""
import unittest
from unittest.mock import Mock

import test_form_imports as fixture


class FormImportPlanAdmissionTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FormImportTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FormImportTests.tearDownClass.__func__)
    setUp = fixture.FormImportTests.setUp
    tearDown = fixture.FormImportTests.tearDown
    wait = fixture.FormImportTests.wait
    create = fixture.FormImportTests.create
    existing = fixture.FormImportTests.existing

    def disabled_with_exception(self, *, segmented=True, managed=True):
        account = self.existing()
        account = self.accounts.save_plan(account.id, account.plan | {'enabled': False}, account.revision)
        gate = Mock()
        gate.is_managed.side_effect = lambda account_id: managed and account_id == account.id
        self.accounts.managed_gate = gate
        override = {'action': 'override', 'date': '2026-10-26'}
        if segmented:
            override['segments'] = [dict(id='morning', begin='09:00', end='12:00', seat_ids=['1']),
                                    dict(id='afternoon', begin='14:00', end='17:00', seat_ids=['2'])]
        else:
            override.update(begin='14:00', end='17:00', seat_ids=['2'])
        return self.accounts.update_date_exceptions(account.id, override, account.revision), gate

    def assert_denied_without_account_commit(self, account, batch):
        before = self.store.get_account(account.id)
        history = list(self.store.runs.find())
        result = self.wait(self.forms.confirm(batch['id']))
        self.assertEqual(result['rows'][0]['status'], 'error')
        self.assertIn('双机', result['rows'][0]['message'])
        self.assertEqual(self.store.get_account(account.id), before)
        self.assertEqual(list(self.store.runs.find()), history)
        self.assertEqual(fixture.ImportSchool.posts, [])

    def test_managed_import_cannot_enable_preserved_segmented_exception(self):
        account, _ = self.disabled_with_exception()
        batch = self.create()
        self.assertEqual(batch['ready_count'], 1)
        self.assert_denied_without_account_commit(account, batch)

    def test_admission_is_rechecked_when_managed_scope_changes_after_review(self):
        account, gate = self.disabled_with_exception(managed=False)
        batch = self.create()
        gate.is_managed.side_effect = lambda account_id: account_id == account.id
        self.assert_denied_without_account_commit(account, batch)

    def test_managed_ordinary_exception_can_still_be_enabled_by_import(self):
        account, _ = self.disabled_with_exception(segmented=False)
        before = self.store.get_account(account.id)
        result = self.wait(self.forms.confirm(self.create()['id']))
        after = self.store.get_account(account.id)
        self.assertEqual(result['rows'][0]['status'], 'done')
        self.assertTrue(after['plan']['enabled'])
        self.assertEqual(after['date_exceptions'], before['date_exceptions'])
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.assertEqual(fixture.ImportSchool.posts, [])

    def test_local_segmented_exception_can_still_be_enabled_by_import(self):
        account, _ = self.disabled_with_exception(managed=False)
        before = self.store.get_account(account.id)
        result = self.wait(self.forms.confirm(self.create()['id']))
        after = self.store.get_account(account.id)
        self.assertEqual(result['rows'][0]['status'], 'done')
        self.assertTrue(after['plan']['enabled'])
        self.assertEqual(after['date_exceptions'], before['date_exceptions'])
        self.assertEqual(fixture.ImportSchool.posts, [])
