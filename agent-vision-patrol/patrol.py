"""
patrol.py — Vision Patrol Agent

Runs on a schedule via Dagu. Screenshots two Grafana time-series panels,
sends each to a local multimodal Ollama model, and fires an Ntfy alert when
the narrative suggests anything other than nominal behaviour. Always writes
PNG screenshots and a markdown report for debug click-through.
"""
import base64
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GRAFANA_URL    = os.getenv("GRAFANA_URL",    "http://192.168.99.178:3000")
GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY", "")
OLLAMA_URL     = os.getenv("OLLAMA_URL",     "http://192.168.99.178:11434")
VISION_MODEL   = os.getenv("VISION_MODEL",   "llava:7b")
NOTIFY_SH      = os.getenv("NOTIFY_SH",      "")
REPORTS_DIR    = Path(os.getenv("REPORTS_DIR", Path(__file__).parent / "reports"))

# Grafana render dimensions
PANEL_WIDTH  = 1200
PANEL_HEIGHT = 500
PANEL_FROM   = "now-1h"
PANEL_TO     = "now"

# Panels to patrol: (dashboard_uid, panel_id, friendly_name)
PANELS = [
    ("rYdddlPWk", 77,  "cpu-basic"),
    ("rYdddlPWk", 78,  "memory-basic"),
]

def _send_ntfy(title: str, tags: str, priority: str, message: str) -> None:
    if not NOTIFY_SH or not os.path.exists(NOTIFY_SH):
        print(f"[WARN] notify.sh not found at {NOTIFY_SH!r}", file=sys.stderr)
        return
    subprocess.run([NOTIFY_SH, title, tags, priority, message], capture_output=True, timeout=10)


ANALYSIS_PROMPT = """\
You are an SRE reviewing a Grafana monitoring panel for anomalies.
Describe what you see in 2-3 sentences. Focus on: unusual spikes or drops, \
saturation, flat-lines that suggest missing data, or trends approaching limits.
If everything looks normal, start your response with the word "Nominal".
Be specific about values and timeframes if visible."""


# ---------------------------------------------------------------------------
# Grafana render
# ---------------------------------------------------------------------------
def capture_panel(dashboard_uid: str, panel_id: int) -> bytes:
    url = (
        f"{GRAFANA_URL}/render/d-solo/{dashboard_uid}"
        f"?orgId=1&panelId={panel_id}"
        f"&width={PANEL_WIDTH}&height={PANEL_HEIGHT}"
        f"&from={PANEL_FROM}&to={PANEL_TO}&tz=UTC"
    )
    headers = {}
    if GRAFANA_API_KEY:
        headers["Authorization"] = f"Bearer {GRAFANA_API_KEY}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if not resp.content or len(resp.content) < 100:
        raise ValueError(f"Render returned empty/tiny response ({len(resp.content)} bytes) — is grafana-image-renderer running?")
    return resp.content


# ---------------------------------------------------------------------------
# Ollama multimodal analysis
# ---------------------------------------------------------------------------
def analyze_panel(image_bytes: bytes, panel_name: str) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": VISION_MODEL,
        "prompt": ANALYSIS_PROMPT,
        "images": [b64],
        "stream": False,
    }
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json().get("response", "").strip()
    if not text:
        raise ValueError("Ollama returned empty response")
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[vision-patrol] {now} — model={VISION_MODEL}")

    findings: list[tuple[str, str]] = []   # (panel_name, narrative)
    anomalies: list[str] = []
    errors: list[str] = []

    for (uid, panel_id, name) in PANELS:
        print(f"  [{name}] capturing panel {panel_id}...")
        try:
            img_bytes = capture_panel(uid, panel_id)
        except Exception as e:
            msg = f"capture failed: {e}"
            print(f"  [{name}] ERROR: {msg}", file=sys.stderr)
            errors.append(f"{name}: {msg}")
            continue

        img_path = REPORTS_DIR / f"{name}.png"
        img_path.write_bytes(img_bytes)
        print(f"  [{name}] screenshot saved ({len(img_bytes):,} bytes) → {img_path}")

        print(f"  [{name}] analysing with {VISION_MODEL}...")
        try:
            narrative = analyze_panel(img_bytes, name)
        except Exception as e:
            msg = f"analysis failed: {e}"
            print(f"  [{name}] ERROR: {msg}", file=sys.stderr)
            errors.append(f"{name}: {msg}")
            continue

        findings.append((name, narrative))
        is_nominal = narrative.lower().startswith("nominal")
        status = "nominal" if is_nominal else "ANOMALY"
        print(f"  [{name}] {status}: {narrative[:120]}")

        if not is_nominal:
            anomalies.append(name)

    # Write markdown report (always — useful for Dagu log click-through)
    report_lines = [f"# Vision Patrol — {now}\n"]
    for name, narrative in findings:
        flag = "" if name not in anomalies else " ⚠️"
        report_lines.append(f"## {name}{flag}\n\n{narrative}\n")
    if errors:
        report_lines.append(f"## Errors\n\n" + "\n".join(f"- {e}" for e in errors) + "\n")

    report_text = "\n".join(report_lines)
    report_path = REPORTS_DIR / "latest_report.md"
    report_path.write_text(report_text)
    print(f"[vision-patrol] report → {report_path}")

    # Alert
    if anomalies:
        panel_list = ", ".join(anomalies)
        ntfy_body = "\n\n".join(f"[{n}] {t}" for n, t in findings if n in anomalies)
        _send_ntfy(
            title=f"Vision Patrol: anomaly in {panel_list}",
            tags="eyes,rotating_light",
            priority="default",
            message=ntfy_body,
        )
        print(f"[vision-patrol] ntfy sent — {panel_list}")
    elif errors and not findings:
        _send_ntfy(
            title="Vision Patrol: all panels failed to capture",
            tags="eyes,warning",
            priority="default",
            message="\n".join(errors),
        )
        print("[vision-patrol] ntfy sent — capture errors")
        sys.exit(1)
    else:
        print("[vision-patrol] all panels nominal")


if __name__ == "__main__":
    main()
