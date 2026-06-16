# homelab-tools

A collection of autonomous agents and developer tools built for a self-hosted homelab running Unraid, UniFi, and the Arr media stack. All agents communicate with the homelab via [homelab-mcp](https://github.com/collani-homelab/homelab-mcp) — a self-hosted Model Context Protocol server.

## Agents

Each agent under `agent-*/` is a self-contained Python service with its own `Dockerfile` and `pyproject.toml`. They run on a scheduled [Dagu](https://github.com/daguflow/dagu) orchestrator via `docker run`.

| Agent | Schedule | Purpose |
|-------|----------|---------|
| [`agent-sre-patrol`](agent-sre-patrol/) | Every 15 min | Cross-service SRE health sweep — Unraid array state, disk errors, UPS status, RAM utilization. Sends ntfy alerts on threshold breaches. |
| [`agent-network-sentinel`](agent-network-sentinel/) | Every 5 min | Polls UniFi for active clients against a MAC allowlist. Alerts once per unknown device on first sighting. |
| [`agent-media-health`](agent-media-health/) | Daily 09:00 | Checks NZBGet, Sonarr, Radarr, Lidarr queues and Tautulli session errors. |
| [`agent-storage-report`](agent-storage-report/) | Weekly Mon 08:00 | Unraid array utilization report with projected fill dates and LLM narrative. |
| [`agent-standup`](agent-standup/) | Nightly | Go binary — parallel fan-out to 8 AI personas (SRE, Dev, Manager, Architect, Security, QA, Data, UI/UX) then synthesizes a daily standup report. |

## Developer Tools

| Tool | Language | Purpose |
|------|----------|---------|
| [`agent-status`](agent-status/) | Python | CLI dashboard for parsing and displaying roadmap/project status. |
| [`prompt-optimizer`](prompt-optimizer/) | Python | Hill-climbing prompt optimizer built on [`llm-eval-kit`](https://github.com/wcollani/llm-eval-kit)'s `GEval` scoring. Mutates a base prompt across generations and keeps the top performers. |

YAML-driven LLM benchmarking (single-agent, Generator-Critic-Refiner, Mob of Experts, SRE triage pipelines, OTEL tracing + DeepEval scoring) now lives in the standalone [`llm-eval-kit`](https://github.com/wcollani/llm-eval-kit) package — the `agent-eval/` directory that used to live here is archived at [`Archive/agent-eval`](Archive/agent-eval/) and should not be used.

## Architecture

### Python agents — shared pattern

All Python agents follow the same structure:

```
MCP tool calls (parallel, via homelab-mcp SSE)
  → data extraction / threshold logic (pure Python, no LLM)
    → LLM call for one-line summary only
      → ntfy push alert (if issues found)
        → markdown report written to $REPORTS_DIR
          → document indexed to RAG (homelab-mcp index_document)
```

The LLM is deliberately kept out of the decision loop — all threshold checks are deterministic Python. The LLM only generates a human-readable summary after the severity has already been determined.

### agent-standup — Go fan-out pattern

Uses the [Eino](https://github.com/cloudwego/eino) framework to fan out 8 persona agents concurrently (Option A) or sequentially (Option B), then synthesizes their reports via a synthesizer agent. See [`agent-standup/README.md`](agent-standup/README.md) for benchmark results comparing patterns and model configurations.

### llm-eval-kit — YAML experiment spec

```yaml
name: My Experiment
workflow: single_agent          # or: multi_agent_blog_gen, mob_of_experts, multi_agent_triage
models_to_test: [sre/qwen2.5-coder:7b]
judge_model: ws/qwen2.5-coder:14b
system_prompt: "You are a helpful assistant."
test_cases:
  - name: My Test
    input_file: path/to/input.txt
    expected_output_criteria: "Response should include X and Y"
```

Run with (requires `pip install -e ~/repos/llm-eval-kit` or `pip install llm-eval-kit`):
```bash
llm-eval my_experiment.yaml
```

## Configuration

All agents are configured via environment variables. Copy `.env.example` files where present. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_SSE_URL` | `http://localhost:8083/sse` | homelab-mcp SSE endpoint |
| `OLLAMA_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint |
| `REPORTS_DIR` | `<agent-dir>/reports/` | Directory for markdown report output |
| `NOTIFY_SH` | *(empty — notifications disabled)* | Path to ntfy wrapper script |
| `OLLAMA_PROXY_URL` | `http://localhost:4000/v1` | LiteLLM proxy URL (agent-standup) |
| `OLLAMA_SRE_URL` / `OLLAMA_WS_URL` | `http://localhost:11434` | Direct Ollama routing for `sre/`/`ws/`-prefixed models (llm-eval-kit, prompt-optimizer) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `localhost:4319` | OTLP trace export endpoint |

## Building agent images

```bash
bash build-agents.sh
```

Builds and pushes all `agent-*/` Docker images to the registry set in `$REGISTRY` (defaults to `localhost:5000`).

## Running locally

Each agent can be run directly without Docker:

```bash
cd agent-sre-patrol
uv run python patrol.py
```

```bash
cd agent-standup
go build && ./agent-standup --roadmap /path/to/ROADMAP.md --hardware /path/to/HARDWARE.md
```
