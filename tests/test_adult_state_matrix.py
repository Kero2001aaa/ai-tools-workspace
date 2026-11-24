"""Exhaustive Stage 3 state-pair evidence, with dedicated API routing.

Only five-failure counters are seeded as synthetic preconditions. Every source
state is reached using public APIs. No provider dispatch or payload is involved.
"""
import tempfile
import unittest
from pathlib import Path
import tests.test_adult_task_store as helpers
from agent.adult import adult_task_store as storage
from agent.adult.models import AdultTaskRequest, TaskBudget

GRAPH = {
    'queued': {'analyzing', 'cancelled', 'failed'},
    'analyzing': {'running', 'checkpointed', 'waiting_user', 'needs_input', 'paused', 'failed', 'cancelled'},
    'running': {'evaluating', 'checkpointed', 'waiting_user', 'needs_input', 'paused', 'failed', 'cancelled'},
    'evaluating': {'completed', 'running', 'checkpointed', 'paused', 'failed', 'cancelled'},
    'checkpointed': {'queued', 'analyzing', 'cancelled', 'paused', 'needs_input'},
    'waiting_user': {'queued', 'cancelled'}, 'needs_input': {'queued', 'cancelled'},
    'paused': {'queued', 'cancelled', 'failed'},
    'completed': set(), 'failed': set(), 'cancelled': set(),
}
FUTURE_ONLY = {('needs_input', 'queued'), ('paused', 'queued')}
ACTIVE = {'analyzing', 'running', 'evaluating'}


class StateMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = storage.AdultTaskStore(Path(self.temp.name) / 'synthetic.sqlite3')
        self.task = self.store.create_task(
            AdultTaskRequest('text.analyze', 'hmac-sha256:0123456789abcdef'), TaskBudget())
        self.lease = None

    def current(self):
        return self.store.get_task(self.task.task_id)

    def generic(self, destination):
        t = self.current()
        return self.store.transition(t.task_id, {t.status}, destination,
                                     'state_transition', t.state_version,
                                     lease=self.lease if t.status in ACTIVE else None)

    def waiting(self):
        t = self.current()
        snapshot = helpers.AdultTaskStoreSchemaTests._checkpoint_snapshot(
            t, status='waiting_user', total_attempts=5,
            failed_attempts_since_approval=5, pending_user_action='approval_required',
            allowed_decisions=('continue', 'cancel'))
        return self.store.set_waiting_user(t.task_id, snapshot, self.lease, t.state_version)

    def five_failures(self):
        # Stage 3 owns persistence, not attempt execution. Seed only documented
        # counters; never a status/lease/checkpoint row or bypassed transition.
        with self.store._connect() as db:
            db.execute('UPDATE adult_tasks SET total_attempts=5,failed_attempts_since_approval=5 WHERE task_id=?',
                       (self.task.task_id,))

    def reach(self, status):
        if status == 'queued':
            return
        if status in {'failed', 'cancelled'}:
            self.generic(status)
            return
        self.lease = self.store.claim_task(self.task.task_id, 'matrix-worker', lease_seconds=300)
        if status == 'analyzing':
            return
        if status in {'running', 'evaluating', 'completed'}:
            self.generic('running')
            if status != 'running':
                self.generic('evaluating')
            if status == 'completed':
                self.generic('completed')
        elif status == 'waiting_user':
            self.five_failures()
            self.waiting()
        else:
            self.generic(status)
        self.assertEqual(status, self.current().status)

    def dump(self):
        with self.store._connect() as db:
            return tuple(db.iterdump())

    def assert_atomic_rejection(self, operation, errors):
        before = self.dump()
        with self.assertRaises(errors):
            operation()
        self.assertEqual(before, self.dump())

    def assert_arrival(self, source, destination, operation):
        old = self.current()
        with self.store._connect() as db:
            count = db.execute('SELECT COUNT(*) FROM adult_task_events').fetchone()[0]
        operation()
        new = self.current()
        self.assertEqual(destination, new.status)
        self.assertEqual(old.state_version + 1, new.state_version)
        with self.store._connect() as db:
            self.assertEqual(count + 1, db.execute('SELECT COUNT(*) FROM adult_task_events').fetchone()[0])
            event = db.execute('SELECT from_status,to_status FROM adult_task_events ORDER BY rowid DESC LIMIT 1').fetchone()
            self.assertEqual((source, destination), tuple(event))
            row = db.execute('SELECT lease_owner,lease_expires_at,user_action_required,cancel_requested FROM adult_tasks').fetchone()
            self.assertEqual(destination in ACTIVE, row['lease_owner'] is not None)
            self.assertEqual(destination in ACTIVE, row['lease_expires_at'] is not None)
            self.assertEqual(destination in {'waiting_user', 'needs_input', 'paused'}, bool(row['user_action_required']))
            self.assertEqual(destination == 'cancelled' or (source == destination and source in ACTIVE), bool(row['cancel_requested']))

    def test_graph_contract_has_35_allowed_86_disallowed_pairs(self):
        self.assertEqual(GRAPH, storage._ALLOWED_TRANSITIONS)
        self.assertEqual(35, sum(map(len, GRAPH.values())))
        self.assertEqual(86, len(GRAPH) ** 2 - sum(map(len, GRAPH.values())))


