"""
report.py — Weekly Homelab Storage Capacity Report

Runs Monday 08:00 via Dagu. Collects array and fill-rate data from
homelab-mcp, computes projected runway per node, diffs against last
week's RAG snapshot, and generates a markdown report. Sends ntfy
summary and indexes the report to RAG.
"""
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI

from shared import send_ntfy, parse_mcp_tool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_SSE_URL      = os.getenv("MCP_SSE_URL",       "http://localhost:8083/sse")
REPORT_MODEL     = os.getenv("REPORT_MODEL",       "phi4:14b")
WORKSTATION_URL  = os.getenv("WORKSTATION_OLLAMA", "http://localhost:11434/v1")
REPORTS_DIR      = os.getenv("REPORTS_DIR",        os.path.join(os.path.dirname(__file__), "reports"))
NOTIFY_SH        = os.getenv("NOTIFY_SH",          "")

os.makedirs(REPORTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class DiskInfo:
    name: str
    size_tb: float
    free_tb: float
    fill_pct: float
    errors: int
    status: str
    temp: int | None

@dataclass
class NodeStorage:
    name: str
    total_tb: float
    free_tb: float
    fill_pct: float
    array_state: str
    parity_status: str
    disks: list[DiskInfo] = field(default_factory=list)
    delta_7d_gb: float = 0.0    # negative = filling
    delta_24h_gb: float = 0.0
    runway_days: float | None = None  # None = not filling / stable

# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------
async def _call(session: ClientSession, tool: str, **kwargs) -> dict | list:
    try:
        return parse_mcp_tool(await session.call_tool(tool, kwargs))
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Data collection and computation
# ---------------------------------------------------------------------------
def _build_node(name: str, array_raw: dict, delta_7d: dict, delta_24h: dict) -> NodeStorage:
    arr = array_raw.get("array", {})
    disks_raw = arr.get("disks", [])

    # fsSize and fsFree from Unraid GraphQL are in 1 KB blocks
    KB = 1024
    TB = KB ** 3  # 1 TB in KB = 1,099,511,627,776 KB ≈ 1e9

    total_kb = sum(d.get("fsSize", 0) for d in disks_raw)
    free_kb  = sum(d.get("fsFree", 0) for d in disks_raw)
    total_tb = total_kb / 1e9
    free_tb  = free_kb / 1e9
    fill_pct = round(100 * (1 - free_kb / total_kb), 2) if total_kb else 0

    disks = []
    for d in sorted(disks_raw, key=lambda x: x.get("name", "")):
        fs  = d.get("fsSize", 0)
        ff  = d.get("fsFree", 0)
        dpct = round(100 * (1 - ff / fs), 1) if fs else 0
        disks.append(DiskInfo(
            name=d.get("name", "?"),
            size_tb=round(fs / 1e9, 1),
            free_tb=round(ff / 1e9, 2),
            fill_pct=dpct,
            errors=d.get("numErrors", 0),
            status=d.get("status", "UNKNOWN"),
            temp=d.get("temp"),
        ))

    parity = arr.get("parityCheckStatus", {}) or {}
    parity_str = parity.get("status", "N/A")

    # Fill deltas from Prometheus (already in GB)
    node_7d  = delta_7d.get(name, 0.0)
    node_24h = delta_24h.get(name, 0.0)

    # Runway: days until full at 7-day average fill rate
    runway = None
    if node_7d < 0:  # negative = filling
        daily_fill_gb = abs(node_7d) / 7
        free_gb = free_tb * 1000
        runway = round(free_gb / daily_fill_gb)

    return NodeStorage(
        name=name,
        total_tb=round(total_tb, 1),
        free_tb=round(free_tb, 2),
        fill_pct=fill_pct,
        array_state=arr.get("state", "UNKNOWN"),
        parity_status=parity_str,
        disks=disks,
        delta_7d_gb=round(node_7d, 1),
        delta_24h_gb=round(node_24h, 1),
        runway_days=runway,
    )


def _prom_disk_deltas(prom_result: dict, window_label: str) -> dict[str, float]:
    """Aggregate per-disk delta into per-node totals (GB). negative=filling."""
    totals: dict[str, float] = {}
    for r in prom_result.get("data", {}).get("result", []):
        metric = r.get("metric", {})
        val = float(r["value"][1]) if isinstance(r.get("value"), list) else 0.0
        inst = metric.get("instance", "")
        mnt  = metric.get("mountpoint", "")
        if "mnt/disk" not in mnt:
            continue
        node = "dionysus" if "115" in inst else "archive" if "189" in inst else None
        if node:
            totals[node] = totals.get(node, 0.0) + val
    return totals


def _prom_disk_breakdown(prom_7d: dict) -> dict[str, list[dict]]:
    """Per-disk 7d delta by node, top movers only."""
    by_node: dict[str, list] = {}
    for r in prom_7d.get("data", {}).get("result", []):
        metric = r.get("metric", {})
        val = float(r["value"][1]) if isinstance(r.get("value"), list) else 0.0
        inst = metric.get("instance", "")
        mnt  = metric.get("mountpoint", "")
        if "mnt/disk" not in mnt or abs(val) < 0.5:
            continue
        node = "dionysus" if "115" in inst else "archive" if "189" in inst else None
        if node:
            by_node.setdefault(node, []).append({
                "disk": mnt.split("/")[-1],
                "delta_7d_gb": round(val, 1),
            })
    for node in by_node:
        by_node[node].sort(key=lambda x: x["delta_7d_gb"])  # most filling first
    return by_node


# ---------------------------------------------------------------------------
# LLM report generation
# ---------------------------------------------------------------------------
REPORT_PROMPT = """\
You are a senior SRE writing a weekly homelab storage capacity report. Write clean markdown (no fenced code blocks).

DATA FIELD NOTES:
- consumed_7d_gb: POSITIVE = node consumed that much space this week (filling). NEGATIVE = space was freed.
- actively_filling: true if the node consumed space net over the last 7 days.
- runway_days: estimated days until array is full at current 7-day fill rate (null if stable/freeing).
- top_consuming_disks: the individual disks that consumed the most space this week.

Include these sections (use ## headings):

## Executive Summary
One paragraph: key finding and any urgent action.

## Capacity Overview
Table: | Node | Total TB | Free TB | Fill % | Consumed 7d (GB) | Runway |

## Active Fill Analysis
Which nodes/disks are filling and at what rate. If runway_days is set, state the projected date to array-full.

## Disk Health
Disk errors, non-OK disks, parity status. If all clear, say so.

## Comparison to Previous Week
Diff capacity numbers against prior_week_snapshot if available. If not, note this is the first report.

## Recommendations
Concrete bullet actions ranked by urgency. Reference specific disks and node names.

Keep it under 600 words. Use exact numbers from the data."""


async def _generate_report(dionysus: NodeStorage, archive: NodeStorage,
                           disk_breakdown: dict, prior_snapshot: str, now: str) -> str:
    data_summary = {
        "report_date": now,
        "nodes": [
            {
                "name": n.name,
                "total_tb": n.total_tb,
                "free_tb": n.free_tb,
                "fill_pct": n.fill_pct,
                "array_state": n.array_state,
                "parity_status": n.parity_status,
                # consumed_7d_gb: POSITIVE means consuming space (filling).
                # NEGATIVE means space was freed (data deleted/moved off).
                "consumed_7d_gb": round(-n.delta_7d_gb, 1),
                "consumed_24h_gb": round(-n.delta_24h_gb, 1),
                "runway_days": n.runway_days,
                "actively_filling": n.delta_7d_gb < 0,
                "disk_errors_total": sum(d.errors for d in n.disks),
                "non_ok_disks": [d.name for d in n.disks if d.status != "DISK_OK"],
                # top_consuming_disks: positive consumed_7d_gb = disk is filling
                "top_consuming_disks": [
                    {"disk": e["disk"], "consumed_7d_gb": round(-e["delta_7d_gb"], 1)}
                    for e in disk_breakdown.get(n.name, [])
                    if e["delta_7d_gb"] < 0
                ][:5],
                "hottest_disks": sorted(
                    [{"name": d.name, "temp": d.temp} for d in n.disks if d.temp],
                    key=lambda x: x["temp"], reverse=True
                )[:3],
            }
            for n in (dionysus, archive)
        ],
        "prior_week_snapshot": prior_snapshot or "none (first report)",
    }

    client = AsyncOpenAI(base_url=WORKSTATION_URL, api_key="ollama")
    response = await client.chat.completions.create(
        model=REPORT_MODEL,
        messages=[
            {"role": "system", "content": REPORT_PROMPT},
            {"role": "user",   "content": json.dumps(data_summary, indent=2)},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def _collect(session: ClientSession) -> tuple:
    """Collect all MCP data in one session before closing it."""
    return await asyncio.gather(
        _call(session, "get_unraid_array_status_dionysus"),
        _call(session, "get_unraid_array_status_archive"),
        _call(session, "query_promql",
              query="delta(node_filesystem_avail_bytes"
                    "{fstype!~\"tmpfs|overlay|squashfs\",job=\"unraid-nodes\"}[7d])"
                    " / 1024 / 1024 / 1024"),
        _call(session, "query_promql",
              query="delta(node_filesystem_avail_bytes"
                    "{fstype!~\"tmpfs|overlay|squashfs\",job=\"unraid-nodes\"}[24h])"
                    " / 1024 / 1024 / 1024"),
        _call(session, "query_knowledge",
              query="weekly storage capacity report", type="artifact"),
    )


async def _index(session: ClientSession, date_label: str, report_md: str) -> None:
    await _call(
        session, "index_document",
        content=f"Weekly Storage Capacity Report — {date_label}\n\n{report_md}",
        source=f"storage-report/{date_label}",
        type="artifact",
    )


async def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[storage-report] {now} — connecting to {MCP_SSE_URL}")

    # Phase 1: collect all data and close MCP session before the slow LLM call
    print("[1/4] Collecting storage data...")
    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            (dionysus_array, archive_array,
             delta_7d_raw, delta_24h_raw, prior_rag) = await _collect(session)

    delta_7d       = _prom_disk_deltas(delta_7d_raw,  "7d")
    delta_24h      = _prom_disk_deltas(delta_24h_raw, "24h")
    disk_breakdown = _prom_disk_breakdown(delta_7d_raw)
    dionysus       = _build_node("dionysus", dionysus_array, delta_7d, delta_24h)
    archive        = _build_node("archive",  archive_array,  delta_7d, delta_24h)

    prior_snapshot = ""
    if isinstance(prior_rag, dict) and "results" in prior_rag:
        snippets = [r.get("content", "") for r in prior_rag["results"][:2]]
        prior_snapshot = "\n\n".join(snippets)[:2000]

    print(f"      Dionysus: {dionysus.total_tb:.0f} TB, {dionysus.free_tb:.1f} TB free, 7d {dionysus.delta_7d_gb:+.0f} GB")
    print(f"      Archive:  {archive.total_tb:.0f} TB, {archive.free_tb:.1f} TB free, 7d {archive.delta_7d_gb:+.0f} GB")

    # Phase 2: LLM report — MCP session is closed, no keepalive pressure
    print(f"[2/4] Generating report with {REPORT_MODEL}...")
    try:
        report_md = await _generate_report(dionysus, archive, disk_breakdown, prior_snapshot, now)
    except Exception as e:
        print(f"[WARN] LLM report generation failed: {e}", file=sys.stderr)
        report_md = (
            f"## Capacity Overview\n\n"
            f"| Node | Total TB | Free TB | Fill % |\n|------|----------|---------|--------|\n"
            f"| dionysus | {dionysus.total_tb} | {dionysus.free_tb} | {dionysus.fill_pct}% |\n"
            f"| archive  | {archive.total_tb}  | {archive.free_tb}  | {archive.fill_pct}% |\n\n"
            f"_LLM generation failed: {e}_"
        )

    # Phase 3: save report
    print("[3/4] Saving report...")
    report_path = os.path.join(REPORTS_DIR, f"{date_label}-storage-capacity.md")
    with open(report_path, "w") as f:
        f.write(f"# Weekly Storage Capacity Report — {date_label}\n\n")
        f.write(report_md)
    print(f"      Saved to {report_path}")

    # Phase 4: notify + index (new MCP session)
    print("[4/4] Sending notification and indexing to RAG...")
    urgency = []
    if archive.runway_days and archive.runway_days < 90:
        urgency.append(f"Archive {archive.runway_days}d runway")
    if dionysus.runway_days and dionysus.runway_days < 90:
        urgency.append(f"Dionysus {dionysus.runway_days}d runway")
    if not urgency:
        urgency.append("stable")

    ntfy_body = (
        f"Dionysus: {dionysus.free_tb:.1f} TB free ({dionysus.fill_pct:.1f}%), 7d {dionysus.delta_7d_gb:+.0f} GB\n"
        f"Archive:  {archive.free_tb:.1f} TB free ({archive.fill_pct:.1f}%), 7d {archive.delta_7d_gb:+.0f} GB\n"
        f"Status: {', '.join(urgency)}"
    )
    priority = "high" if any(r for r in [archive.runway_days, dionysus.runway_days]
                             if r is not None and r < 30) else "default"
    send_ntfy(
        NOTIFY_SH,
        title=f"Storage Report: {', '.join(urgency)}",
        tags="floppy_disk,chart_with_upwards_trend",
        priority=priority,
        message=ntfy_body,
    )

    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await _index(session, date_label, report_md)

    print(f"[storage-report] Done. Report at {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
