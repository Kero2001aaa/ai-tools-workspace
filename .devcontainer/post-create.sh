#!/usr/bin/env bash
set -euo pipefail

echo "Preparing AI Tools Workspace..."

# Install uv in the Codespaces user's home directory; never write to /usr/local.
python -m pip install --user --upgrade uv
export PATH="$HOME/.local/bin:$PATH"

# Keep project dependencies isolated and writable by the vscode user.
uv venv .venv --python python
if [ -f requirements.txt ]; then
  uv pip install --python .venv/bin/python -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add API keys only in this private Codespace."
fi

echo
echo "Workspace bootstrap complete."
echo "Virtual environment: .venv"
echo "Activate with: source .venv/bin/activate"
echo "No paid API keys or external services were activated."
