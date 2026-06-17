# homelab-tools Coding Guidelines (CLAUDE.md)

A collection of Python agents running as Dagu DAGs on the SRE machine. Each agent in `agent-*/` is self-contained with its own `Dockerfile`, `pyproject.toml`, and `uv.lock`.

## Build and Development Commands

### Running an agent locally (no Docker)

```bash
cd agent-sre-patrol     # or any agent-* dir
uv run python patrol.py
```

### Building a Docker image locally (dev only)

```bash
# Production images are built by CI and pushed to ghcr.io/collani-homelab/homelab-agent-*
cd agent-sre-patrol
docker build -t homelab-agent-sre-patrol:dev .
```

### Updating dependencies

```bash
cd agent-sre-patrol
uv add <package>          # updates pyproject.toml + uv.lock
# Commit both pyproject.toml AND uv.lock — CI uses uv sync --frozen
```

### Triggering a CI image build manually

```bash
gh workflow run build-and-push.yml --repo collani-homelab/homelab-tools
```

## Coding Standards

### 1. Agent structure (mandatory pattern)

All Python agents must follow the MCP-first, LLM-last pattern:

1. Gather data via parallel MCP tool calls to homelab-mcp SSE
2. Apply deterministic threshold checks in Python
3. Call the LLM **only** for a human-readable summary after severity is determined
4. Push an ntfy alert if thresholds are breached
5. Write a markdown report to `$REPORTS_DIR`
6. Index the report via `homelab-mcp`'s `index_document` tool

Never use the LLM to decide whether something is an incident. Keep that logic in Python.

### 2. Environment variables

Always read config from environment variables. Provide a `.env.example`. Never hardcode IPs or credentials.

### 3. uv for dependency management

Use `uv` exclusively — no `pip install`, no `requirements.txt` for agent deps. The Dockerfile copies `pyproject.toml` + `uv.lock` and runs `uv sync --no-dev --frozen`. If you add a dependency, run `uv add <pkg>` and commit the updated lock file.

### 4. Scripts are volume-mounted, not baked in

Agent Dockerfiles install only the Python environment (dependencies). The actual `.py` script files are volume-mounted from the host at runtime by the Dagu DAG. This means:
- Changing a `.py` script takes effect on the next DAG run with no image rebuild needed
- Only dependency changes (pyproject.toml/uv.lock) require a new image build and CI push
