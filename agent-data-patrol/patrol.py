"""
patrol.py — Data Patrol Agent

Detects anomalies from raw telemetry: z-score over Prometheus time-series,
error-rate spike detection in Loki logs, and ERROR span counts from Phoenix.
No LLM in the decision loop — the model only generates a plain-English
summary after severity is determined.

Runs every 30 min alongside agent-vision-patrol to support a head-to-head
comparison of detection approaches (Project #11).
"""
import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_SSE_URL      = os.getenv("MCP_SSE_URL",      "http://localhost:8083/sse")
OLLAMA_URL       = os.getenv("OLLAMA_URL",        "http://localhost:11434/v1")
PATROL_MODEL     = os.getenv("PATROL_MODEL",      "hermes3:8b")
NOTIFY_SH        = os.getenv("NOTIFY_SH",         "")
REPORTS_DIR      = Path(os.getenv("REPORTS_DIR",  str(Path(__file__).parent / "reports")))
PHOENIX_PROJECT  = os.getenv("PHOENIX_PROJECT",   "default")
ZSCORE_THRESHOLD = float(os.getenv("ZSCORE_THRESHOLD",     "2.5"))
LOG_SPIKE_RATIO  = float(os.getenv("LOG_SPIKE_RATIO",      "3.0"))
LOG_ABS_THRESHOLD = int(os.getenv("LOG_ABS_THRESHOLD",    "20"))
MIN_STDDEV       = float(os.getenv("MIN_STDDEV",           "0.1"))

LOKI_ERROR_QUERY = '{job=~".+"} |~ "(?i)\\b(error|fatal|panic|oom killed)\\b"'

# Prometheus metrics to monitor
@dataclass
class MetricSpec:
    name: str       # machine-friendly slug
    display: str    # human-readable label for alerts/reports
    expr: str       # PromQL instant expression
    unit: str       # display unit (e.g. "%" or "bytes/s")

METRICS = [
    MetricSpec(
        name="sre-cpu",
        display="SRE CPU busy",
        expr='100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle",job="node-exporter"}[5m])))',
        unit="%",
    ),
    MetricSpec(
        name="sre-ram",
        display="SRE RAM used",
        expr='100 * (1 - avg(node_memory_MemAvailable_bytes{job="node-exporter"}) / avg(node_memory_MemTotal_bytes{job="node-exporter"}))',
        unit="%",
    ),
    MetricSpec(
        name="sre-net-rx",
        display="SRE network RX",
        expr='sum(rate(node_network_receive_bytes_total{job="node-exporter",device!="lo"}[5m]))',
        unit="bytes/s",
    ),
    MetricSpec(
        name="dionysus-ram",
        display="Dionysus RAM used",
        expr='100 * (1 - node_memory_MemAvailable_bytes{instance="192.168.99.115:9100"} / node_memory_MemTotal_bytes{instance="192.168.99.115:9100"})',
        unit="%",
    ),
    MetricSpec(
        name="archive-ram",
        display="Archive RAM used",
        expr='100 * (1 - node_memory_MemAvailable_bytes{instance="192.168.99.189:9100"} / node_memory_MemTotal_bytes{instance="192.168.99.189:9100"})',
        unit="%",
    ),
]

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------
@dataclass
class Anomaly:
    source: Literal["prometheus", "loki", "phoenix"]
    component: str
    detail: str     # one-line description with values
    zscore: float | None = None

# ---------------------------------------------------------------------------
# Shared MCP helpers
# ---------------------------------------------------------------------------
async def _call(session: ClientSession, tool: str, **kwargs) -> dict | list:
    try:
        result = await session.call_tool(tool, kwargs)
        import json
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}


def _parse_scalar(result: dict | list) -> float | None:
    """Extract a single numeric value from a Prometheus instant query result."""
    if isinstance(result, dict) and result.get("error"):
        return None
    try:
        data = result["data"]
        if data["resultType"] == "scalar":
            return float(data["result"][1])
        if data["resultType"] == "vector":
            vals = [float(r["value"][1]) for r in data["result"] if r.get("value")]
            return sum(vals) / len(vals) if vals else None
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def _count_loki_lines(result: dict | list) -> int:
    """Count total log lines returned by a Loki query_range result."""
    if isinstance(result, dict) and result.get("error"):
        return 0
    try:
        streams = result["data"]["result"]
        return sum(len(s.get("values", [])) for s in streams)
    except (KeyError, TypeError):
        return 0


