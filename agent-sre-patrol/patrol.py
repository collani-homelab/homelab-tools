"""
patrol.py — Autonomous SRE Patrol Agent

Runs every 15 minutes via Dagu. Collects health data from homelab-mcp,
extracts key metrics, asks a local 8B model to identify threshold breaches,
sends ntfy alerts for anything critical/warning, and indexes findings to RAG.
"""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Literal

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_SSE_URL  = os.getenv("MCP_SSE_URL",  "http://localhost:8083/sse")
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434/v1")
REPORTS_DIR  = os.getenv("REPORTS_DIR",  os.path.join(os.path.dirname(__file__), "reports"))
PATROL_MODEL = os.getenv("PATROL_MODEL", "hermes3:8b")
NOTIFY_SH    = os.getenv("NOTIFY_SH",    "")

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------
class Alert(BaseModel):
    severity: Literal["critical", "warning", "info"]
    component: str       # e.g. "dionysus RAM", "archive UPS"
    message: str         # human-readable one-liner
    observed_value: str  # e.g. "95.2% used"
    threshold: str       # e.g. ">90%"

class PatrolReport(BaseModel):
    alerts: list[Alert]
    healthy_components: list[str]
    one_line_summary: str   # ≤120 chars

# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------
def _parse_mcp(result) -> dict | list:
    try:
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return json.loads(text)
    except Exception:
        return {"error": "parse_failed"}


async def _call(session: ClientSession, tool: str, **kwargs) -> dict | list:
    try:
        result = await session.call_tool(tool, kwargs)
        return _parse_mcp(result)
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Data compression — extract only the numbers the LLM needs to check
# ---------------------------------------------------------------------------
def _safe(d: dict | list, *keys, default="unknown"):
    """Drill into nested dicts safely."""
    v = d
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k, default)
        if v is None:
            return default
    return v


def _extract_metrics(
    dionysus_array, dionysus_stats, dionysus_ups,
    archive_array,  archive_stats,  archive_ups,
    ram_prom, fs_prom,
) -> dict:
    """Return a compact dict of only the values we threshold-check."""

    def unraid_summary(name, array, stats, ups):
        arr      = array.get("array", {}) if not array.get("error") else {}
        metrics  = stats.get("metrics", {}) if not stats.get("error") else {}
        cpu      = metrics.get("cpu", {}).get("percentTotal")
        mem      = metrics.get("memory", {})

        disks = arr.get("disks", [])
        total_errors = sum(d.get("numErrors", 0) for d in disks)
        non_ok_disks = [d["name"] for d in disks if d.get("status") != "DISK_OK"]

        # Group disk fills: report only disks >85% full, capped at 5
        disk_fills = []
        for d in disks:
            fssize = d.get("fsSize") or 0
            fsfree = d.get("fsFree") or 0
            if fssize > 0:
                pct = round(100 * (1 - fsfree / fssize), 1)
                if pct > 85:
                    disk_fills.append({"disk": d["name"], "fill_pct": pct})
        disk_fills.sort(key=lambda x: x["fill_pct"], reverse=True)

        return {
            "node":              name,
            "array_state":       arr.get("state", "unknown"),
            "total_disk_errors": total_errors,
            "non_ok_disks":      non_ok_disks,
            "disks_over_85pct":  disk_fills[:5],
            "disks_over_85pct_count": len(disk_fills),
            "cpu_pct":           round(cpu, 1) if cpu is not None else "unknown",
            "ram_used_pct":      round(mem.get("percentTotal", 0), 1) if mem else "unknown",
            "ram_total_gb":      round(mem.get("total", 0) / 1e9, 1) if mem else "unknown",
            "ups_status":        ups.get("status", "error" if ups.get("error") else "unknown"),
            "ups_charge_pct":    ups.get("batteryCharge", "unknown"),
            "ups_runtime_min":   ups.get("estimatedRuntime", "unknown"),
            "ups_error":         ups.get("error"),
            "data_error":        array.get("error") or stats.get("error"),
        }

    def prom_fs_summary(prom_result):
        """Group filesystem fill by host, return hosts with disks >85%."""
        try:
            results = prom_result["data"]["result"]
            by_host: dict[str, list[float]] = {}
            for r in results:
                metric = r.get("metric", {})
                val = float(r["value"][1]) if isinstance(r.get("value"), list) else 0
                host = metric.get("instance", "unknown")
                by_host.setdefault(host, []).append(val)
            summary = []
            for host, vals in by_host.items():
                over = [round(v, 1) for v in vals if v > 85]
                if over:
                    summary.append({
                        "host": host,
                        "disks_over_85pct": len(over),
                        "max_fill_pct": max(over),
                    })
            summary.sort(key=lambda x: x["max_fill_pct"], reverse=True)
            return summary
        except Exception:
            return [{"error": str(prom_result)[:200]}]

    def prom_ram_summary(prom_result):
        try:
            results = prom_result["data"]["result"]
            out = []
            for r in results:
                host = r.get("metric", {}).get("instance", "unknown")
                val  = float(r["value"][1]) if isinstance(r.get("value"), list) else 0
                out.append({"host": host, "ram_used_pct": round(val, 1)})
            out.sort(key=lambda x: x["ram_used_pct"], reverse=True)
            return out
        except Exception:
            return [{"error": str(prom_result)[:200]}]

    return {
        "dionysus": unraid_summary("dionysus", dionysus_array, dionysus_stats, dionysus_ups),
        "archive":  unraid_summary("archive",  archive_array,  archive_stats,  archive_ups),
        "prometheus": {
            "ram_by_host":       prom_ram_summary(ram_prom),
            "fs_hosts_over_85":  prom_fs_summary(fs_prom),
        },
    }


