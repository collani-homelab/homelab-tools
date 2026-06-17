"""
health.py — Daily Media Pipeline Health Check

Runs daily at 09:00 via Dagu (after overnight download window). Checks
NZBGet for failures/pauses, Sonarr/Radarr/Lidarr queues for stuck items,
Lidarr wanted/missing counts, and Tautulli for transcode load and session
errors. Sends ntfy on issues and writes a report.
"""
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from shared import send_ntfy, parse_mcp_tool, parse_mcp_resource

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_SSE_URL  = os.getenv("MCP_SSE_URL",  "http://localhost:8083/sse")
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434/v1")
HEALTH_MODEL = os.getenv("HEALTH_MODEL", "hermes3:8b")
REPORTS_DIR  = os.getenv("REPORTS_DIR",  os.path.join(os.path.dirname(__file__), "reports"))
NOTIFY_SH    = os.getenv("NOTIFY_SH",    "")

# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------
async def _resource(session: ClientSession, uri: str) -> dict | list:
    try:
        return parse_mcp_resource(await session.read_resource(uri))
    except Exception as e:
        return {"error": str(e)}


async def _tool(session: ClientSession, name: str, **kwargs) -> dict | list:
    try:
        return parse_mcp_tool(await session.call_tool(name, kwargs))
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Threshold checks — pure Python, no LLM
# ---------------------------------------------------------------------------
class Issue(BaseModel):
    severity: Literal["critical", "warning", "info"]
    component: str
    message: str
    detail: str = ""


def _check_nzbget(status: dict, history: dict, active: dict) -> list[Issue]:
    issues: list[Issue] = []
    st = status.get("result", {}) if isinstance(status.get("result"), dict) else {}

    if st.get("DownloadPaused"):
        issues.append(Issue(severity="warning", component="NZBGet",
                            message="Downloads are paused",
                            detail="DownloadPaused=true — may be intentional or stuck"))

    free_mb = st.get("FreeDiskSpaceMB", 0)
    if isinstance(free_mb, (int, float)):
        free_gb = free_mb / 1024
        if free_gb < 50:
            issues.append(Issue(severity="critical", component="NZBGet disk",
                                message=f"Destination disk critically low: {free_gb:.0f} GB free",
                                detail="NZBGet will pause downloads when disk is full"))
        elif free_gb < 200:
            issues.append(Issue(severity="warning", component="NZBGet disk",
                                message=f"Destination disk low: {free_gb:.0f} GB free",
                                detail=""))

    # History failures
    hist_items = history.get("result", []) if isinstance(history, dict) else []
    failed = [h for h in hist_items if h.get("Status", "").upper() == "FAILURE"]
    if failed:
        names = ", ".join(h.get("NZBName", "?")[:50] for h in failed[:3])
        issues.append(Issue(severity="warning", component="NZBGet history",
                            message=f"{len(failed)} failed download(s) in recent history",
                            detail=names + ("..." if len(failed) > 3 else "")))

    # Active stuck (paused individual item)
    active_items = active.get("result", []) if isinstance(active, dict) else []
    paused = [a for a in active_items if a.get("Status", "").upper() == "PAUSED"]
    if paused:
        issues.append(Issue(severity="info", component="NZBGet active",
                            message=f"{len(paused)} item(s) individually paused in queue",
                            detail=", ".join(a.get("NZBName", "?")[:40] for a in paused[:3])))

    return issues


def _check_arr_queue(name: str, queue: dict) -> list[Issue]:
    issues: list[Issue] = []
    if isinstance(queue, dict) and queue.get("error"):
        issues.append(Issue(severity="warning", component=name,
                            message=f"{name} unreachable", detail=str(queue["error"])[:120]))
        return issues

    records = queue.get("records", []) if isinstance(queue, dict) else []
    total = queue.get("totalRecords", 0) if isinstance(queue, dict) else 0

    error_items = []
    for r in records:
        status_msgs = r.get("statusMessages", [])
        tracker_msgs = [m.get("title", "") for m in status_msgs if m.get("title")]
        if any("error" in m.lower() or "warning" in m.lower() for m in tracker_msgs):
            error_items.append(r.get("title", r.get("series", {}).get("title", "?")))

    if error_items:
        issues.append(Issue(severity="warning", component=name,
                            message=f"{len(error_items)} queue item(s) have tracker errors",
                            detail=", ".join(str(t)[:40] for t in error_items[:3])))

    return issues


