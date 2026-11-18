#!/usr/bin/env bash
set -euo pipefail

echo "Preparing AI Tools Workspace..."
python -m pip install --upgrade pip
python -m pip install --user uv
export PATH="$HOME/.local/bin:$PATH"

if [ -f requirements.txt ]; then
  uv pip install --system -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add API keys only in this private Codespace."
fi

echo
echo "Workspace bootstrap complete."
echo "Next: read docs/SETUP.md. No paid API keys or external services were activated."