def _send_ntfy(title: str, tags: str, priority: str, message: str) -> None:
    if not NOTIFY_SH or not os.path.exists(NOTIFY_SH):
        print(f"[WARN] notify.sh not found at {NOTIFY_SH!r}", file=sys.stderr)
        return
    subprocess.run([NOTIFY_SH, title, tags, priority, message], capture_output=True, timeout=10)


# ---------------------------------------------------------------------------
# Prometheus z-score check
# ---------------------------------------------------------------------------
def _avg_query(expr: str) -> str:
    return f"avg_over_time(({expr})[1h:5m])"

def _std_query(expr: str) -> str:
    return f"stddev_over_time(({expr})[1h:5m])"


async def check_prometheus(session: ClientSession) -> tuple[list[Anomaly], list[str]]:
    """Z-score anomaly detection over METRICS. Returns (anomalies, healthy_labels)."""
    n = len(METRICS)
    # Fetch current value, 1h mean, and 1h stddev for every metric in parallel
    all_results = await asyncio.gather(*[
        _call(session, "query_promql", query=m.expr)          for m in METRICS
    ], *[
        _call(session, "query_promql", query=_avg_query(m.expr)) for m in METRICS
    ], *[
        _call(session, "query_promql", query=_std_query(m.expr)) for m in METRICS
    ])

    currents = all_results[:n]
    means    = all_results[n:2*n]
    stds     = all_results[2*n:]

    anomalies: list[Anomaly] = []
    healthy: list[str] = []

    for i, m in enumerate(METRICS):
        cur = _parse_scalar(currents[i])
        avg = _parse_scalar(means[i])
        std = _parse_scalar(stds[i])

        if cur is None:
            print(f"  [prom/{m.name}] no data", file=sys.stderr)
            continue

        val_str = f"{cur:.1f}{m.unit}" if m.unit != "bytes/s" else f"{cur/1024:.1f} KB/s"

        if avg is None or std is None or std < MIN_STDDEV:
            # Not enough history or metric is fully stable — use it as healthy signal
            healthy.append(f"{m.display}: {val_str} (no baseline)")
            continue

        zscore = (cur - avg) / std
        if abs(zscore) > ZSCORE_THRESHOLD:
            direction = "spike" if zscore > 0 else "drop"
            avg_str = f"{avg:.1f}{m.unit}" if m.unit != "bytes/s" else f"{avg/1024:.1f} KB/s"
            anomalies.append(Anomaly(
                source="prometheus",
                component=m.display,
                detail=f"{m.display} {direction}: {val_str} (z={zscore:+.1f}, baseline {avg_str}±{std:.1f})",
                zscore=zscore,
            ))
        else:
            healthy.append(f"{m.display}: {val_str} (z={zscore:+.1f})")

    return anomalies, healthy


# ---------------------------------------------------------------------------
# Loki error-rate check
# ---------------------------------------------------------------------------
async def check_loki(session: ClientSession) -> list[Anomaly]:
    """Flag error-log spikes: current 5-min window vs per-5-min average over 1h."""
    result_5m, result_1h = await asyncio.gather(
        _call(session, "query_logql", query=LOKI_ERROR_QUERY, lookback="5m"),
        _call(session, "query_logql", query=LOKI_ERROR_QUERY, lookback="1h"),
    )

    count_5m = _count_loki_lines(result_5m)
    count_1h = _count_loki_lines(result_1h)
    baseline_per_5m = count_1h / 12  # 12 × 5-min windows in 1h

    print(f"  [loki] errors: {count_5m} (last 5m), baseline {baseline_per_5m:.0f}/5m")

    if count_5m > LOG_ABS_THRESHOLD:
        return [Anomaly(
            source="loki",
            component="log errors",
            detail=f"Error log count high: {count_5m} errors in last 5m (absolute threshold {LOG_ABS_THRESHOLD})",
        )]
    if baseline_per_5m > 0 and count_5m > LOG_SPIKE_RATIO * baseline_per_5m:
        return [Anomaly(
            source="loki",
            component="log errors",
            detail=f"Error log spike: {count_5m} errors in last 5m vs baseline {baseline_per_5m:.0f}/5m ({count_5m/baseline_per_5m:.1f}×)",
        )]
    return []


