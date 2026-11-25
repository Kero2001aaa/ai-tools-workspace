# Atomic image dispatch checks

The image pipeline accepts the caller's optional fenced lease and passes it to
attempt commitment. A lease supplied by an executor is checked by the existing
tracker in the same transaction that reserves the attempt. Direct pipeline
callers without the keyword retain their existing behavior; this is not a new
public execution endpoint.

ModelCapabilityRegistry.current_image_route_in_transaction is an opt-in tracker
hook. A trusted composition must wire it as AdultAttemptTracker's
route_validator. It rechecks the exact image route's enabled/verified state,
capability version, adapter/model identity, task modalities, output constraints,
and unexpired capability and separate zero-cost evidence before an attempt can
commit. Input dimensions were checked during route selection and bound by the
unchanged capability version. It cannot revoke a call already dispatched.

The checks do not establish provider availability, certify an image adapter,
permit paid fallback, add retries, or close supervision/terminal cleanup.
Tests use real stores and deterministic offline adapters only.
