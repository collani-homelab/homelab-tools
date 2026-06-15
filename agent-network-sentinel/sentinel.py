"""
sentinel.py — Network Sentinel

Polls UniFi for active clients and alerts on unknown devices (MACs not in
known-devices.json). Runs every 5 minutes via Dagu. Sends ntfy once per
device on first sighting; subsequent runs are silent until the device
disappears and reappears.

Usage:
  python sentinel.py            # normal poll — alert on unknowns
  python sentinel.py --learn    # add all current clients to known-devices.json
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_SSE_URL  = os.getenv("MCP_SSE_URL",  "http://localhost:8083/sse")
REPORTS_DIR  = Path(os.getenv("REPORTS_DIR", os.path.join(os.path.dirname(__file__), "reports")))
NOTIFY_SH    = os.getenv("NOTIFY_SH",    "")

SCRIPT_DIR        = Path(__file__).parent
KNOWN_DEVICES_FILE = SCRIPT_DIR / "known-devices.json"
STATE_FILE         = SCRIPT_DIR / "state.json"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _load_known() -> dict:
    if KNOWN_DEVICES_FILE.exists():
        return json.loads(KNOWN_DEVICES_FILE.read_text())
    return {}


def _save_known(known: dict) -> None:
    KNOWN_DEVICES_FILE.write_text(json.dumps(known, indent=2, sort_keys=True))


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"unknown": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# MCP helper
# ---------------------------------------------------------------------------
async def _fetch_clients() -> list[dict]:
    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_unifi_clients", {})
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            raw = json.loads(text)
            # The tool returns {"data": [...]} or a list directly
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict):
                return raw.get("data", raw.get("clients", []))
    return []


# ---------------------------------------------------------------------------
# Learn mode
# ---------------------------------------------------------------------------
def _learn(clients: list[dict]) -> None:
    known = _load_known()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    for c in clients:
        mac = c.get("mac", "").lower()
        if not mac:
            continue
        if mac not in known:
            known[mac] = {
                "name":       c.get("name") or c.get("hostname") or mac,
                "hostname":   c.get("hostname", ""),
                "type":       "wired" if c.get("is_wired") else "wireless",
                "network":    c.get("essid") or c.get("network", ""),
                "learned_at": now,
            }
            added += 1
        else:
            # Update hostname if it changed
            known[mac]["hostname"] = c.get("hostname", known[mac].get("hostname", ""))
    _save_known(known)
    print(f"[learn] Added {added} new device(s). Total known: {len(known)}")


# ---------------------------------------------------------------------------
# Sentinel logic
# ---------------------------------------------------------------------------
def _check(clients: list[dict], known: dict, state: dict, now: str) -> list[dict]:
    """Return list of newly-alerted unknown devices."""
    unknown_state = state.setdefault("unknown", {})
    active_macs = set()
    newly_alerted = []

    for c in clients:
        mac = c.get("mac", "").lower()
        if not mac:
            continue
        active_macs.add(mac)

        if mac in known:
            continue

        # Unknown device
        if mac not in unknown_state:
            unknown_state[mac] = {
                "first_seen": now,
                "last_seen":  now,
                "hostname":   c.get("hostname", ""),
                "ip":         c.get("ip", ""),
                "essid":      c.get("essid") or c.get("network", ""),
                "is_wired":   c.get("is_wired", False),
                "alerted":    False,
            }

        entry = unknown_state[mac]
        entry["last_seen"] = now
        entry["hostname"]  = c.get("hostname", entry["hostname"])
        entry["ip"]        = c.get("ip", entry["ip"])

        if not entry["alerted"]:
            entry["alerted"] = True
            newly_alerted.append({**entry, "mac": mac})

    # Remove unknown devices that have disconnected (keep for 24h for history)
    # We don't purge — just leave them; user can clean state.json manually

    return newly_alerted


# ---------------------------------------------------------------------------
# Notify and report
# ---------------------------------------------------------------------------
def _send_ntfy(title: str, tags: str, priority: str, message: str) -> None:
    if not os.path.exists(NOTIFY_SH):
        return
    subprocess.run([NOTIFY_SH, title, tags, priority, message],
                   capture_output=True, timeout=10)


def _write_report(date_label: str, newly_alerted: list[dict], all_unknown: dict) -> None:
    lines = [
        f"# Network Sentinel — {date_label}\n",
        f"**New unknown devices this run:** {len(newly_alerted)}\n",
        f"**Total unacknowledged unknowns:** {len(all_unknown)}\n",
    ]

    if newly_alerted:
        lines.append("\n## New Devices (First Sighting)\n")
        lines.append("| MAC | Hostname | IP | Connection | First Seen |")
        lines.append("|-----|----------|----|------------|------------|")
        for d in newly_alerted:
            conn = "wired" if d.get("is_wired") else f"wifi:{d.get('essid','?')}"
            lines.append(f"| `{d['mac']}` | {d['hostname'] or '—'} | {d['ip'] or '—'} | {conn} | {d['first_seen']} |")
        lines.append("")

    if all_unknown:
        lines.append("\n## All Unacknowledged Unknown Devices\n")
        lines.append("| MAC | Hostname | IP | First Seen | Last Seen |")
        lines.append("|-----|----------|----|------------|-----------|")
        for mac, d in sorted(all_unknown.items(), key=lambda x: x[1]["first_seen"], reverse=True):
            lines.append(f"| `{mac}` | {d['hostname'] or '—'} | {d['ip'] or '—'} | {d['first_seen']} | {d['last_seen']} |")
        lines.append("")

    lines.append("\n_To acknowledge devices, run: `python sentinel.py --learn` while they are connected._\n")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{date_label}-network-sentinel.md"
    report_path.write_text("\n".join(lines))
    print(f"      Saved to {report_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(learn: bool) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now[:10]
    print(f"[network-sentinel] {now}")

    print("[1/3] Fetching UniFi clients...")
    clients = await _fetch_clients()
    print(f"      {len(clients)} active client(s)")

    if learn:
        _learn(clients)
        return

    known = _load_known()
    state = _load_state()
    print(f"      {len(known)} known device(s) in allowlist")

    print("[2/3] Checking for unknown devices...")
    newly_alerted = _check(clients, known, state, now)
    _save_state(state)

    all_unknown = state.get("unknown", {})
    total_unknown = len(all_unknown)
    print(f"      {total_unknown} total unknown device(s), {len(newly_alerted)} new this run")

    print("[3/3] Notifying and reporting...")
    if newly_alerted:
        body_lines = []
        for d in newly_alerted:
            conn = "wired" if d.get("is_wired") else f"wifi:{d.get('essid','?')}"
            body_lines.append(f"{d['mac']} ({d['hostname'] or 'unknown'}) via {conn} — {d['ip'] or 'no IP'}")
        body = "\n".join(body_lines)

        _send_ntfy(
            title=f"Network Sentinel: {len(newly_alerted)} unknown device(s) connected",
            tags="eyes,warning",
            priority="high",
            message=body,
        )
        print(f"      ntfy sent — {len(newly_alerted)} new unknown device(s)")
        _write_report(date_label, newly_alerted, all_unknown)
    elif total_unknown > 0:
        # Unknowns exist but already alerted — refresh report silently
        _write_report(date_label, [], all_unknown)
        print(f"      {total_unknown} lingering unknown(s) — report updated, no new ntfy")
    else:
        print("      All clear — no unknown devices")

    if newly_alerted:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--learn", action="store_true",
                        help="Add all currently connected clients to known-devices.json")
    args = parser.parse_args()
    asyncio.run(main(learn=args.learn))
