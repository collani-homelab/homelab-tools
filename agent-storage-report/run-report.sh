#!/bin/bash
set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "[run-report] Creating venv..."
    uv venv
fi

echo "[run-report] Generating weekly storage capacity report..."
uv run python report.py "$@"