def pair_test(source, destination):
    def test(self):
        self.reach(source)
        allowed = destination in GRAPH[source]
        if allowed and destination == 'waiting_user':
            self.five_failures()
        if not allowed:
            errors = storage.TaskTerminal if source in {'completed', 'failed', 'cancelled'} else (
                storage.UserApprovalRequired if destination == 'waiting_user' else storage.InvalidTaskTransition)
            self.assert_atomic_rejection(lambda: self.generic(destination), errors)
        elif (source, destination) in FUTURE_ONLY:
            self.assert_atomic_rejection(lambda: self.store.resume(self.task.task_id, 'continue'),
                                         (storage.UserApprovalRequired, storage.InvalidTaskTransition))
            self.assert_atomic_rejection(lambda: self.generic(destination), storage.UserApprovalRequired)
        else:
            if destination == 'analyzing' and source not in ACTIVE:
                action = lambda: self.store.claim_task(self.task.task_id, 'next-worker', lease_seconds=300)
            elif destination == 'queued':
                action = lambda: self.store.resume(self.task.task_id, 'continue')
            elif destination == 'waiting_user':
                action = self.waiting
            elif destination == 'cancelled' and source not in ACTIVE:
                action = lambda: self.store.request_cancel(self.task.task_id)
            elif destination == 'checkpointed' and source in ACTIVE:
                action = lambda: self.store.release_lease(self.lease, self.current().state_version,
                                                          next_status='checkpointed', reason='yielded')
            else:
                action = lambda: self.generic(destination)
            self.assert_arrival(source, destination, action)
    return test


def generic_test(source, destination):
    def test(self):
        self.reach(source)
        if source in {'completed', 'failed', 'cancelled'}:
            error = storage.TaskTerminal
        elif source in {'checkpointed', 'waiting_user', 'needs_input', 'paused'} and destination == 'queued':
            error = storage.UserApprovalRequired
        elif destination == 'waiting_user':
            error = storage.UserApprovalRequired
        elif destination not in GRAPH[source]:
            error = storage.InvalidTaskTransition
        elif destination in ACTIVE and source not in ACTIVE:
            error = storage.LeaseNotAvailable
        else:
            error = None
        if error:
            self.assert_atomic_rejection(lambda: self.generic(destination), error)
        else:
            self.assert_arrival(source, destination, lambda: self.generic(destination))
    return test


def waiting_test(source):
    def test(self):
        self.reach(source)
        self.five_failures()
        if source == 'evaluating':
            self.assert_atomic_rejection(self.waiting, storage.InvalidTaskTransition)
        else:
            self.assert_arrival(source, 'waiting_user', self.waiting)
    return test


for _source in GRAPH:
    for _destination in GRAPH:
        setattr(StateMatrixTests, f'test_pair_{_source}_to_{_destination}', pair_test(_source, _destination))
        setattr(StateMatrixTests, f'test_generic_{_source}_to_{_destination}', generic_test(_source, _destination))
for _source in sorted(ACTIVE):
    setattr(StateMatrixTests, f'test_dedicated_waiting_{_source}', waiting_test(_source))


def release_test(source, destination):
    def test(self):
        self.reach(source)
        action = lambda: self.store.release_lease(
            self.lease, self.current().state_version, next_status=destination,
            reason='cancel_observed' if destination == 'cancelled' else 'yielded')
        if destination in GRAPH[source] and destination not in ACTIVE and destination != 'waiting_user':
            self.assert_arrival(source, destination, action)
        elif destination == 'waiting_user' and destination in GRAPH[source]:
            # release is not the approval API, even with a graph-allowed edge.
            self.assert_atomic_rejection(action, storage.UserApprovalRequired)
        else:
            self.assert_atomic_rejection(action, storage.InvalidTaskTransition)
    return test


