# Strict specialist text output

The existing tool-less research, coding and review specialists now refuse
non-text model content instead of coercing arbitrary objects with str(). They
also reject invalid UTF-8 text before truncation, including malformed characters
after the retained prefix. Errors use specialist_response_invalid without
including raw model content. Valid Unicode and the existing 12000-character
truncation contract are unchanged.

This also covers the supervisor's final reviewer. Previously a list or object
could become apparently valid final analysis after coercion. The affected call
is attempted once and fails closed; no completed workflow or extra retry follows.
No provider, tool, task type, permission, persistence or budget is added. Structured
multimodal content is deliberately unsupported by this text-only contract, not
silently flattened into prose. Text validity is not semantic correctness or
source authenticity. Tests use injected responses only, not a live model.
