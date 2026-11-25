# Explicit inactive-plan cancellation UI

The existing private local dashboard now offers a separate plan selection and
confirmation for POST /api/plans/cancel. Only queued/checkpointed/paused entries
from the latest successfully authenticated plan snapshot are candidates, excluding
visible running/unknown steps. This is a convenience filter, never authority or
proof cancellation is safe. The existing server transaction remains decisive.
The confirmation names plan ID, last-read status and completed/total counts,
explaining no running-process kill, no completed-result rollback and no resume.

One click after confirmation submits the exact last-read updated_at once. A global
in-flight fence disables double submission and selector refresh. Monitoring
requests already in flight are invalidated at submit and settlement, so an older
snapshot cannot silently repopulate candidates after the outcome. Token/epoch/selection/request fences
hide stale success/failure text. Token changes clear candidates and result. After
a response or unknown transport outcome, candidates are cleared and the operator
must reload state before another attempt. No automatic retry or cancellation is
triggered by monitoring, selection or reload. A submitted request may still commit
after locking/changing selection; UI privacy fences do not cancel server work.
The result is rendered as fixed text rather than private returned plan content.

This is an explicit destructive lifecycle action available to the trusted local
operator, not model authorization or public activation. Existing private binding,
Bearer/Host/Origin/Fetch-Site/rate limits and inactive-only transaction apply.
Current UI filtering does not recognize every hidden conflicting queue state;
refusals require reload and reconciliation, not override. Existing English machine
states and native confirmation styling remain part of the partial Arabic locale.
Tests execute the actual JavaScript with synthetic DOM/network replies; separate
actual HTTP tests cover the route. Chrome desktop and390CSS-pixel previews inspect
normal loaded synthetic plan state. No live user plan was cancelled by this work.
