# Offline tool-contract candidate

This module is intentionally NOT imported by production model loading or transports. Production guard still denies tools/plugins and image parts. No environment opt-in exists. It is a staging contract, not a live-provider success or completion claim.

Official evidence inspected24November2026:
- https://openrouter.ai/openrouter/free states zero prompt/completion price, feature-aware routing for tool calling and image understanding, text/image input and text output.
- https://openrouter.ai/docs/guides/routing/provider-selection documents require_parameters support and data_collection deny, with max_price zero routing.

Candidate pins exact caller-supplied schemas for workspace_status and recent_memories only; unknown schema names,changed schemas,parallel calls,unmatched/duplicate call IDs,nonobject arguments,unknown fields,paid model/endpoint overrides and privacy changes fail closed. Existing production validator owns common endpoint/model/output/price policy. The SDK-to-mock serial roundtrip and actual LangGraph graph both pass offline. The graph invokes the existing read-only workspace_status implementation exactly once and returns its result through a second SDK request. Provider HTTP is mocked; no external model call occurs.

Not yet certified: actual provider compatibility/availability, remote tool schema support under private free routing, image understanding, image generation/editing, general JSON-schema support, tool response content security beyond existing graph instructions, or a general tool-execution authorization layer. A caller-supplied proposal validator can now run inside the actual graph immediately after model response and before ToolNode; candidate tests prove bad arguments and unknown/parallel proposals cause zero tool executions. Production default remains unchanged and the model HTTP transport still denies tools. Merely pinning a tool name does not authorize arbitrary execution. The runtime stays closed until these boundaries are checked. Earlier429remains unavailable inference and no further live probe was made.
