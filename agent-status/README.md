# agent-status

A homelab CLI tool that queries live infrastructure data across the network and produces a structured JSON status report with an LLM-generated summary.

## What it does

- Connects to [`homelab-mcp`](http://your-server-ip:8083) over SSE to fetch UniFi infrastructure devices and all connected clients
- Queries Prometheus (`:9091`) for live metrics scrape status and node uptimes
- Queries Loki (`:3100`) to check log shipping status per host
- Assembles a structured JSON report covering:
  - **8 infrastructure devices** — servers (Unraid, SRE), gateway, switch, APs — each with `logs / metrics / otel` observability status
  - **All network clients** — wired and wireless stations with hostname, IP, VLAN, uplink, RSSI, satisfaction score, and uptime
- Uses a local LLM (`ollama/qwen2.5-coder:14b` via LiteLLM proxy at `:4000`) to generate a 3-5 sentence executive summary

## Prerequisites

| Requirement | Value |
|---|---|
| Python | ≥ 3.14 |
| Package manager | [`uv`](https://docs.astral.sh/uv/) |
| MCP server | `homelab-mcp` running at `http://your-server-ip:8083/sse` |
| LiteLLM proxy | Running at `http://your-server-ip:4000/v1` with `ollama/qwen2.5-coder:14b` |
| Prometheus | Running at `http://your-server-ip:9091` |
| Loki | Running at `http://your-server-ip:3100` |

## Running

```bash
cd homelab/Tools/agent-status
uv run agent_status.py
```

Output is printed to stdout as formatted JSON. You can pipe it:

```bash
uv run agent_status.py | jq '.devices[] | {name, metrics, logs, otel}'
uv run agent_status.py | jq '.clients | length'
uv run agent_status.py | jq '[.clients[] | select(.connection_type == "wireless")] | sort_by(.satisfaction)'
```

## Output Schema

```jsonc
{
  "summary": "LLM-generated narrative...",
  "devices": [
    {
      "name": "homelab-sre",
      "mac": "",
      "device_type": "server",        // server | udm | usw | uap
      "version": "Linux",
      "ip_address": "your-server-ip",
      "uptime": "9d 20h 12m 57s",
      "state": "running",
      "interfaces": [],
      "logs": "working",              // working | not_working | not_configured | n/a
      "metrics": "working",
      "otel": "working"
    }
    // ...
  ],
  "clients": [
    {
      "hostname": "boys-pcp",
      "mac": "70:d8:c2:14:8e:e4",
      "ip_address": "your-server-ip",
      "connection_type": "wired",     // wired | wireless
      "uplink": "USW Pro 24 PoE",
      "vlan": "Default",
      "signal_rssi": null,            // dBm, wireless only
      "satisfaction": 100,            // 0–100 UniFi experience score
      "uptime": "22h 58m 38s"
    }
    // ...
  ]
}
```

## Observability Status Logic

| Device type | Logs | Metrics | OTel |
|---|---|---|---|
| UniFi APs / Switch / Gateway | `n/a` | `n/a` | `n/a` |
| Unraid nodes (Dionysus, Archive) | `not_configured` | live Prometheus check | `n/a` |
| SRE machine (`your-server-ip`) | `working` | live Prometheus check | `working` |

Metrics status is determined by querying the live `/api/v1/targets` endpoint — a node is `working` if at least one target for its IP (or a known Docker job like `node-exporter` / `cadvisor`) is healthy.

## Architecture

```
Phase 1  MCP SSE     → get_unifi_devices + get_unifi_clients
Phase 2  Prometheus  → /api/v1/targets (which IPs are scraped and healthy)
Phase 3  Loki        → /loki/api/v1/label/host/values (log shipping check)
Phase 4  Prometheus  → node_boot_time_seconds (real uptime for server nodes)
Phase 5  Python      → deterministic parse into Pydantic models
Phase 6  LLM         → free-text summary only (no schema pressure)
```

The two-phase pattern (data collection deterministic in Python, LLM only for narrative) is the recommended approach for local models. See [`homelab-platform/docs/golden_paths/hybrid_ai_patterns.md`](../../../homelab-platform/docs/golden_paths/hybrid_ai_patterns.md) for the full pattern reference.

### Configuration

Infrastructure lists (Unraid nodes, static nodes, OTel/Loki IPs) must be configured in a `config.yaml` file located in the same directory as the script.

## Dependencies

Managed via `uv`. Install and sync automatically on first `uv run`:

```
pydantic-ai >= 1.102.0
mcp          >= 1.27.1
aiohttp
python-dotenv
pyyaml       >= 6.0.2
```

## MCP Server

The `get_unifi_clients` tool (hitting `stat/sta`) was added to `homelab-mcp` as part of this project. If you redeploy `homelab-mcp`, ensure `MCP_TRANSPORT=sse` is set — it is now included in `homelab-mcp/.env`.
