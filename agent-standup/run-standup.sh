#!/bin/bash

# Default to ad-hoc if no argument is provided
MODE=${1:-"ad-hoc"}

# Get the directory where the script is located to ensure portability
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BINARY_PATH="$SCRIPT_DIR/agent-standup"

# Required: paths to your roadmap and hardware context markdown files
ROADMAP=${ROADMAP:-""}
HARDWARE=${HARDWARE:-""}

if [[ -z "$ROADMAP" || -z "$HARDWARE" ]]; then
  echo "error: ROADMAP and HARDWARE env vars must be set to markdown file paths"
  echo "  export ROADMAP=/path/to/ROADMAP.md"
  echo "  export HARDWARE=/path/to/ARCH_HARDWARE.md"
  exit 1
fi

case "$MODE" in
  "ad-hoc")
    # Ad-Hoc execution using Mono_8 configuration
    "$BINARY_PATH" -roadmap="$ROADMAP" -hardware="$HARDWARE" \
      -seq=false -sre=deepseek-r1:14b -dev=phi4:14b -mgr=hermes3:8b -arch=hermes3:8b \
      -sec=hermes3:8b -qa=hermes3:8b -data=hermes3:8b -ui=hermes3:8b -syn=deepseek-r1:14b
    ;;
  "overnight")
    # Overnight execution using Edge_Seq configuration
    "$BINARY_PATH" -roadmap="$ROADMAP" -hardware="$HARDWARE" \
      -seq=true -strict-local=true -sre=hermes3:8b -dev=mistral-nemo:12b -mgr=hermes3:8b \
      -arch=hermes3:8b -sec=hermes3:8b -qa=hermes3:8b -data=hermes3:8b -ui=hermes3:8b \
      -syn=mistral-nemo:12b
    ;;
  *)
    echo "Invalid mode. Please use 'ad-hoc' or 'overnight'."
    exit 1
    ;;
esac

exit 0
