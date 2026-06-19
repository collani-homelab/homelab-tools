#!/bin/bash
set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "[run-patrol] Creating venv..."
    uv venv
fi

echo "[run-patrol] Starting data patrol..."
PYTHONPATH="$(dirname "$SCRIPT_DIR"):${PYTHONPATH:-}" uv run python patrol.py "$@"
