#!/bin/bash
set -e
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then uv venv; fi
uv run python sentinel.py "$@"
