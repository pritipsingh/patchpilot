#!/usr/bin/env bash
# Create .venv and install the project.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

uv venv .venv --python 3.12
VIRTUAL_ENV="$ROOT/.venv" uv pip install -r requirements.txt
VIRTUAL_ENV="$ROOT/.venv" uv pip install ruff mypy pytest pytest-asyncio

echo "Done. Run: source .venv/bin/activate && python -m app.main"
