# Universal MCP Gateway — Phase 21

## Purpose

Expose a deliberately small subset of the existing local MCP STDIO servers
through one MCP Streamable HTTP endpoint. The existing STDIO transport and
server implementations remain unchanged.

## Run locally

```bash
.venv/bin/python -m agent.gateway_mcp_server
```

Default endpoint:

`http://127.0.0.1:8765/mcp`

Configuration:

- `MCP_GATEWAY_HOST`: defaults to `127.0.0.1`; only `localhost` and loopback IPs are accepted.
- `MCP_GATEWAY_PORT`: defaults to `8765`.

## Exposed tools

Names are prefixed with the upstream server name:

- `workspace__text_stats`
- `workspace__workspace_status`
- `memory__recall`
- `memory__recent_memories`
- `research__web_search`
- `research__scrape_url`
- `research__research_provider_status`
- `tasks__list_tasks`
- `planner__get_plan`
- `planner__list_plans`
- `browser__browser_inspect` (only when browser tools are enabled)

The gateway uses an explicit allowlist. State-changing memory, task, and
planner tools are excluded. Graphify is excluded pending a separate review
of its project-path access.

## Security boundary

This phase is local-only. The server rejects non-loopback bind addresses.
It does not implement authentication, OAuth, user identity, or remote-client
authorization. Loopback binding is a development safeguard, not a substitute
for authentication.

Do not expose this endpoint through a public Codespaces port, reverse proxy,
tunnel, or internet-facing host. Do not configure Claude or ChatGPT to connect
to it remotely yet.

## Roadmap

1. Phase 22 — authentication and permissions.
2. Phase 23 — remote HTTPS deployment and operational hardening.
3. Phase 24 — Claude integration.
4. Phase 25 — ChatGPT integration.
5. Phase 26 — cross-platform integration tests.

Remote compatibility must be verified against each client's current MCP
transport and authentication requirements before claiming support.
