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

The test suite should report six tests and `OK`. With no model credentials, the CLI runs in offline mode.

## 3. Configure an optional chat model

The agent supports OpenAI-compatible chat-completion endpoints. The adapter does not select a provider or activate a service automatically.

1. Open the local `.env` file in the Codespace.
2. Set `MODEL_API_KEY` and `MODEL_NAME` for the endpoint you have chosen.
3. Set `MODEL_BASE_URL` only if the provider requires a custom OpenAI-compatible endpoint.
4. Restart the CLI command.

Example shape only — replace values with credentials and model identifiers from your chosen provider:

```dotenv
MODEL_API_KEY=your_provider_key
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_NAME=your-model-id
```

Leave all three variables blank to remain in offline mode. A model request is sent only when the CLI runs with a complete configuration. Hosted inference may incur charges; verify the provider's current free-tier limits and pricing before use. Never paste credentials into chat or commit `.env` to Git.

## 4. Recommended build order

1. **LangGraph** — workflow and orchestration; implemented and tested.
2. **Chat-model adapter** — configurable OpenAI-compatible interface; implemented. A live provider has not been selected or connected.
3. **MCP adapters** — connect one trusted tool at a time and restrict its permissions.
4. **Persistent memory** — choose a storage backend, retention policy, and deletion process before adding Mem0 or an alternative.
5. **Research providers** — evaluate Exa and Firecrawl; configure keys only if needed.
6. **Prompt compression** — evaluate LLMLingua only after measuring context and resource requirements.
7. **MCP security scanning** — scan third-party servers before enabling them.
8. **Browser automation** — add only after deciding which browser and authentication model to use.

Do not install every repository blindly. Components can overlap, require separate services, consume significant memory, or need paid API credentials. Add and test one component at a time.

## 5. What this setup does not do

- It does not increase ChatGPT's native context window, token allowance, or model permissions.
- It does not connect these services to this ChatGPT conversation automatically.
- It does not provision paid cloud resources or API subscriptions.
- It does not store credentials in GitHub.
- It does not provide free model inference by itself; a compatible endpoint is still required.

## 6. Control Codespaces costs

Stop Codespaces when they are not in use. Check the Codespaces page for active instances and stop unused ones. Review your GitHub billing and included quota before leaving a Codespace running.
