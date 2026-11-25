# Deterministic task intake validation before persistence

The general queue and plan intake now share the same operation-specific payload
validator. workspace_status accepts only an empty object; memory_recent accepts
only an optional integer limit1..20, never a boolean. The existing configured
local_code_report schema is reused unchanged. None means the default empty
object for standalone enqueue; false, zero, empty text and arrays are no longer
silently replaced by an empty payload. Invalid input creates no task or attempt.

Standalone max_attempts must be an exact integer1..10, not a float, boolean or
numeric string. This does not authorize more retries, alter defaults, change
failed-step approvals or change the worker registry. Existing persisted rows
are not migrated or rewritten. Worker-side validation remains in place.

The shared validator runs before insert for standalone tasks and before plan
creation/binding. Actual MCP invalid-payload calls leave an empty queue, then a
normal status task succeeds. This is an internal contract correction, not full
Stage4.1 acceptance, source permission or model/tool activation.

The task MCP max_attempts parameter uses Pydantic StrictInt to prevent FastMCP
from coercing a boolean, float or numeric string before repository validation.
Its JSON schema remains integer; the runtime no longer accepts those coercions.
The actual stdio test rejects all four invalid values without queue inserts.