# ---------------------------------------------------------------------------
# LLM analysis
# ---------------------------------------------------------------------------
def _check_thresholds(metrics: dict) -> tuple[list[Alert], list[str]]:
    """Pure Python threshold checks — no LLM required for severity."""
    alerts: list[Alert] = []
    healthy: list[str] = []

    for node_key in ("dionysus", "archive"):
        n = metrics[node_key]
        name = n["node"]

        # Node unreachable
        if n.get("data_error"):
            alerts.append(Alert(
                severity="warning", component=name,
                message=f"{name} data unavailable",
                observed_value=str(n["data_error"])[:120],
                threshold="reachable",
            ))
            continue

        # Array state
        state = n.get("array_state", "unknown").upper()
        if state not in ("STARTED", "NORMAL"):
            alerts.append(Alert(
                severity="critical", component=f"{name} array",
                message=f"Array state is not Started",
                observed_value=state,
                threshold="STARTED",
            ))
        else:
            healthy.append(f"{name} array ({state})")

        # Disk errors
        errs = n.get("total_disk_errors", 0)
        if errs:
            alerts.append(Alert(
                severity="warning", component=f"{name} disks",
                message=f"{errs} disk error(s) detected",
                observed_value=str(errs),
                threshold="0",
            ))

        non_ok = n.get("non_ok_disks", [])
        if non_ok:
            alerts.append(Alert(
                severity="critical", component=f"{name} disks",
                message=f"Disks not OK: {', '.join(non_ok[:5])}",
                observed_value=str(non_ok),
                threshold="all DISK_OK",
            ))

        # Disk fill
        count = n.get("disks_over_85pct_count", 0)
        if count:
            top = n.get("disks_over_85pct", [{}])
            max_fill = top[0].get("fill_pct", 0) if top else 0
            sev: Literal["critical", "warning", "info"] = "critical" if max_fill >= 98 else "warning"
            alerts.append(Alert(
                severity=sev, component=f"{name} storage",
                message=f"{count} disk(s) >85% full (highest {max_fill}%)",
                observed_value=f"{count} disks, max {max_fill}%",
                threshold=">85% fill",
            ))

        # RAM
        ram_pct = n.get("ram_used_pct", 0)
        if isinstance(ram_pct, (int, float)):
            if ram_pct > 95:
                alerts.append(Alert(
                    severity="critical", component=f"{name} RAM",
                    message=f"RAM critically high",
                    observed_value=f"{ram_pct}%",
                    threshold=">95%",
                ))
            elif ram_pct > 90:
                alerts.append(Alert(
                    severity="warning", component=f"{name} RAM",
                    message=f"RAM usage high",
                    observed_value=f"{ram_pct}%",
                    threshold=">90%",
                ))
            else:
                healthy.append(f"{name} RAM ({ram_pct}%)")

        # UPS
        ups_err = n.get("ups_error")
        ups_runtime = n.get("ups_runtime_min")
        ups_charge = n.get("ups_charge_pct")
        if ups_err:
            alerts.append(Alert(
                severity="warning", component=f"{name} UPS",
                message="UPS monitoring unavailable (apcaccess returned no data)",
                observed_value="unreachable",
                threshold="reachable",
            ))
        else:
            if isinstance(ups_runtime, (int, float)):
                if ups_runtime < 10:
                    alerts.append(Alert(severity="critical", component=f"{name} UPS",
                                        message="UPS runtime critically low",
                                        observed_value=f"{ups_runtime} min", threshold="<10 min"))
                elif ups_runtime < 15:
                    alerts.append(Alert(severity="warning", component=f"{name} UPS",
                                        message="UPS runtime low",
                                        observed_value=f"{ups_runtime} min", threshold="<15 min"))
                else:
                    healthy.append(f"{name} UPS ({ups_runtime} min)")
            if isinstance(ups_charge, (int, float)):
                if ups_charge < 25:
                    alerts.append(Alert(severity="critical", component=f"{name} UPS",
                                        message="UPS battery critically low",
                                        observed_value=f"{ups_charge}%", threshold="<25%"))
                elif ups_charge < 50:
                    alerts.append(Alert(severity="warning", component=f"{name} UPS",
                                        message="UPS battery low",
                                        observed_value=f"{ups_charge}%", threshold="<50%"))

    return alerts, healthy


