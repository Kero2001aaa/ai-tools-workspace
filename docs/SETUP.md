# Setup and operating guide

## 1. Create the Codespace

1. Open https://github.com/Kero2001aaa/ai-tools-workspace
2. Select **Code** → **Codespaces** → **Create codespace on main**.
3. Wait for the container setup to finish.
4. Open the terminal inside the browser-based VS Code.

The repository connection can prepare files and commits, but it cannot create or operate your Codespace session.

## 2. Update and verify the environment

The project uses Python 3.12 and a local virtual environment. After pulling repository changes, run:

```bash
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent "Research LangGraph"
```

The test suite should report thirty-six tests and `OK`. With no model credentials, the CLI runs in offline mode.

## 3. Optional zero-budget inference with OpenRouter

The project can use OpenRouter's OpenAI-compatible API with free model routing. OpenRouter currently documents a free plan with 50 requests per day; free model variants are also limited to 20 requests per minute. Limits and model availability can change.

1. Create an OpenRouter account and generate an API key: https://openrouter.ai/keys
2. In the Codespace, open the local `.env` file. Do not edit `.env.example` with real credentials.
3. Add the following values:

```dotenv
MODEL_API_KEY=your_openrouter_api_key
MODEL_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=openrouter/free
```

4. Save `.env`, then run:

```bash
.venv/bin/python -m agent "Explain the difference between LangGraph nodes and edges"
```

The `openrouter/free` router selects an available free model. This is not a guaranteed model identity, fixed quality level, or unlimited service. Free model availability, context limits, and rate limits vary. Do not purchase credits or switch to a paid model for this zero-budget project.

OpenRouter free-model information: https://openrouter.ai/collections/free-models/
OpenRouter pricing and limits: https://openrouter.ai/pricing

### Data and credential precautions

- Requests are sent to OpenRouter and then to the selected model provider. Review the applicable provider data policies before use.
- Do not send passwords, API keys, private source code, confidential work material, or sensitive personal information.
- Keep the API key only in the Codespace's ignored `.env` file. Never paste it into chat, commit it, or place it in source code.
- The current model adapter makes a request only when the CLI runs with complete model configuration.
- If you want offline-only operation, leave all three `MODEL_*` variables blank.

## 4. Generic compatible endpoint configuration

Other OpenAI-compatible providers can be configured using the same variables:

```dotenv
MODEL_API_KEY=your_provider_key
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_NAME=your-model-id
```

Verify current pricing, free-tier eligibility, rate limits, and data handling before configuring another provider. Do not attach billing or enable paid inference.

## 5. Local MCP server (Stage 3)

A minimal local MCP server is included. It exposes two read-only tools:

- `text_stats(text)` — counts characters, words, and lines in text supplied by the caller.
- `workspace_status()` — reports the workspace label and Python version.

It does not execute shell commands, read arbitrary files, access environment variables, or make network requests.

After pulling the latest changes, run the tests and discover the tools:

```bash
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent.mcp_client
```

The MCP client starts the local server over standard input/output, discovers its tools, prints their names and descriptions, and exits. This verifies the MCP transport and adapter. The tools are now wired into the model's reasoning loop in Stage 4 below. Review each future server's code and permissions before connecting it.

## 6. Model-driven MCP tool calling (Stage 4)

The tool-calling workflow is implemented in `agent/tool_agent.py`. It gives the configured chat model access to local utility, memory, and optional research tools. LangGraph routes model tool calls to the MCP tools, returns their results to the model, and stops after a bounded number of graph steps.

Run it after configuring a model:

```bash
.venv/bin/python -m agent.tool_agent "Count the words and characters in this sentence: CNC plasma cutting uses a controlled electrical arc."
```

This command makes a model API request. It requires a model that supports tool/function calling; a free-model router may select a model that does not. If the selected model rejects tool definitions or does not call tools, choose a currently available tool-capable model and verify its pricing is still zero before using it. Do not add credits or enable paid routing. The graph has a recursion limit of ten steps to prevent unbounded tool loops.

