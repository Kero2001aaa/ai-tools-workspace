# Universal MCP Gateway and Restricted Terminal Agent

## Local gateway

The gateway exposes selected local MCP STDIO servers through one MCP
Streamable HTTP endpoint. The default bind is loopback-only:

```bash
.venv/bin/python -m agent.gateway_mcp_server
```

Default endpoint: `http://127.0.0.1:8765/mcp`

Configuration:

- `MCP_GATEWAY_HOST`: defaults to `127.0.0.1`; only localhost and loopback IPs are accepted.
- `MCP_GATEWAY_PORT`: defaults to `8765`.
- `ENABLE_TERMINAL_TOOLS`: defaults to `false`. Explicitly set to `true` to add the restricted terminal MCP server.
- `ENABLE_SPECIALIST_AGENTS`: defaults to `false`. Set to `true` to add the isolated specialist MCP server; calls may consume model-provider quota.
- `ENABLE_GATEWAY_PLANNER_ACTIONS`: defaults to `false`. Set to `true` to expose bounded plan creation and execution tools; plan steps remain restricted to approved read-only operations.

## Tool exposure

Names are prefixed with the upstream server name. The gateway uses an explicit
allowlist. Graphify, state-changing memory tools, task-queue mutation tools,
and `retry_failed_plan_step` remain excluded. Specialist tools are exposed only
when `ENABLE_SPECIALIST_AGENTS=true` and consist of `delegate_specialist` and
`supervise_request`; the latter runs a bounded workflow of up to four model calls
and may consume provider quota.

Planner reads (`planner__get_plan`, `planner__list_plans`) are available by
default. Set `ENABLE_GATEWAY_PLANNER_ACTIONS=true` to additionally expose
`planner__create_plan`, `planner__run_next_plan_step`,
`planner__run_plan_until_done`, and `planner__supervise_plan`. These actions
persist plans and execute only the planner's existing approved read-only step
types under explicit step/time budgets. Failed-step retry remains local-only.

The terminal server is separately opt-in and exposes three narrowly scoped tools:

- `terminal__run_workspace_task` — accepts exactly one task identifier from this fixed catalog:

| Task | Fixed operation |
|---|---|
| `run_tests` | `.venv Python -m unittest discover -s tests -v` using the active interpreter |
| `git_status` | `git status --short --branch` |
| `git_diff_check` | `git diff --check` |
| `git_log` | `git log -5 --oneline --decorate` |
| `python_version` | Active interpreter version |

The terminal tool does not accept shell text, arbitrary arguments, caller
selected working directories, or filesystem paths. It invokes subprocesses
with `shell=False`, a fixed repository root, a 120-second timeout, bounded
output, and a reduced environment that does not forward model/API credentials.
It does not provide a general-purpose shell, install packages, or perform Git mutations such as commit, push, reset, or checkout.

- `terminal__read_project_file` — reads UTF-8 files up to 50 KB from `agent/`, `tests/`, `docs/`, or a small approved set of root files.
- `terminal__write_project_file` — creates or replaces UTF-8 files up to 100 KB in those same approved locations. It rejects traversal, symlinks, hidden/protected directories, `.env`, and paths outside the repository. Writes are atomic and return a SHA-256 digest. Review the resulting diff and run tests before committing.

## Security boundary — important

The current gateway is a **local development service**, not a production
remote endpoint. Loopback binding is not authentication. The Streamable HTTP
gateway does not yet implement OAuth, bearer-token verification, user identity,
or remote-client authorization.

Do not expose port 8765 through a public Codespaces port, reverse proxy, tunnel,
or internet-facing host. Do not connect Claude or ChatGPT to it remotely.
Enabling terminal tools increases the impact of a compromised local gateway;
keep `ENABLE_TERMINAL_TOOLS=false` unless deliberately testing the restricted
local interface. Specialist and planner-action options are also disabled by
default; enable only the capabilities required for a specific local workflow.

## Supported remote connection paths

### OpenAI / ChatGPT

OpenAI documents Secure MCP Tunnel as an outbound-only way to connect a
private MCP server to supported OpenAI products without opening inbound ports
or making the server public. The tunnel client runs beside the MCP server,
polls the OpenAI-hosted endpoint, and forwards requests locally.

Official guide:
https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

This repository does not yet configure or run `tunnel-client`. Setup requires
an OpenAI Platform tunnel ID, a runtime API key, the correct Platform
organization and ChatGPT workspace association, and tunnel permissions.
ChatGPT custom MCP availability is also controlled by the target account or
workspace plan and permissions. Verify eligibility in the current ChatGPT
workspace before spending time on tunnel setup.

### Claude

Claude custom connectors use remote MCP. Claude's cloud service must be able
to reach the MCP endpoint over public HTTPS (port 443). Claude documents OAuth
and fixed request-header authentication options. A local loopback server by
itself is not reachable from Claude's cloud service.

Official guide:
https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp

A separate public HTTPS deployment or a compatible secure relay is therefore
still required for Claude. Do not expose the current unauthenticated gateway
directly.

## What remains before remote ChatGPT/Claude control

A secure remote integration still requires all of the following:

1. An authenticated remote MCP authorization layer compatible with each client
   (including the client's current OAuth requirements).
2. HTTPS deployment with TLS termination, secret rotation, rate limits,
   request auditing, and a narrowly scoped identity.
3. A persistent, outbound-authenticated Codespace agent or another trusted
   execution host. Codespaces may stop or suspend; execution cannot be assumed
   to remain online.
4. End-to-end tests against the actual ChatGPT and Claude connector flows.

These are not completed by the local terminal MCP server. No public endpoint,
OAuth client, deployment, or remote connection is created by this change.
