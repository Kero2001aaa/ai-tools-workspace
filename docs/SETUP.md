# Setup and operating guide

## 1. Create the Codespace (one manual action)

1. Open https://github.com/Kero2001aaa/ai-tools-workspace
2. Select **Code** → **Codespaces** → **Create codespace on main**.
3. Wait for the container to finish its first setup. The bootstrap script installs the lightweight Python foundation.
4. Open the terminal inside the browser-based VS Code if you need to run commands.

The repository connection can prepare files and commits, but it cannot create or operate your Codespace session.

## 2. Verify the environment

In the Codespaces terminal:

```bash
python --version
node --version
docker --version
python -c "import langgraph; import langchain_mcp_adapters; print('Core packages OK')"
```

## 3. Configure credentials safely

- Open the `.env` file in the Codespace.
- Add only the API keys for services you actually choose to use.
- Never paste keys into chat, commit them to Git, or put them in source files.
- Some hosted providers require paid plans or usage-based billing. Source code being open-source does not make those APIs free.

## 4. Recommended build order

1. **LangGraph** — agent workflow and orchestration.
2. **MCP adapters / mcp-use** — connect compatible tools through MCP.
3. **Mem0** — add persistent memory after choosing a storage backend.
4. **Exa and Firecrawl** — optional research providers; configure keys only if needed.
5. **LLMLingua** — optional token/prompt compression; evaluate resource requirements before installing.
6. **MCP Scanner** — scan third-party MCP servers before enabling them.
7. **Browser automation** — add only after deciding which browser and authentication model to use.

Do not install every repository blindly. Some components overlap, require separate services, consume significant memory, or need paid API credentials.

## 5. What this setup does not do

- It does not increase ChatGPT's native context window, token allowance, or model permissions.
- It does not connect these services to this ChatGPT conversation automatically.
- It does not provision paid cloud resources or API subscriptions.
- It does not store credentials in GitHub.

## 6. Shut down to avoid unnecessary compute use

When finished, use the Codespaces menu to stop the Codespace. Review your included monthly compute/storage quota and billing settings before leaving it running.