def _check_lidarr(queue: dict, wanted: dict) -> list[Issue]:
    issues: list[Issue] = _check_arr_queue("Lidarr", queue)

    if isinstance(wanted, dict) and wanted.get("error"):
        issues.append(Issue(severity="warning", component="Lidarr wanted",
                            message="Lidarr wanted/missing unreachable",
                            detail=str(wanted["error"])[:120]))
        return issues

    total = wanted.get("totalRecords", 0) if isinstance(wanted, dict) else 0
    if total > 100:
        records = wanted.get("records", []) if isinstance(wanted, dict) else []
        titles = ", ".join(r.get("title", "?")[:40] for r in records[:3])
        issues.append(Issue(severity="critical", component="Lidarr wanted",
                            message=f"{total} album(s) missing/wanted",
                            detail=titles + ("..." if total > 3 else "")))
    elif total > 20:
        issues.append(Issue(severity="warning", component="Lidarr wanted",
                            message=f"{total} album(s) missing/wanted",
                            detail=""))

    return issues


def _check_tautulli(activity: dict) -> tuple[list[Issue], dict]:
    issues: list[Issue] = []
    stats: dict = {}

    data = activity.get("response", {}).get("data", {}) if isinstance(activity, dict) else {}
    sessions = data.get("sessions", [])
    total = len(sessions)
    transcodes = sum(1 for s in sessions if s.get("transcode_decision") == "transcode")
    directs = total - transcodes

    stats = {"total_sessions": total, "transcoding": transcodes, "direct_play": directs}

    if total > 0:
        ratio = transcodes / total
        if ratio > 0.6:
            issues.append(Issue(severity="warning", component="Plex transcodes",
                                message=f"{transcodes}/{total} sessions transcoding ({ratio*100:.0f}%)",
                                detail="High CPU load — check Plex quality profiles or client compatibility"))
        elif ratio > 0.3:
            issues.append(Issue(severity="info", component="Plex transcodes",
                                message=f"{transcodes}/{total} sessions transcoding ({ratio*100:.0f}%)",
                                detail=""))

    errored = [s for s in sessions if s.get("state") == "error"]
    if errored:
        titles = ", ".join(
            (s.get("full_title") or s.get("title") or "?")[:40] for s in errored[:3]
        )
        issues.append(Issue(severity="warning", component="Plex sessions",
                            message=f"{len(errored)} session(s) in error state",
                            detail=titles))

    return issues, stats


# ---------------------------------------------------------------------------
# LLM summary
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """\
Write a single concise line (≤120 chars) summarising a media pipeline health check.
List the most important issues first. If all clear, say "Media pipeline healthy — no issues detected."
Return ONLY the summary string, no JSON, no quotes."""


