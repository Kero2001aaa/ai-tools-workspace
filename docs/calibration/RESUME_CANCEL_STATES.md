# Explicit resume cancellation from blocked states

The existing `resume_task(..., decision='cancel')` now accepts paused and
needs_input tasks, as the draft section8.7 specifies. It uses the same atomic
request_cancel and terminal-input cleanup path. No input reupload, route,
attempt or approval increment is needed. waiting/checkpointed behavior stays.
Terminal tasks still cannot be resumed. Invalid bounds/decisions/replacements
are refused before state changes.

Continue remains only waiting/checkpointed, with the existing exact typed
zero-attempt ephemeral restore methods for needs_input. Generic changed-input
replacement is still refused: the draft replacement fields do not preserve all
original instruction/image-analysis prompt context. This correction is not a
public endpoint or real owner-consent implementation.
