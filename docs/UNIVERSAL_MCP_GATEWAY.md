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

## Tool exposure

Names are prefixed with the upstream server name. The gateway uses an explicit
allowlist. Graphify and all state-changing memory, task-queue, and planner
tools remain excluded.

The terminal server is separately opt-in and exposes only
`terminal__run_workspace_task`. It accepts exactly one task identifier from
this fixed catalog:

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
It does not provide a general-purpose shell, write files, install packages,
or perform Git mutations such as commit, push, reset, or checkout.

## Security boundary — important

The current gateway is a **local development service**, not a production
remote endpoint. Loopback binding is not authentication. The Streamable HTTP
gateway does not yet implement OAuth, bearer-token verification, user identity,
or remote-client authorization.

Do not expose port 8765 through a public Codespaces port, reverse proxy, tunnel,
or internet-facing host. Do not connect Claude or ChatGPT to it remotely.
Enabling terminal tools increases the impact of a compromised local gateway;
keep `ENABLE_TERMINAL_TOOLS=false` unless you are deliberately testing the
local, restricted task interface.

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