async def _summarise(issues: list[Issue], stats: dict) -> str:
    if not issues:
        return "Media pipeline healthy — no issues detected"
    client = AsyncOpenAI(base_url=OLLAMA_URL, api_key="ollama")
    issue_text = "; ".join(f"[{i.severity.upper()}] {i.component}: {i.message}" for i in issues)
    response = await client.chat.completions.create(
        model=HEALTH_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user",   "content": f"Issues: {issue_text}"},
        ],
        temperature=0.1,
        max_tokens=60,
    )
    return (response.choices[0].message.content or "").strip().strip('"')[:120]


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def _build_report(now: str, issues: list[Issue], tautulli_stats: dict,
                  nzbget_st: dict, radarr_q: dict, sonarr_q: dict,
                  lidarr_q: dict, lidarr_wanted: dict, summary: str) -> str:
    lines = [f"# Media Pipeline Health — {now[:10]}\n",
             f"**Summary:** {summary}\n"]

    lines.append("\n## Pipeline Status\n")
    nzbget_res = nzbget_st.get("result", {}) if isinstance(nzbget_st.get("result"), dict) else {}
    free_gb = nzbget_res.get("FreeDiskSpaceMB", 0) / 1024
    day_mb = nzbget_res.get("DaySizeMB", 0)
    rate = nzbget_res.get("DownloadRate", 0)
    paused = nzbget_res.get("DownloadPaused", False)
    standby = nzbget_res.get("ServerStandBy", True)
    lidarr_wanted_total = lidarr_wanted.get("totalRecords", "?") if isinstance(lidarr_wanted, dict) else "?"

    lines.append(f"| Component | Status |\n|-----------|--------|\n"
                 f"| NZBGet | {'⏸ Paused' if paused else ('💤 Idle' if standby else f'⬇ {rate/1024:.0f} KB/s')} |\n"
                 f"| NZBGet disk free | {free_gb:.0f} GB |\n"
                 f"| Downloaded today | {day_mb:.0f} MB |\n"
                 f"| Radarr queue | {radarr_q.get('totalRecords', 0) if isinstance(radarr_q, dict) else '?'} items |\n"
                 f"| Sonarr queue | {sonarr_q.get('totalRecords', 0) if isinstance(sonarr_q, dict) else '?'} items |\n"
                 f"| Lidarr queue | {lidarr_q.get('totalRecords', 0) if isinstance(lidarr_q, dict) else '?'} items |\n"
                 f"| Lidarr wanted | {lidarr_wanted_total} missing albums |\n"
                 f"| Plex sessions | {tautulli_stats.get('total_sessions', 0)} "
                 f"({tautulli_stats.get('transcoding', 0)} transcoding) |\n")

    if issues:
        lines.append("\n## Issues\n")
        for i in issues:
            icon = "🔴" if i.severity == "critical" else "🟡" if i.severity == "warning" else "🔵"
            lines.append(f"- {icon} **[{i.severity.upper()}] {i.component}:** {i.message}")
            if i.detail:
                lines.append(f"  _{i.detail}_")
        lines.append("")
    else:
        lines.append("\n✅ No issues detected.\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now[:10]
    print(f"[media-health] {now}")

    print("[1/3] Collecting media pipeline data...")
    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            (nzbget_status, nzbget_history, nzbget_active,
             radarr_queue, sonarr_queue,
             lidarr_queue, lidarr_wanted,
             tautulli_activity) = await asyncio.gather(
                _resource(session, "nzbget://status"),
                _resource(session, "nzbget://history"),
                _resource(session, "nzbget://listgroups"),
                _resource(session, "radarr://queue"),
                _resource(session, "sonarr://queue"),
                _resource(session, "lidarr://queue"),
                _resource(session, "lidarr://wanted"),
                _resource(session, "tautulli://activity"),
            )

    print("[2/3] Checking thresholds...")
    issues: list[Issue] = []
    issues += _check_nzbget(nzbget_status, nzbget_history, nzbget_active)
    issues += _check_arr_queue("Radarr", radarr_queue)
    issues += _check_arr_queue("Sonarr", sonarr_queue)
    issues += _check_lidarr(lidarr_queue, lidarr_wanted)
    tautulli_issues, tautulli_stats = _check_tautulli(tautulli_activity)
    issues += tautulli_issues

    criticals = [i for i in issues if i.severity == "critical"]
    warnings  = [i for i in issues if i.severity == "warning"]
    print(f"      {len(issues)} issue(s): {len(criticals)} critical, {len(warnings)} warning")

    try:
        summary = await _summarise(issues, tautulli_stats)
    except Exception as e:
        print(f"[WARN] LLM summary failed: {e}", file=sys.stderr)
        summary = f"{len(issues)} issue(s) detected" if issues else "Media pipeline healthy — no issues detected"

    print("[3/3] Writing report and notifying...")
    report_md = _build_report(now, issues, tautulli_stats,
                               nzbget_status, radarr_queue, sonarr_queue,
                               lidarr_queue, lidarr_wanted, summary)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"{date_label}-media-health.md")
    with open(report_path, "w") as f:
        f.write(report_md)
    print(f"      Saved to {report_path}")

    if criticals or warnings:
        sev = "CRITICAL" if criticals else "WARNING"
        body = "\n".join(f"[{i.severity.upper()}] {i.component}: {i.message}"
                         for i in (criticals + warnings))
        send_ntfy(
            NOTIFY_SH,
            title=f"Media Health {sev}: {summary[:80]}",
            tags="movie_camera,warning" if warnings else "movie_camera,rotating_light",
            priority="high" if criticals else "default",
            message=body,
        )
        print(f"      ntfy sent — {sev}")
    else:
        print("      All clear — no notifications sent")

    if criticals:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