def claim_test(source):
    def test(self):
        self.reach(source)
        action = lambda: self.store.claim_task(self.task.task_id, 'claim-worker', lease_seconds=300)
        if source in {'queued', 'checkpointed'}:
            self.assert_arrival(source, 'analyzing', action)
        else:
            error = storage.TaskTerminal if source in {'completed', 'failed', 'cancelled'} else storage.LeaseNotAvailable
            self.assert_atomic_rejection(action, error)
    return test


def resume_test(source):
    def test(self):
        self.reach(source)
        action = lambda: self.store.resume(self.task.task_id, 'continue')
        if source in {'checkpointed', 'waiting_user'}:
            self.assert_arrival(source, 'queued', action)
        else:
            error = storage.TaskTerminal if source in {'completed', 'failed', 'cancelled'} else (
                storage.UserApprovalRequired if source == 'needs_input' else storage.InvalidTaskTransition)
            self.assert_atomic_rejection(action, error)
    return test


def cancel_test(source):
    def test(self):
        self.reach(source)
        action = lambda: self.store.request_cancel(self.task.task_id)
        if source in {'completed', 'failed'}:
            self.assert_atomic_rejection(action, storage.TaskTerminal)
        elif source == 'cancelled':
            before = self.dump()
            action()
            self.assertEqual(before, self.dump())
        elif source in ACTIVE:
            self.assert_arrival(source, source, action)
        else:
            self.assert_arrival(source, 'cancelled', action)
        if source in ACTIVE:
            self.assertTrue(self.current().cancel_requested)
            self.assert_arrival(source, 'cancelled', lambda: self.store.release_lease(
                self.lease, self.current().state_version, next_status='cancelled', reason='cancel_observed'))
    return test


def recovery_test(source, cancelled):
    def test(self):
        self.reach(source)
        if cancelled:
            self.store.request_cancel(self.task.task_id)
        with self.store._connect() as db:
            db.execute("UPDATE adult_tasks SET lease_expires_at='2000-01-01T00:00:00.000000Z' WHERE task_id=?",
                       (self.task.task_id,))
        self.assert_arrival(source, 'cancelled' if cancelled else 'checkpointed',
                            lambda: self.assertEqual(1, self.store.recover_expired_leases()))
        with self.store._connect() as db:
            generation = db.execute('SELECT lease_generation FROM adult_tasks').fetchone()[0]
        self.assertEqual(self.lease.lease_generation + 1, generation)
    return test


for _source in ACTIVE:
    for _destination in GRAPH:
        setattr(StateMatrixTests, f'test_release_{_source}_to_{_destination}', release_test(_source, _destination))
    for _cancelled in (False, True):
        setattr(StateMatrixTests, f'test_recover_{_source}_cancel_{_cancelled}', recovery_test(_source, _cancelled))
for _source in GRAPH:
    setattr(StateMatrixTests, f'test_claim_{_source}', claim_test(_source))
    setattr(StateMatrixTests, f'test_resume_{_source}', resume_test(_source))
    setattr(StateMatrixTests, f'test_cancel_{_source}', cancel_test(_source))


def exhausted_test(source, api):
    def test(self):
        self.reach(source)
        if api == 'waiting':
            self.five_failures()
        with self.store._connect() as db:
            db.execute("UPDATE adult_tasks SET deadline_at='2000-01-01T00:00:00.000000Z' WHERE task_id=?",
                       (self.task.task_id,))
        if api == 'claim':
            action = lambda: self.store.claim_task(self.task.task_id, 'budget-worker')
        elif api == 'resume':
            action = lambda: self.store.resume(self.task.task_id, 'continue')
        else:
            action = self.waiting
        if api == 'resume':
            self.assert_arrival(source, 'failed', action)
        else:
            def exhausted():
                with self.assertRaises(storage.TaskBudgetExhausted):
                    action()
            self.assert_arrival(source, 'failed', exhausted)
    return test


for _source, _api in [('queued', 'claim'), ('checkpointed', 'claim'),
                      ('checkpointed', 'resume'), ('waiting_user', 'resume'),
                      ('analyzing', 'waiting'), ('running', 'waiting')]:
    setattr(StateMatrixTests, f'test_exhausted_{_source}_{_api}', exhausted_test(_source, _api))
