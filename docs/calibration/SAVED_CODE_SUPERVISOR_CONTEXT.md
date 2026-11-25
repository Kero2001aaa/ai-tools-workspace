# Saved code evidence context for the existing supervisor

The trusted-local library function saved_code_context(database, plan_id, step_id)
reuses read_saved_report to validate an already completed plan/queue result. It
returns complete JSON at most4000characters, matching the existing supervisor
context input bound. It never invokes a model, executes a worker, rereads source,
writes state, adds a tool or grants access. Calling it is not permission to send
source excerpts to any model or other audience. The caller must separately have
that disclosure authority before using the context in an inference call.

One or two saved citations remain present, with equal character allocation when
needed. JSON escaping counts toward the cap. Full stored line ranges and quote
hashes are named as stored metadata; projected excerpts have their own hashes,
original lengths and explicit truncation flags. Excerpts can end mid-line. Empty
excerpts are possible when metadata leaves no text budget. If metadata alone
cannot fit, preparation fails closed. The original validated report is unchanged.
The question is bounded to500characters with its own truncation flag. Invalid
Unicode is refused even in text outside the retained prefix.

This is a bounded stored-snapshot handoff, not source authenticity, current code,
complete source context or model understanding. The supervisor's final reviewer
still sees specialist excerpts rather than independent full-source evidence.
Existing plaintext DB/backup retention and trusted-local caller limits remain.
Decoded source strings are untrusted; downstream displays must escape them.
Tests use actual saved results plus injected models only. No connected-provider
qualification or full Stage5 acceptance follows from this helper.

## Explicit CLI preparation

```
python -m agent.code_report_cli --db /private/plans.sqlite3 \
  --plan-id PLAN_UUID --step-id STEP_UUID --supervisor-context
```

The existing CLI defaults to the full saved report as before. This explicit flag
prepares bounded evidence only, without source reads, execution, persistence or
model invocation. It prints single-line ASCII-escaped JSON so terminal control
characters remain inert. The4000-character bound applies to the helper's logical
Unicode JSON, not this terminal transport: ASCII escapes may be longer. A caller
consuming terminal output must JSON-decode it and reserialize with ensure_ascii=False
and compact separators before handing it to the existing supervisor. The helper
is the direct in-process route. Do not blindly paste the longer escaped transport
into a4000-character field or slice its JSON. Displays still must escape decoded
source strings. No CLI option grants disclosure to a model or another audience.
