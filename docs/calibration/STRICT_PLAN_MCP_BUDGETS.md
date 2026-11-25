# Preserve strict execution budgets at the MCP boundary

run_plan_until_done and supervise_plan now annotate max_steps and max_seconds as
Pydantic StrictInt. The existing store already rejects bool and non-integer
budgets and enforces step1..10/time1..300. FastMCP's prior ordinary int annotation
could convert true,1.0 or numeric strings before those checks, allowing execution
for a value the store itself would refuse. These four boundary annotations now
preserve the caller's type for validation. Tool input JSON schemas are unchanged.

No additional execution, retries, tools, permissions or model/provider calls are
introduced. Integer defaults and normal bounded checkpoint/resume are unchanged.
The time budget remains an elapsed dispatch boundary, not interruption of an
already running step. This narrow change covers execution budgets only, not all
numeric fields across unrelated servers or planner Unicode behavior.

Actual stdio tests reject bool/whole float/string/null/range violations for both
fields on both tools, with exact unchanged plan snapshots and no queue rows after
every refusal. A valid integer run checkpoints and supervision completes the
same plan, with one attempt per step. Previous and current FastMCP schemas are
compared in separate processes, not inferred from the Python annotation alone.
