# agent-standup

A multi-agent simulation tool that mimics a team standup by assigning various LLM roles (SRE, Dev, Manager, etc.) to discuss infrastructure or project status.

## What it does

- Simulates a structured standup process using multiple LLM agents
- Supports different execution modes:
  - `ad-hoc`: Uses a predefined configuration (e.g., `Mono_8`) with specific LLM assignments (e.g., `deepseek-r1:14b` for SRE, `phi4:14b` for Dev)
  - `overnight`: Uses a different configuration (e.g., `Edge_Seq`) for longer-running simulations
- Assigns specific roles to LLMs: SRE, Dev, Manager, Architect, Security, QA, Data, UI, and Sync
- Can be integrated into larger automation workflows via the `run-standup.sh` script

## Prerequisites

| Requirement | Value |
|---|---|
| Go | ≥ 1.26 |
| Shell | `bash` |

## Building

To build the `agent-standup` binary, navigate to the tool directory and run:

```bash
go build -o agent-standup
```

## Running

You can run the simulation using the provided shell script. The script is portable and can be run from any directory:

```bash
# Run in ad-hoc mode
./homelab/Tools/agent-standup/run-standup.sh ad-hoc

# Run in overnight mode
./homelab/Tools/agent-standup/run-standup.sh overnight
```

## Configuration

The roles and models used in each mode are configured within `run-standup.sh`. You can modify the model assignments for each role directly in this script to customize your simulation. Each role (e.g., `-sre`, `-dev`, `-mgr`) is passed as a flag to the `agent-standup` binary.

## Dependencies

The project is a Go module. Ensure you have Go installed to build or run the tool.