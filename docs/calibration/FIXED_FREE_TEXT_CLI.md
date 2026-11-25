# Fixed-free operator CLI

Run explicitly with `python -m agent.adult.fixed_free_text_cli --state-dir DIR COMMAND`.
This is an operator entry point, not a registered MCP tool, gateway endpoint,
background runner or proof that the live model is available. Use a dedicated
state directory. Do not share it with the deterministic local statistics demo.

Configure `ADULT_PAYLOAD_ENCRYPTION_KEY`, `ADULT_OMNIROUTE_BASE_URL` (exactly
`https://openrouter.ai/api/v1`) and `ADULT_OMNIROUTE_API_KEY` securely in the
operator environment. No key, endpoint or provider management tool is exposed.
Workspace `MODEL` variables do not select this route.

Commands:

- `stage --task-type text.analyze --provider-call-budget 1`: bounded UTF-8 JSON
  stdin with `text` and optional `instruction`. The other supported task types
  are `text.generate` and `text.rewrite`. Stores encrypted input and yields a
  task ID. It makes zero provider calls. Unknown JSON fields are rejected.
- `status TASK_ID`: read-only metadata without keys, input or result text.
- `run TASK_ID`: one explicit bounded service segment, at most one provider
  attempt. It requires existing preverified and enabled fixed-free route
  evidence. Missing evidence pauses with zero attempts. It never discovers,
  registers, verifies or enables routes. Do not use repeated runs as an
  availability probe. A no-route pause is not a successful AI response.
- `result TASK_ID [--offset N] [--max-bytes N]`: explicitly reads a completed
  encrypted output artifact as a bounded base64 chunk, including MIME type,
  total size, next offset and completion flag. Default chunk limit is 262144
  bytes. This command intentionally discloses result bytes to its caller,
  unlike metadata-only status. It never returns original source input.
- `cancel TASK_ID`: existing cancellation transaction, no keys or provider call.

Errors are fixed and redacted. Missing state reads create no replacement
state. Wrong keys and invalid endpoints are refused before task mutation.
The original 600-second task wall deadline begins at staging, not at run or
restart. A completed, failed or cancelled task does not cause another attempt.
No CLI command certifies availability or changes the current unavailable-provider
state. Do not schedule provider retries without a new grounded availability
signal. Only free verified routes are accepted; privacy settings are unchanged.

Validation uses real separate processes for stage/status/run/cancel and no-route
pause, and a patched fake client for an encrypted completed artifact chunk.
No live inference was performed. The prior local counting journey remains a
separate deterministic demo and is not relabeled as AI.

The CLI now delegates staging and running through FixedFreeTextApplication,
including its explicit encrypted-retention choice. CLI output field names stay
compatible. Application and CLI share one task-owned artifact-chunk serializer;
reading a completed result still requires only its encryption key, not provider
credentials or availability. Keyless status/cancel paths remain unchanged. The
CLI does not inject the optional feedback bridge, so it never creates or publishes
feedback implicitly. No new entry point, model request or transport is added.
