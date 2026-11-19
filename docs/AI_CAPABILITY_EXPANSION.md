# Phase 13.5 — AI capability expansion

This phase evaluates and integrates the eight unique repositories requested before Phase 14. OmniRoute was listed twice and is tracked once.

## Integration rules

- Keep the default workspace free and offline-capable.
- Never enable billing, purchase credits, or configure paid inference automatically.
- Never copy browser cookies, account sessions, passwords, or private credentials into the workspace.
- Treat provider quotas, free-tier status, model names, and repository installation instructions as changeable. Verify them at setup time.
- Add one dependency or service at a time; run the full test suite after each code change.
- A repository link or catalog entry is not an installed or verified integration.

## Inventory and decision

| Component | Intended role | Decision | Reason / boundary |
|---|---|---|---|
| [OmniRoute](https://github.com/cheaper-inference/OmniRoute) | OpenAI-compatible model gateway, provider routing and fallback | **First integration: endpoint-compatible** | The current `ChatOpenAI` adapter already accepts `MODEL_BASE_URL`, `MODEL_API_KEY`, and `MODEL_NAME`. Pointing it at a verified OmniRoute endpoint requires configuration, not a second model adapter. The gateway itself is not deployed by this change. Confirm the canonical upstream, current installation method, license, and provider terms before deployment. |
| [awesome-free-llm-apis](https://github.com/mnfst/awesome-free-llm-apis) | Discovery catalog for free inference APIs | **Reference catalog** | It is a curated list, not a runtime service. Entries and quotas can become stale; verify each provider's official pricing, acceptable-use, data, and retention policies before adding credentials. |
| [Graphify](https://github.com/Graphify-Labs/graphify) | Build/query a local code knowledge graph | **Optional MCP adapter added and verified** | Graphify MCP is registered when the executable and graph path are configured. User generated a local code-only graph (306 nodes, 602 edges) and verified agent queries against it. |
| [Agent-Reach](https://github.com/Panniantong/Agent-Reach) | Search/read social and specialist web sources | **Public GitHub + V2EX MCP bridge added and runtime-verified; opt-in** | Pinned CLI v1.6.1 installed in an isolated uv tool environment. Six read-only operations use anonymous GitHub REST and public V2EX API routes. MCP discovery found all six tools; live agent calls successfully queried both backends. No login, session, or social connectors are enabled. |
| [Superpowers](https://github.com/obra/superpowers) | Structured software-engineering skills: discovery, plans, TDD, review | **Adapted locally (docs-only)** | Its published integrations target specific coding-agent harnesses. This LangGraph agent is not automatically a compatible harness. Selected practices are documented in [SUPERPOWERS_WORKFLOW.md](SUPERPOWERS_WORKFLOW.md); the upstream plugin is not installed and no automatic skill hooks are claimed. |
| [RTK](https://github.com/rtk-ai/rtk) | Compress terminal command output before an agent consumes it | **Deferred: not applicable to current agent runtime** | RTK rewrites shell commands through coding-agent hooks. This workspace agent exposes no arbitrary shell tool, so installing its hooks would not compress MCP tool results or conversation history. Upstream source also documents an Apache-2.0 CLI combined by default with a BUSL-1.1 engine (Additional Use Grant permits production use of the distributed binary; Change Date 2030-09-09). Reassess only if a controlled shell/coding-agent integration is added. |
| [Understand Anything](https://github.com/Egonex-AI/Understand-Anything) | Interactive codebase knowledge graph and guided exploration | **Deferred pending comparison** | Significant overlap with Graphify's codebase graph. Compare output quality, resource footprint, runtime, and integration surface before choosing one; avoid running both by default. |
| [REA](https://github.com/morluto/rea) | MCP-based reverse engineering of binaries, apps, and runtime behavior | **Deferred specialist tool** | Not required for the general agent baseline. Node-based setup and reverse-engineering tools expand the attack surface; install only for a specific authorized analysis task after reviewing tool contracts and sandboxing. |

## Phase order

1. **OmniRoute compatibility** — document the gateway endpoint contract and retain the existing OpenAI-compatible adapter.
2. **Free API catalog** — use the upstream list as a discovery reference; no credentials are harvested or configured.
3. **Graphify** — optional stdio MCP adapter added; user installed the upstream package, generated a code-only graph, and verified MCP discovery and graph queries.
4. **Agent-Reach** — pinned v1.6.1 installed; public GitHub API and V2EX API bridge implemented and live-tested. The bridge remains opt-in.
5. **Superpowers adaptation** — selected practices documented in `docs/SUPERPOWERS_WORKFLOW.md`; no upstream plugin runtime installed.
6. **RTK evaluation** — assessed source, CLI hook model, and licensing; deferred because this agent has no shell tool to benefit from it.
7. **Understand Anything comparison** — compare against Graphify and select at most one default graph engine.
8. **REA specialist evaluation** — keep disabled until a concrete reverse-engineering use case exists.

## OmniRoute endpoint contract

The existing model loader in `agent/model.py` supports any OpenAI-compatible base URL:

```dotenv
MODEL_API_KEY=your_gateway_key
MODEL_BASE_URL=https://your-verified-omniroute-host/v1
MODEL_NAME=your-configured-model-or-route
```

These values are examples, not live credentials or a verified OmniRoute deployment URL. The gateway may require its own provider credentials. Configure only providers with a confirmed free tier and keep paid fallback/routing disabled. Do not assume an aggregate free-token headline is a quota granted by OmniRoute; each upstream provider controls its own limits and terms.

## Current completion boundary

The repository does **not** deploy OmniRoute or automatically install the upstream Superpowers plugin, RTK, Understand Anything, or REA. Agent-Reach v1.6.1 is installed separately in the user's Codespace; its public-only MCP bridge is opt-in and runtime-tested. Superpowers-inspired practices are documented locally. Graphify's optional MCP adapter is installed and verified in the user's Codespace. No new Python runtime dependency, provider credential, browser session, or paid service is added.

After each actual installation, record the exact pinned version/commit, license, required secrets, resource footprint, enabled capabilities, and test evidence here.
