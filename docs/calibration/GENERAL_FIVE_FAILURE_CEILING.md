# General plan fifth-failure checkpoint, candidate

## Grounding and unit

Original plan task4.3 requires fifth-failure checkpoint/wait, explicit-reply continuation, restart without replay of a completed effect, and cancel tests. Discovery21 historical design material describes attempts for the same request and resuming the same task, not a global counter across unrelated work. Its original source is https://chatgpt.com/c/6b008253-447c-83ea-ae4c-3340e757874b . The historical passage is design context, not fresh owner permission. The archived original browser read is recorded at2026-11-24 19:46:50/54 in the project-history research record. Live source access on2026-11-25 loaded its signed-in shell after one reload but did not expose the original virtualized turn; no new authorship or permission is inferred.

Implementation mapping: one persistent general PlanStore plan represents one request, with failures counted across its steps. This mapping is an engineering choice, not a quote of a per-plan user instruction. A separate unrelated plan does not inherit failures. A success on one step does not reset the request's count. Only a separate explicit continue decision resets the window. Existing journal rows are the evidence; legacy unrecorded history stays unknown rather than invented. If an upgraded plan already has more than five recorded failures, it blocks immediately and the decision records the exact observed watermark, not a fabricated fifth-failure time.

## No retry target or new engine

Five is a ceiling, never a target. Existing permission/input/storage/unknown failures pause immediately. The current operations are deterministic workspace_status, memory_recent, configured local_code_report. No new alternative route or worker identity is invented to reach five. No loop automatically retries a failed general step. Existing AdultSupervisor shows the invariant but its stores/private payloads are not bridged here.

Reuse: PlanStore, TaskQueue, task_attempts, targeted claims, persisted plan steps/results, and existing cancellation. A small plan_continuations table stores only decision UUID, plan ID, exact cumulative failure count, and time. Existing failed task/step/attempt rows preserve the checkpoint. No second payload or result copy is added. The count is recorded failures since the last explicit continuation, not consecutive failures reset by success.

## Continue and cancellation

get_failure_checkpoint reports count, last approved watermark, required flag and current updated_at. On five failures, retry_failed_step raises ContinuationRequired before any mutation. The run path also checks the ceiling before a new pending claim. The normal paused supervisor returns wait with the explicit checkpoint state. No sixth worker call can follow from simply invoking a supervisor again.

continue_after_decision requires an exact current plan updated_at, exact failure count, canonical decision UUID, paused state and no running plan/queue outcome. It commits only the decision and new updated_at. It never requeues or dispatches. Duplicate/stale/concurrent decisions fail, never increment or reset twice. The plan remains paused with failed work retained. A later retry and authorized run remain separate actions. A continuation never waives Step4.2's specific repair assertion for nonretryable failures, source scope, money, or any permission.

The local API and stdio MCP can record a trusted caller's decision, but cannot authenticate a human reply. The trusted conversation entry must obtain the owner's actual explicit reply and route only that decision. Synthetic test decisions prove software state, not owner consent. No public endpoint or real user continuation was added or performed.

Cancellation reuses cancel_plan with its current updated_at. It cancels only inactive remaining work, preserves success/result/attempt evidence, and blocks continuation and later run. Running/unknown outcomes remain unreplayed and require reconciliation. This is not process interruption or effect rollback.

## Recovery and evidence

Private backup/restore accepts exact three-table legacy, four-table journal, or five-table continuation schema without mutating the source. It validates decision identity, timestamp, plan association, ascending windows of at least five, and failure counts supported by journal evidence. Legacy history is not fabricated. No-init cancellation accepts all three schema versions without migration. Existing three-table row-count receipt shape stays stable.

Tests cover actual SQLite, production worker success, controlled transient faults, genuine production memory worker failures against deliberately corrupted synthetic metadata, individually explicit retries, fifth durable wait, reopen/backup/restore, no sixth call, exact explicit decision, stale/duplicate/concurrent refusal, nonretryable gate after continuation, second five-failure gate, unrelated-plan isolation, cancel at wait, retained completed first step and no completed replay. The MCP journey uses actual subprocess/production memory worker and a real metadata repair, not synthetic worker return values. Corrupt metadata and timeout injections are fault fixtures, not five useful real AI approaches or live-provider execution.

This candidate needs independent written-criterion review. No acceptance weight, publication, live AI success, real owner reply journey, automatic alternative selection, or whole Stage4 completion is claimed by its test count.
