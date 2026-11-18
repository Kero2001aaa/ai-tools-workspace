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

The test suite should report twenty-seven tests and `OK`. With no model credentials, the CLI runs in offline mode.

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

## 10. Recommended build order

1. **LangGraph** — workflow and orchestration; implemented.
2. **Chat-model adapter** — configurable OpenAI-compatible interface; live inference verified with the configured free-model route.
3. **Local MCP server and client** — implemented; local transport and tool discovery verified.
4. **Model tool-calling loop** — implemented; asynchronous tool execution verified.
5. **Persistent memory** — local SQLite store and MCP tools implemented; keyword retrieval.
6. **Research providers** — Exa search and Firecrawl scraping both live-verified.
7. **Context budgeting** — conservative, dependency-free history selection implemented; local test verification pending.
8. **MCP security scanning** — next: inspect and scan third-party servers before enabling them.
9. **Browser automation** — add after deciding which browser and authentication model to use.

Do not install every repository blindly. Components can overlap, require separate services, consume significant memory, or need paid API credentials. Add and test one component at a time.

## 11. What this setup does not do

- It does not increase ChatGPT's native context window, token allowance, or model permissions.
- It does not connect these services to this ChatGPT conversation automatically.
- It does not provision paid cloud resources or API subscriptions.
- It does not store credentials in GitHub.
- It does not guarantee a free provider's uptime, model availability, or quality.

## 12. Control Codespaces costs

Stop Codespaces when they are not in use. Check the Codespaces page for active instances and stop unused ones. Review your GitHub billing and included quota before leaving a Codespace running.