SUMMARY_PROMPT = """\
Write a single line (≤120 chars) summarising the homelab SRE patrol findings.
Be direct and specific. List the most important issues first. If all clear, say "All systems nominal."
Return ONLY the summary string, no JSON, no quotes."""


async def _summarise(alerts: list[Alert], healthy: list[str]) -> str:
    if not alerts:
        return "All systems nominal"
    client = AsyncOpenAI(base_url=OLLAMA_URL, api_key="ollama")
    alert_text = "; ".join(
        f"[{a.severity.upper()}] {a.component}: {a.observed_value}"
        for a in alerts
    )
    response = await client.chat.completions.create(
        model=PATROL_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user",   "content": f"Alerts: {alert_text}"},
        ],
        temperature=0.1,
        max_tokens=60,
    )
    raw = (response.choices[0].message.content or "").strip().strip('"').strip("'")
    return raw[:120] or "Patrol complete — see alerts"


async def _analyse(metrics: dict, now: str) -> PatrolReport:
    alerts, healthy = _check_thresholds(metrics)
    summary = await _summarise(alerts, healthy)
    return PatrolReport(alerts=alerts, healthy_components=healthy, one_line_summary=summary)


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------
def _send_ntfy(title: str, tags: str, priority: str, message: str) -> None:
    if not os.path.exists(NOTIFY_SH):
        print(f"[WARN] notify.sh not found at {NOTIFY_SH}", file=sys.stderr)
        return
    subprocess.run(
        [NOTIFY_SH, title, tags, priority, message],
        capture_output=True, timeout=10,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[patrol] {now} — connecting to {MCP_SSE_URL}")

    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("[1/3] Collecting health data...")
            (
                dionysus_array, dionysus_stats, dionysus_ups,
                archive_array,  archive_stats,  archive_ups,
                ram_prom, fs_prom,
            ) = await asyncio.gather(
                _call(session, "get_unraid_array_status_dionysus"),
                _call(session, "get_unraid_system_stats_dionysus"),
                _call(session, "get_unraid_ups_status_dionysus"),
                _call(session, "get_unraid_array_status_archive"),
                _call(session, "get_unraid_system_stats_archive"),
                _call(session, "get_unraid_ups_status_archive"),
                _call(session, "query_promql",
                      query="100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"),
                _call(session, "query_promql",
                      query='100 * (1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} '
                            '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"})'),
            )

            metrics = _extract_metrics(
                dionysus_array, dionysus_stats, dionysus_ups,
                archive_array,  archive_stats,  archive_ups,
                ram_prom, fs_prom,
            )

            print(f"[2/3] Analysing with {PATROL_MODEL}...")
            report = await _analyse(metrics, now)

            criticals = [a for a in report.alerts if a.severity == "critical"]
            warnings  = [a for a in report.alerts if a.severity == "warning"]
            print(f"[3/3] {len(report.alerts)} alert(s) — {report.one_line_summary}")

            if criticals or warnings:
                sev = "CRITICAL" if criticals else "WARNING"
                body = "\n".join(
                    f"[{a.severity.upper()}] {a.component}: {a.message} ({a.observed_value})"
                    for a in (criticals + warnings)
                )
                _send_ntfy(
                    title=f"SRE Patrol {sev}: {report.one_line_summary}",
                    tags="rotating_light,computer" if criticals else "warning,computer",
                    priority="high" if criticals else "default",
                    message=body,
                )
                print(f"[patrol] ntfy sent — {sev}")
            else:
                print("[patrol] All clear")

            if report.alerts:
                doc = (
                    f"# SRE Patrol — {now}\n\n"
                    f"**Summary:** {report.one_line_summary}\n\n"
                    + "\n".join(
                        f"- **[{a.severity.upper()}] {a.component}:** {a.message} "
                        f"(observed: {a.observed_value}, threshold: {a.threshold})"
                        for a in report.alerts
                    )
                    + f"\n\n**Healthy:** {', '.join(report.healthy_components) or 'none listed'}"
                )
                # Write to shared reports dir for the web viewer
                os.makedirs(REPORTS_DIR, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
                report_path = os.path.join(REPORTS_DIR, f"{now[:10]}-sre-patrol-{ts[11:]}.md")
                with open(report_path, "w") as f:
                    f.write(doc)

                await _call(session, "index_document",
                            content=doc, source="sre-patrol/automated", type="artifact")
                print(f"[patrol] Finding written to {report_path} and indexed to RAG")

    if criticals:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
