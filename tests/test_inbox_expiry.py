"""Expiry must terminate untouched edits without discarding uncertain applied ones."""
import unittest
import json
import uuid
import test_inbox_gateway as gateway


class InboxExpiryTests(unittest.TestCase):
    setUp = gateway.InboxStoreTests.setUp
    tearDown = gateway.InboxStoreTests.tearDown
    enqueue = gateway.InboxStoreTests.enqueue

    def broken_snapshot(self):
        packet = gateway.snapshot(2)
        packet['accounts'][0].update(sync_error='fixture snapshot unavailable', auth=None)
        self.store.publish(packet)

    def test_unattempted_expired_edit_is_terminal_despite_snapshot_error(self):
        receipt = self.enqueue()
        self.now += 2*86400+1
        self.broken_snapshot()
        self.assertEqual(self.store.poll(), [])
        result = self.store.receipt(self.user['student_id'], receipt['request_id'])
        self.assertEqual((result['state'], result['code']), ('failed', 'inbox_expired'))
        self.assertEqual(self.store.summary()['pending'], 0)
        self.store.publish(gateway.snapshot(3))
        token = self.store.issue(self.store.authenticate(self.user['student_id'], 'fixture-password', 'fixture'))
        next_receipt = self.store.enqueue(self.user['student_id'],
            {'request_id': 'd12db2c4-ef5a-4895-aa60-c8888e4d607f', 'expected_revision': 3,
             'plan': {'enabled': False}}, token)
        self.assertEqual(next_receipt['state'], 'pending')

    def test_unexpired_snapshot_error_still_waits_without_consuming_an_attempt(self):
        receipt = self.enqueue(); self.broken_snapshot()
        self.assertEqual(self.store.poll(), [])
        result = self.store.receipt(self.user['student_id'], receipt['request_id'])
        self.assertEqual(result['state'], 'pending')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT attempts FROM queue').fetchone()[0], 0)

    def test_attempted_expired_edit_keeps_reconciliation_and_original_ack(self):
        receipt = self.enqueue(); first = self.store.poll()[0]
        self.now += 2*86400+1; self.broken_snapshot()
        recovered = self.store.poll()[0]
        self.assertEqual(recovered['request_id'], first['request_id'])
        self.assertTrue(recovered['reconcile_only'])
        self.store.ack(recovered['request_id'], recovered['lease'], {'state': 'applied', 'revision': 4})
        self.assertEqual(self.store.receipt(self.user['student_id'], receipt['request_id'])['state'], 'applied')

    def test_backed_off_snapshot_errors_do_not_starve_the_next_healthy_account(self):
        receipt = self.enqueue()
        with self.store.db() as db:
            for index in range(100):
                student = str(2320000000+index)
                row = gateway.snapshot()['accounts'][0] | {'student_id': student, 'sync_error': 'fixture', 'auth': None}
                db.execute('INSERT INTO mirror VALUES (?,?)', (student, json.dumps(row)))
                db.execute("INSERT INTO queue (id,student,version,digest,body,generation,state,created,expires) "
                           "VALUES (?,?,1,'fixture',?,'fixture','pending',?,?)",
                           (str(uuid.UUID(int=index+1)), student,
                            json.dumps({'expected_revision': 3, 'plan': {'enabled': False}}),
                            self.now-1, self.now+86400))
        self.assertEqual(self.store.poll(), [])
        self.now += .5
        jobs = self.store.poll()
        self.assertEqual([job['request_id'] for job in jobs], [receipt['request_id']])


if __name__ == '__main__': unittest.main()
