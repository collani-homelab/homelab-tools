# homelab-tools AI Agent Context (AGENTS.md)

This file is the primary context layer for AI agents operating in this repository. Read it before touching any code.

## 1. Repository Purpose & Scope

A collection of autonomous Python agents and developer tools for a self-hosted homelab. All agents communicate with the homelab over [homelab-mcp](https://github.com/collani-homelab/homelab-mcp) SSE. They run as scheduled Dagu DAGs on the SRE machine.

**This is a public portfolio repo.** Keep secrets out of code and commit history. Sensitive config goes in `.env` files on the host, never in this repo.

## 2. Agent Architecture (Shared Pattern)

Every Python agent in `agent-*/` follows the same structure:

```
MCP tool calls (parallel, via homelab-mcp SSE at MCP_SSE_URL)
  → threshold logic in pure Python (no LLM in the decision loop)
    → LLM call for one-line human-readable summary only
      → ntfy push alert (if threshold breached)
        → markdown report written to $REPORTS_DIR
          → document indexed to RAG (homelab-mcp index_document)
```

Key rule: **the LLM never decides severity.** All threshold checks are deterministic Python. The LLM only generates the human summary after severity is already known.

## 3. Directory Structure

| Path | Purpose |
|------|---------|
| `agent-sre-patrol/` | Every 15 min SRE health sweep — Unraid, UPS, RAM, disk errors |
| `agent-network-sentinel/` | Every 5 min UniFi client scan against MAC allowlist |
| `agent-media-health/` | Daily media stack health (NZBGet, Sonarr, Radarr, Tautulli) |
| `agent-storage-report/` | Weekly Unraid array utilization report with projected fill dates |
| `agent-vision-patrol/` | Every 30 min — screenshots Grafana panels, multimodal LLM anomaly detection |
| `agent-data-patrol/` | Every 30 min — z-score over Prometheus + Loki error rate + Phoenix spans; compare.py for head-to-head eval vs vision-patrol |
| `agent-standup/` | Go binary — nightly 8-persona fan-out standup report |
| `agent-status/` | CLI dashboard for roadmap/project status |
| `prompt-optimizer/` | Hill-climbing prompt optimizer using llm-eval-kit GEval scoring |

## 4. CI/CD & Container Images

**This is a public repo — CI runs on GitHub-hosted runners only.** Never use `self-hosted` in any workflow here.

```
push to main (dependency files only: Dockerfile, pyproject.toml, uv.lock)
  → build-and-push.yml on ubuntu-latest
  → docker buildx builds each agent-* image in parallel (matrix strategy)
  → pushes to ghcr.io/collani-homelab/homelab-agent-<name>:latest + :<sha>
```

Trigger manually via `gh workflow run build-and-push.yml` if you change a Dockerfile without touching deps.

**Deploy:** Images are pulled by the `image-updater` Dagu DAG in `homelab-platform` (nightly 3am). DAG definitions reference `ghcr.io/collani-homelab/homelab-agent-*:latest`. The SRE machine Docker daemon must be authenticated: `gh auth token | docker login ghcr.io -u wcollani --password-stdin`.

## 5. Adding a New Agent

1. Copy an existing agent dir: `cp -r agent-sre-patrol agent-newagent`
2. Update `pyproject.toml` (name, deps), `uv.lock` (`uv lock`), and the main script
3. Add it to the matrix in `.github/workflows/build-and-push.yml`
4. Add a DAG YAML in `homelab-platform/services/dagu/dags/<name>.yaml` referencing `ghcr.io/collani-homelab/homelab-agent-newagent:latest`
5. Push — CI builds the image; homelab-platform DAG runs it on schedule

## 6. Development Workflow

Run an agent locally without Docker:
```bash
cd agent-sre-patrol
cp .env.example .env  # fill in MCP_SSE_URL, OLLAMA_URL, etc.
uv run python patrol.py
```

Key environment variables (all agents):

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_SSE_URL` | `http://localhost:8083/sse` | homelab-mcp SSE endpoint |
| `OLLAMA_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint |
| `REPORTS_DIR` | `<agent-dir>/reports/` | Output directory for markdown reports |
| `NOTIFY_SH` | *(empty)* | Path to ntfy wrapper script (disables alerts if unset) |

## 7. Evaluation & Benchmarking

Use [`llm-eval-kit`](https://github.com/wcollani/llm-eval-kit) for any agent prompt evaluation or A/B testing. The `prompt-optimizer/` tool in this repo uses llm-eval-kit's GEval scoring internally. Do not use the archived `Archive/agent-eval/` — it is superseded by llm-eval-kit.
