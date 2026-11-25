"""Narrow operational-store transaction for exact zero-attempt input restore.

Separated from the large core store module; uses its existing transaction,
validation/error contracts and readback, without a new schema or state owner.
"""
import re
import uuid
from agent.adult.models import AdultModelValidationError
from agent.adult.adult_task_store import (AdultTaskStore, TaskNotFound,
    StaleTaskVersion, InvalidTaskTransition, TaskBudgetExhausted,
    TaskStoreSchemaError, _utc_now)

def resume_restored_ephemeral(tasks, task_id: str, payload_id: str,
                              input_fingerprint: str, expected_state_version: int):
    """Queue exact zero-attempt input restored by the trusted application.

    No reference, fingerprint, budget, deadline or approval counter changes.
    This does not authorize a new approach or a changed input envelope.
    """
    if not isinstance(tasks, AdultTaskStore):
        raise TypeError('task_store_required')
    if (not isinstance(task_id, str) or not task_id or len(task_id) > 128
            or not isinstance(payload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", payload_id)
            or not isinstance(input_fingerprint, str)
            or not isinstance(expected_state_version, int)
            or isinstance(expected_state_version, bool) or expected_state_version < 0):
        raise AdultModelValidationError("Invalid transient restore reference.")
    now = _utc_now()
    with tasks._transaction() as db:
        task = db.execute("SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise TaskNotFound("Task does not exist.")
        if task["state_version"] != expected_state_version:
            raise StaleTaskVersion("Task changed before input restore.")
        if (task["status"] != "needs_input" or task["retention_policy"] != "ephemeral"
                or task["total_attempts"] != 0 or task["provider_calls"] != 0
                or task["cancel_requested"] or task["payload_id"] != payload_id
                or task["input_fingerprint"] != input_fingerprint):
            raise InvalidTaskTransition("Task cannot accept restored transient input.")
        if task["deadline_at"] <= now or task["max_total_attempts"] < 1:
            raise TaskBudgetExhausted("No execution budget remains.")
        if db.execute("SELECT 1 FROM adult_attempts WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
            raise InvalidTaskTransition("Prior attempts prevent transient restore.")
        db.execute("UPDATE adult_tasks SET status='queued',user_action_required=0,"
                   "state_version=state_version+1,updated_at=? WHERE task_id=?", (now, task_id))
        db.execute("INSERT INTO adult_task_events "
                   "(event_id,task_id,event_type,from_status,to_status,metadata_json,created_at) "
                   "VALUES (?,?,'task_resumed','needs_input','queued',?,?)",
                   (str(uuid.uuid4()), task_id, '{"reason_code":"exact_ephemeral_input_restored"}', now))
    result = tasks.get_task(task_id)
    if result is None:
        raise TaskStoreSchemaError("Restored task could not be reloaded.")
    return result
