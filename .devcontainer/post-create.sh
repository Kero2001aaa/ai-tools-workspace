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

  # The base Codespaces image may contain an outdated Yarn APT signing key.
  # Temporarily disable only that third-party source while installing the
  # official Debian packages required by Playwright, then restore it.
  yarn_source="/etc/apt/sources.list.d/yarn.list"
  yarn_disabled="${yarn_source}.codespace-disabled"
  restore_yarn_source() {
    if sudo test -f "$yarn_disabled"; then
      sudo mv "$yarn_disabled" "$yarn_source"
    fi
  }

  if sudo test -f "$yarn_source"; then
    if sudo test -e "$yarn_disabled"; then
      echo "Refusing to overwrite existing APT backup: $yarn_disabled" >&2
      exit 1
    fi
    sudo mv "$yarn_source" "$yarn_disabled"
    trap restore_yarn_source EXIT
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