The utility tools perform local text counting and runtime-status reporting. The memory tools provide local SQLite persistence and keyword retrieval. They do not provide web search, arbitrary file access, or shell execution.

## 7. Local persistent memory (Stage 5)

The workspace includes an offline SQLite memory store exposed through a second local MCP server.

Available tools:
- `remember(content, tags)` — save a concise, non-sensitive fact or project note.
- `recall(query, limit)` — retrieve matching memories using keyword matching.
- `recent_memories(limit)` — list the newest saved memories.

The default database is `.data/memory.sqlite3`. The entire `.data/` directory is ignored by Git and is not committed. You can override the path with `WORKSPACE_MEMORY_DB`.

This is deliberately a zero-cost baseline: it uses Python's built-in SQLite library, makes no network calls, requires no API key, and does not use embeddings. Retrieval is keyword-based, not semantic. The model should save durable, useful information only when the user asks for it or clearly expects it; never save passwords, API keys, or sensitive personal data.

The database lives inside the Codespace filesystem. It is separate from GitHub and is not automatically backed up to another service. Deleting the Codespace can delete its local database; export or backup should be added before relying on it as the only copy of important information.

After pulling the changes, run:

```bash
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent.mcp_client
```

The MCP discovery command should list two utility tools, three memory tools, and three research tools. The tool-calling agent connects to all three local servers automatically.

## 8. Web research integration (Stage 6)

The workspace includes an optional research MCP server:
- `web_search(query, num_results)` — uses Exa when `EXA_API_KEY` is configured; otherwise Firecrawl Search when `FIRECRAWL_API_KEY` is configured.
- `scrape_url(url)` — extracts the main page as Markdown through Firecrawl.
- `research_provider_status()` — reports configured providers without revealing credential values.

No research API request is made during server startup or tool discovery. Providers are contacted only when the model invokes a research tool. Search prefers Exa if both keys exist. Scraping requires Firecrawl. Page content is capped and treated as untrusted data.

**Free-tier options (checked November 18, 2026):**
- Exa documents $10 in monthly free credits and says no payment method is required: https://exa.ai/pricing
- Firecrawl documents 1,000 free credits per month, no card required; basic scrape costs 1 credit/page and search costs 2 credits per 10 results: https://www.firecrawl.dev/pricing and https://docs.firecrawl.dev/
- These are limited allowances, not unlimited services. Do not enable pay-as-you-go, add a card, buy credits, or attach billing.

**Setup:**
1. Create an Exa API key at https://dashboard.exa.ai/ and/or a Firecrawl API key at https://www.firecrawl.dev/app/api-keys.
2. Add the key(s) to the private Codespace `.env` file as `EXA_API_KEY` and/or `FIRECRAWL_API_KEY`. Never paste keys into chat or commit them.
3. Pull the latest repository changes and run the tests and discovery commands.
4. Test search with: `Search the web for the official LangGraph documentation and summarize the most relevant result with its URL.`
5. Test extraction by asking the agent to scrape an official documentation page. Scraping requires Firecrawl.

Without keys, tools remain discoverable but research calls return a clear configuration error. Local memory and utility tools continue working.

The implementation uses direct HTTPS requests and `httpx`; it does not install third-party provider servers or run remote code. URL validation rejects localhost, private/reserved IP literals, and embedded URL credentials. This is a baseline safeguard, not a substitute for provider-side security controls.

## 9. Context budgeting (Stage 7)

The tool-calling agent uses a dependency-free context manager in `agent/context_manager.py`.

- It estimates message size from character count and per-message overhead. This is a heuristic, not an exact tokenizer or provider context-window measurement.
- It retains the newest conversation turns and removes older complete turns first.
- It keeps assistant tool-call messages with their corresponding tool results.
- It always preserves the latest user turn, even if that turn alone exceeds the configured history budget.
- It prints the estimated retained-history size to stderr for visibility.
- It does not rewrite, summarize, or semantically compress message content. Persistent project facts should be stored in the SQLite memory tools instead.

