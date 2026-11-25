"""Real duplicate audit reads, no invented attempts or provider work."""
import unittest
from tests import test_adult_attempt_tracker as fixtures
from agent.adult.adult_response_contracts import validate_response
from agent.adult.adult_attempt_tracker import DuplicateApproach,AdultAttemptTracker,AttemptValidationError
from agent.adult.adult_task_store import AdultTaskStore


class DuplicateSkipHistoryTests(unittest.TestCase):
    setUp=fixtures.AdultAttemptTrackerTests.setUp
    tearDown=fixtures.AdultAttemptTrackerTests.tearDown
    begin=fixtures.AdultAttemptTrackerTests.begin
    query=fixtures.AdultAttemptTrackerTests.query

    def duplicate(self):
        handle=self.begin()
        self.tracker.fail(handle,'PROVIDER_ERROR','Temporary provider failure.',True)
        for _ in range(3):
            with self.assertRaises(DuplicateApproach):self.begin()
        return handle

    def test_restart_preserves_skips_separate_from_one_actual_attempt(self):
        handle=self.duplicate()
        restarted=AdultAttemptTracker(AdultTaskStore(self.db))
        rows=restarted.list_skipped(self.task.task_id)
        self.assertEqual(3,len(rows))
        self.assertEqual(1,restarted.total_attempts(self.task.task_id))
        self.assertEqual(1,restarted.failures_since_approval(self.task.task_id))
        for row in rows:
            self.assertEqual(handle.attempt_id,row['attempt_id'])
            self.assertEqual(1,row['attempt_no']);self.assertTrue(row['skipped_as_duplicate'])
        validate_response('history',{'attempts':[],'skipped':list(rows)})
        self.assertNotIn('approach_key',str(rows));self.assertNotIn('fingerprint',str(rows))
        self.assertNotIn('Temporary provider',str(rows));self.assertNotIn('metadata',str(rows))

    def test_bounds_task_scope_read_only_and_order(self):
        self.duplicate();before=self.db.read_bytes()
        rows=self.tracker.list_skipped(self.task.task_id,2)
        self.assertEqual(2,len(rows));self.assertEqual(sorted(r['created_at'] for r in rows),[r['created_at'] for r in rows])
        self.assertEqual((),self.tracker.list_skipped('f'*32))
        for limit in (True,1.0,'1',0,21):
            with self.assertRaises(AttemptValidationError):self.tracker.list_skipped(self.task.task_id,limit)
        self.assertEqual(before,self.db.read_bytes())

    def test_corrupt_event_metadata_never_returned(self):
        self.duplicate()
        db=self.store._connect()
        import sqlite3
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE adult_attempt_events SET metadata_json=? WHERE event_type='approach_skipped'",('{"raw":"private-marker"}',))
        finally:db.close()
        rows=self.tracker.list_skipped(self.task.task_id)
        self.assertNotIn('private-marker',str(rows))
        validate_response('history',{'attempts':[],'skipped':list(rows)})

    def test_combined_app_history_uses_real_task_and_strict_envelope(self):
        from tests import test_adult_agent as combined
        helper=combined.AdultAgentTests();helper.setUp();self.addCleanup(helper.doCleanups)
        app=helper.agent();helper.enable_text()
        task=app.submit_text_task('text.analyze','private source')['task_id'];app.run_task(task)
        history=app.get_attempt_history(task)
        validate_response('history',history)
        self.assertEqual(1,len(history['attempts']));self.assertEqual([],history['skipped'])
        self.assertEqual(app.get_attempts(task),history['attempts'])
        with self.assertRaises(ValueError):app.get_attempt_history(task,21)
        with self.assertRaises(ValueError):app.get_attempt_history('missing-task')
