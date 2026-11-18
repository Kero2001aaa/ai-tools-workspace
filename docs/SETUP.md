# Setup and operating guide

## 1. Create the Codespace

1. Open https://github.com/Kero2001aaa/ai-tools-workspace
2. Select **Code** → **Codespaces** → **Create codespace on main**.
3. Wait for the container setup to finish.
4. Open the terminal inside the browser-based VS Code.

The repository connection can prepare files and commits, but it cannot create or operate your Codespace session.

## 2. Verify the environment

The project uses Python 3.12 and a local virtual environment. In the Codespaces terminal run:

```bash
python --version
.venv/bin/python -c "import langgraph; import langchain_mcp_adapters; print('Core packages OK')"
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent "Research LangGraph"
```

The test command should report four tests and `OK`.

## 3. Configure credentials safely

- The bootstrap script creates a local `.env` file from `.env.example`.
- Add only API keys for services you explicitly choose to use.
- Never paste keys into chat, commit them to Git, or put them in source files.
- Some hosted providers require paid plans or usage-based billing. Open-source code does not make hosted APIs free.

## 4. Recommended build order

1. **LangGraph** — agent workflow and orchestration. The initial provider-free workflow is implemented and tested.
2. **MCP adapters** — connect compatible tools through MCP, starting with a small, trusted local example.
3. **Persistent memory** — choose a storage backend and retention policy before adding Mem0 or an alternative.
4. **Research providers** — evaluate Exa and Firecrawl; configure keys only if needed.
5. **Prompt compression** — evaluate LLMLingua only after measuring context and resource requirements.
6. **MCP security scanning** — scan third-party servers before enabling them.
7. **Browser automation** — add only after deciding which browser and authentication model to use.

Do not install every repository blindly. Components can overlap, require separate services, consume significant memory, or need paid API credentials. Add and test one component at a time.

## 5. What this setup does not do

- It does not increase ChatGPT's native context window, token allowance, or model permissions.
- It does not connect these services to this ChatGPT conversation automatically.
- It does not provision paid cloud resources or API subscriptions.
- It does not store credentials in GitHub.

## 6. Control Codespaces costs

Stop Codespaces when they are not in use. Check the Codespaces page for active instances and stop unused ones. Review your GitHub billing and included quota before leaving a Codespace running.