# ---------------------------------------------------------------------------
# Phoenix error check
# ---------------------------------------------------------------------------
async def check_phoenix(session: ClientSession) -> list[Anomaly]:
    """Flag any ERROR spans in Phoenix in the last 30 minutes."""
    result = await _call(session, "get_phoenix_span_errors",
                         project=PHOENIX_PROJECT, lookback="30m", limit=10)
    if result.get("error"):
        print(f"  [phoenix] query error: {result['error']}", file=sys.stderr)
        return []

    spans = result.get("spans", [])
    count = result.get("count", len(spans))
    print(f"  [phoenix] {count} ERROR span(s) in last 30m")

    if count == 0:
        return []

    models = {s.get("attributes", {}).get("llm.model_name", "unknown") for s in spans}
    return [Anomaly(
        source="phoenix",
        component="LLM traces",
        detail=f"{count} ERROR span(s) in Phoenix last 30m (models: {', '.join(sorted(models))})",
    )]


# ---------------------------------------------------------------------------
# LLM narrative summary
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """\
Write a single line (≤150 chars) summarising these homelab anomalies detected via statistical analysis.
Be specific: include the metric names and direction (spike/drop). If all clear, say "All systems nominal."
Return ONLY the summary string, no JSON, no quotes."""


async def _summarize(anomalies: list[Anomaly], healthy: list[str]) -> str:
    if not anomalies:
        return "All systems nominal"
    try:
        client = AsyncOpenAI(base_url=OLLAMA_URL, api_key="ollama")
        details = "; ".join(a.detail for a in anomalies)
        response = await client.chat.completions.create(
            model=PATROL_MODEL,
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user",   "content": f"Anomalies: {details}"},
            ],
            temperature=0.1,
            max_tokens=80,
        )
        raw = (response.choices[0].message.content or "").strip().strip('"\'')
        return raw[:150] or f"{len(anomalies)} anomaly/anomalies detected"
    except Exception as e:
        print(f"[WARN] LLM summary failed: {e}", file=sys.stderr)
        return f"{len(anomalies)} anomaly/anomalies detected — check report"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[data-patrol] {now} — connecting to {MCP_SSE_URL}")

    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("[1/3] Fetching telemetry (Prometheus × Loki × Phoenix)...")
            (prom_anomalies, healthy), loki_anomalies, phoenix_anomalies = await asyncio.gather(
                check_prometheus(session),
                check_loki(session),
                check_phoenix(session),
            )

            all_anomalies = prom_anomalies + loki_anomalies + phoenix_anomalies
            print(f"[2/3] {len(all_anomalies)} anomaly/anomalies — summarising...")
            summary = await _summarize(all_anomalies, healthy)
            print(f"[3/3] {summary}")

            # Report (always written — useful for Dagu click-through and compare.py)
            report_lines = [
                f"# Data Patrol — {now}\n",
                f"**Summary:** {summary}\n",
            ]
            if all_anomalies:
                report_lines.append("## Anomalies\n")
                for a in all_anomalies:
                    zscore_str = f" (z={a.zscore:+.1f})" if a.zscore is not None else ""
                    report_lines.append(f"- **[{a.source.upper()}]** {a.detail}{zscore_str}")
                report_lines.append("")
            report_lines.append("## Healthy signals\n")
            for h in healthy:
                report_lines.append(f"- {h}")

            report_text = "\n".join(report_lines)
            report_path = REPORTS_DIR / "latest_report.md"
            report_path.write_text(report_text)
            print(f"[data-patrol] report → {report_path}")

            # Ntfy alert
            if all_anomalies:
                body = "\n".join(f"[{a.source.upper()}] {a.detail}" for a in all_anomalies)
                _send_ntfy(
                    title=f"Data Patrol: {summary[:80]}",
                    tags="chart_with_upwards_trend,rotating_light",
                    priority="default",
                    message=body,
                )
                print("[data-patrol] ntfy sent")
            else:
                print("[data-patrol] all clear")

    if any(a.zscore is not None and abs(a.zscore) > ZSCORE_THRESHOLD * 1.5 for a in prom_anomalies):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