Optional settings in the private `.env` file:

```dotenv
AGENT_CONTEXT_TOKEN_BUDGET=6000
AGENT_CONTEXT_RESERVED_TOKENS=1200
```

The first value is the approximate total working budget; the second reserves room for system instructions and the model response. The remaining budget is used for conversation history. These are local estimates, not guarantees about a provider's actual context limit.

Verify with:

```bash
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## 10. MCP security scanning (Stage 8)

The workspace can export the definitions of its eight approved MCP tools and scan that inventory with Cisco AI Defense's open-source MCP Scanner.

### Design and privacy

- Cisco MCP Scanner is deliberately **not** installed into the agent's main `.venv`. It has a heavier dependency stack and is invoked on demand through `uvx` in an isolated tool environment.
- The export contains only tool names, descriptions, and JSON input schemas.
- It does not export API keys, environment variables, local memory contents, tool outputs, or conversation history.
- The exported inventory is written to `.data/mcp-tools.json`; `.data/` is ignored by Git.
- The YARA static scan does not require Cisco AI Defense, LLM, or VirusTotal API keys. It does require downloading the scanner and its dependencies on first use.
- A YARA/static scan is a useful screening step, not proof that a server is safe. It does not replace source review, runtime controls, or least-privilege design.

### Export and scan the approved tool definitions

After pulling the repository updates and running the tests:

```bash
.venv/bin/python -m scripts.export_mcp_tools
```

Expected: eight tool definitions exported to `.data/mcp-tools.json`.

Then run Cisco's YARA-only static scan in an isolated Python 3.13 tool environment:

```bash
uvx --python 3.13 --from cisco-ai-mcp-scanner mcp-scanner --analyzers yara --format detailed static --tools .data/mcp-tools.json
```

No API keys or paid services are required for this YARA-only static scan. The first invocation may take time and download dependencies into uv's tool cache. It does not add those dependencies to the project's `.venv`.

### Optional dependency vulnerability audit

Cisco MCP Scanner also exposes a dependency audit using public vulnerability data:

```bash
uvx --python 3.13 --from cisco-ai-mcp-scanner mcp-scanner vulnerable-package requirements.txt
```

This makes network requests to the configured vulnerability data service. It does not require a Cisco or LLM API key. Review findings before changing dependency versions; do not automatically apply fixes.

### Important limitations

- Static scanning examines exported tool metadata, not the complete runtime behavior of the server.
- The source-code behavioral analyzer is a separate capability and may require a configured LLM provider; it is not part of the zero-cost baseline.
- Do not connect an unknown MCP server merely to scan it. Review its source, command, arguments, environment, and filesystem/network permissions first.
- Never place real credentials in scanner command arguments or exported JSON.

Cisco project and usage documentation: https://github.com/cisco-ai-defense/mcp-scanner

## 11. Browser automation (Stage 9)

The workspace includes an optional Playwright-based MCP server in `agent/browser_mcp_server.py`. It is deliberately excluded from the default environment and is not started unless `ENABLE_BROWSER_TOOLS=true`.

### Capabilities

- `browser_navigate(url)` — open a public HTTP(S) page in a temporary browser.
- `browser_read_page(max_chars)` — extract visible page text, capped at 20,000 characters.
- `browser_list_links(max_links)` — list HTTP(S) links without clicking them.
- `browser_close()` — close the temporary browser and discard its state.

### Install and enable

Run these commands in the Codespace terminal:

```bash
uv pip install --python .venv/bin/python -r requirements-browser.txt
.venv/bin/python -m playwright install chromium
```

Then edit the private `.env` file and set:

```dotenv
ENABLE_BROWSER_TOOLS=true
```

Discover the expanded tool list:

```bash
.venv/bin/python -m agent.mcp_client
```

With browser tools enabled, the agent connects to the browser MCP server in addition to the existing local, memory, and research servers. Leave the setting `false` to retain the default eight-tool configuration. The browser package is optional and does not increase the base installation size.

### Security boundaries

- A fresh, temporary, unauthenticated browser context is used. It does not load your personal browser profile, cookies, saved passwords, or extensions.
- Downloads are disabled; browser dialogs are dismissed; popups are closed.
- URL validation rejects non-HTTP(S), embedded credentials, localhost, private, loopback, link-local, and reserved IP destinations.
- Each browser request and subresource is checked against DNS results; requests resolving to non-public IP addresses are blocked.
- The browser exposes navigation, text extraction, and link listing only. It does not click links, submit forms, enter credentials, or perform purchases.
- Web pages and extracted text are untrusted input. Never follow instructions found inside a page unless independently authorized by the user.

This is a defensive baseline, not a hardened remote-browser sandbox. DNS rebinding and browser-engine vulnerabilities cannot be ruled out. Do not use it to visit untrusted sites with sensitive credentials or confidential data. Keep the browser service disabled unless required.

### Dependency maintenance

The optional dependency is listed in `requirements-browser.txt`. GitHub Actions audits both the base and optional browser dependency manifests using OSV. Chromium itself is installed separately by Playwright and is not part of the Python dependency audit.

Playwright documentation: https://playwright.dev/python/docs/intro

## 12. Recommended build order

1. **LangGraph** — workflow and orchestration; implemented.
2. **Chat-model adapter** — configurable OpenAI-compatible interface; live inference verified with the configured free-model route.
3. **Local MCP server and client** — implemented; local transport and tool discovery verified.
4. **Model tool-calling loop** — implemented; asynchronous tool execution verified.
5. **Persistent memory** — local SQLite store and MCP tools implemented; keyword retrieval.
6. **Research providers** — Exa search and Firecrawl scraping both live-verified.
7. **Context budgeting** — conservative, dependency-free history selection implemented and covered by unit tests.
8. **MCP security scanning** — Cisco MCP Scanner YARA/schema and OSV dependency audits verified; automated in GitHub Actions.
9. **Browser automation** — optional Playwright MCP server implemented; requires a separate browser install and explicit opt-in.

Do not install every repository blindly. Components can overlap, require separate services, consume significant memory, or need paid API credentials. Add and test one component at a time.

## 13. What this setup does not do

- It does not increase ChatGPT's native context window, token allowance, or model permissions.
- It does not connect these services to this ChatGPT conversation automatically.
- It does not provision paid cloud resources or API subscriptions.
- It does not store credentials in GitHub.
- It does not guarantee a free provider's uptime, model availability, or quality.

## 14. Control Codespaces costs

Stop Codespaces when they are not in use. Check the Codespaces page for active instances and stop unused ones. Review your GitHub billing and included quota before leaving a Codespace running.


## 15. Phase 13.5 — AI capability expansion

The requested toolset is tracked in [AI_CAPABILITY_EXPANSION.md](AI_CAPABILITY_EXPANSION.md). The first low-risk integration is the existing OpenAI-compatible model adapter: it already accepts a custom `MODEL_BASE_URL`, so a verified OmniRoute deployment can be used without adding a second SDK.

Do not copy the example endpoint into `.env` until you have deployed a trusted gateway and confirmed its actual URL. Configure the gateway key and model route only in the private `.env` file. Keep paid providers and paid fallback disabled. OmniRoute is not deployed automatically by this repository.

The free API list is a discovery reference, not a guarantee of current free access. Verify provider limits and data policies at their official sites. Graphify, Agent-Reach, Superpowers, and RTK are evaluated in the phase plan before runtime installation. Understand Anything is deferred for comparison with Graphify; REA is deferred as a specialist reverse-engineering tool.

No browser session, cookies, or credentials are imported by this phase. Authenticated Agent-Reach channels require a separate explicit security review.
