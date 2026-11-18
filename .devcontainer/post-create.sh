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

# Browser automation is part of this workspace: install its Python package,
# Chromium system libraries, and browser binary during Codespaces creation.
if [ -f requirements-browser.txt ]; then
  uv pip install --python .venv/bin/python -r requirements-browser.txt

  # The base Codespaces image may contain an obsolete Yarn APT signing key.
  # Yarn is not required by this Python workspace; disable its unrelated
  # third-party source so Debian package installation remains reproducible.
  # Do not restore a source whose signing key is invalid.
  yarn_source="/etc/apt/sources.list.d/yarn.list"
  yarn_disabled="${yarn_source}.disabled"
  if sudo test -f "$yarn_source"; then
    if sudo test -e "$yarn_disabled"; then
      echo "Refusing to overwrite existing disabled Yarn source: $yarn_disabled" >&2
      exit 1
    fi
    sudo mv "$yarn_source" "$yarn_disabled"
    echo "Disabled obsolete Yarn APT source: $yarn_disabled"
  fi

  sudo .venv/bin/python -m playwright install-deps chromium
  restore_yarn_source
  trap - EXIT

  .venv/bin/python -m playwright install chromium
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
