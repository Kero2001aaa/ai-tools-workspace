# Optional image-analysis prompt

`image.analyze` accepts one validated image plus an optional bounded text
prompt. This extends the Stage 8 analysis envelope, which was image-only; it
establishes no live provider, semantic moderation, supervision transport or
public endpoint.

## Contract

- The prompt is optional. A promptless analysis produces exactly the prior
  image-only canonical envelope bytes, so stored promptless envelopes and
  their fingerprints are unchanged.
- The prompt is validated by the same encoder as generate/edit prompts:
  strict UTF-8, non-empty after trimming, at most 4,000 characters and 16 KiB
  encoded, no prohibited control characters. Invalid content creates no task.
- The prompt is stored as its own task-scoped payload under the task's
  retention policy and is referenced from the canonical envelope as
  `prompt_payload_id`. The envelope decoder accepts exactly the two analysis
  shapes (with and without the prompt reference) and no other keys.
- The prompt is part of the deterministic canonical input, so the keyed
  content fingerprint binds image and prompt together before any task or
  payload identifier exists. A changed prompt is different content.
- Exact zero-attempt ephemeral restore is unchanged in shape: after a full
  loss the caller must re-upload the byte-identical image and prompt; after a
  partial loss only missing parts are restored under the surviving envelope's
  original references. A missing, changed or orphaned prompt is rejected
  before any queueing or dispatch, without echoing content.
- `ImageAnalysisRequest` carries the optional prompt reference to the
  understanding adapter. The pipeline validates stored prompt decodability
  before routing; an undecodable stored prompt is a bounded INVALID_INPUT
  rejection with no route selection and no attempt.
- The OmniRoute understanding adapter sends the prompt strictly as marked
  untrusted user-role data after the fixed adapter-owned instruction. It never
  enters system or developer instructions and never changes them. The fixed
  instruction to ignore text depicted inside the image is unchanged.
- Routing modalities for analysis stay `(image) -> text`; the optional prompt
  does not change capability queries or route selection.

## Boundaries

- Analysis prompts remain untrusted user data. Nothing here grants a prompt
  authority over policy, routing, budget or instructions.
- No new provider call pattern is introduced: the understanding adapter still
  makes exactly one bounded chat-completion request per attempt.
- Tests use real stores with deterministic offline adapters and mocked HTTP.
  They establish contract plumbing only, not AI inference quality, semantic
  safety or live free-route availability.
