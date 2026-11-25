# Bounded duplicate skip history

The tracker already persists duplicate-approach audit events. New `list_skipped`
reads at most 20 recent records for one task, returns chronological order, and
joins each event to the original actual attempt. It never interprets/returns
arbitrary event metadata, approach keys, fingerprints, error summaries, retry
reasons or content. The original attempt's ID/number/provider/model/tool and
current terminal status are returned with a separate skip event ID/time/flag.
A skip is not a new attempt and never increments counters.

Text/image/combined internal applications expose `get_attempt_history` as a
strict envelope with separate bounded attempts and skipped lists. Legacy
`get_attempts` remains a list with unchanged meaning. Output models reject
extra fields and duplicate event IDs. No additional public tool is registered.
This is existing event evidence made readable, not synthetic work, retries,
real owner approval, semantic policy, provider availability or public release.
