#!/usr/bin/env bash
# Bootstrap a private Python environment, then run the guided connector setup.
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
if ! command -v uv >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing uv with Homebrew..."
        brew install uv
    else
        echo "Install the free uv tool first: https://docs.astral.sh/uv/getting-started/installation/" >&2
        echo "Then run this script again. On macOS: brew install uv" >&2
        exit 1
    fi
fi
if [ ! -x .venv-mcp/bin/python ]; then
    uv venv --python 3.12 .venv-mcp
fi
uv pip install --python .venv-mcp/bin/python -r requirements-mcp.txt
exec .venv-mcp/bin/python connector_setup.py "$@"
