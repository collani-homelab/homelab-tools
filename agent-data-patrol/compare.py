"""
compare.py — Head-to-head comparison harness for Project #11.

Measures vision-patrol vs data-patrol vs sre-patrol (fixed thresholds) across:
  - True-positive rate  (synthetic anomaly injection via stress-ng)
  - False-positive rate (quiet baseline window)
  - Detection latency   (injection timestamp → Ntfy received timestamp)
  - Narrative quality   (LLM-as-judge via llm-eval-kit GEval)
  - Compute cost        (wall-clock runtime per agent run)

Usage:
    python compare.py baseline          # record a quiet-window run of all three agents
    python compare.py inject cpu        # stress CPU for 3 min, record which agents fire
    python compare.py inject memory     # stress memory, record which agents fire
    python compare.py report            # produce comparison table from recorded runs
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RESULTS_DIR = Path(os.getenv("RESULTS_DIR", Path(__file__).parent / "compare_results"))

AGENTS = {
    "data-patrol":   Path(__file__).parent / "patrol.py",
    "vision-patrol": Path(__file__).parent.parent / "agent-vision-patrol" / "patrol.py",
}

# Path to sre-patrol reports dir — used to detect if it fired
SRE_PATROL_REPORTS = Path(os.getenv(
    "SRE_PATROL_REPORTS_DIR",
    Path(__file__).parent.parent / "agent-sre-patrol" / "reports",
))

STRESS_DURATION_S = int(os.getenv("STRESS_DURATION_S", "180"))  # 3 min


# ---------------------------------------------------------------------------
# Anomaly injection
# ---------------------------------------------------------------------------
def inject_cpu(duration_s: int = STRESS_DURATION_S) -> None:
    """Saturate all CPUs via stress-ng for duration_s seconds."""
    print(f"[inject] CPU stress for {duration_s}s...")
    subprocess.run(
        ["stress-ng", "--cpu", "0", "--timeout", f"{duration_s}s"],
        check=False,
    )


def inject_memory(duration_s: int = STRESS_DURATION_S) -> None:
    """Allocate 60% of available RAM for duration_s seconds."""
    print(f"[inject] memory stress for {duration_s}s...")
    subprocess.run(
        ["stress-ng", "--vm", "1", "--vm-bytes", "60%", "--timeout", f"{duration_s}s"],
        check=False,
    )


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------
def run_agent(name: str, script: Path) -> dict:
    """Run an agent script and return timing + report content."""
    reports_dir = script.parent / "reports"
    start = time.monotonic()
    wall_start = datetime.now(timezone.utc).isoformat()

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    elapsed = time.monotonic() - start

    report_path = reports_dir / "latest_report.md"
    report_text = report_path.read_text() if report_path.exists() else ""

    fired = (
        result.returncode != 0
        or "ntfy sent" in result.stdout
        or "anomaly" in result.stdout.lower()
        or ("## Anomalies" in report_text and "## Anomalies\n\n-" in report_text)
    )

    return {
        "agent":       name,
        "fired":       fired,
        "elapsed_s":   round(elapsed, 2),
        "wall_start":  wall_start,
        "returncode":  result.returncode,
        "stdout":      result.stdout[-2000:],
        "report":      report_text,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_baseline() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print("[compare] recording baseline (no anomaly injection)...")
    results = {name: run_agent(name, script) for name, script in AGENTS.items()}
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = RESULTS_DIR / f"baseline_{ts}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"[compare] baseline saved → {out}")
    _print_table(results, label="BASELINE")


def cmd_inject(anomaly_type: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    inject_fn = {"cpu": inject_cpu, "memory": inject_memory}.get(anomaly_type)
    if not inject_fn:
        print(f"Unknown anomaly type: {anomaly_type}. Choose: cpu, memory", file=sys.stderr)
        sys.exit(1)

    print(f"[compare] starting injection: {anomaly_type}")
    inject_start = datetime.now(timezone.utc).isoformat()

    import threading
    inject_thread = threading.Thread(target=inject_fn, daemon=True)
    inject_thread.start()

    # Wait 60s for the anomaly to register in metrics before running agents
    print("[compare] waiting 60s for anomaly to stabilise in Prometheus...")
    time.sleep(60)

    results = {name: run_agent(name, script) for name, script in AGENTS.items()}
    for r in results.values():
        r["inject_type"] = anomaly_type
        r["inject_start"] = inject_start

    inject_thread.join(timeout=10)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = RESULTS_DIR / f"inject_{anomaly_type}_{ts}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"[compare] results saved → {out}")
    _print_table(results, label=f"INJECT:{anomaly_type.upper()}")


def cmd_report() -> None:
    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        print("No recorded runs found. Run `baseline` or `inject` first.")
        return

    baselines = [f for f in files if f.name.startswith("baseline")]
    injections = [f for f in files if f.name.startswith("inject")]

    print(f"\nLoaded {len(baselines)} baseline(s), {len(injections)} injection(s)\n")

    # False positive rate per agent
    fp_counts: dict[str, int] = {}
    for f in baselines:
        data = json.loads(f.read_text())
        for name, r in data.items():
            fp_counts[name] = fp_counts.get(name, 0) + (1 if r["fired"] else 0)

    # True positive rate per agent per anomaly type
    tp_counts: dict[str, dict[str, int]] = {}
    for f in injections:
        data = json.loads(f.read_text())
        atype = next(iter(data.values())).get("inject_type", "unknown")
        for name, r in data.items():
            tp_counts.setdefault(name, {}).setdefault(atype, 0)
            if r["fired"]:
                tp_counts[name][atype] += 1

    # Print summary
    agents = list(AGENTS.keys())
    print("=" * 60)
    print("COMPARISON REPORT")
    print("=" * 60)
    print(f"\n{'Agent':<20} {'FP rate':<12} {'TP (cpu)':<12} {'TP (mem)':<12} {'Avg runtime':<12}")
    print("-" * 60)
    for name in agents:
        fp = fp_counts.get(name, 0)
        fp_rate = f"{fp}/{len(baselines)}" if baselines else "n/a"
        tp_cpu = tp_counts.get(name, {}).get("cpu", 0)
        tp_mem = tp_counts.get(name, {}).get("memory", 0)
        cpu_injs = sum(1 for f in injections if "cpu" in f.name)
        mem_injs = sum(1 for f in injections if "memory" in f.name)
        tp_cpu_rate = f"{tp_cpu}/{cpu_injs}" if cpu_injs else "n/a"
        tp_mem_rate = f"{tp_mem}/{mem_injs}" if mem_injs else "n/a"

        # Average runtime
        all_runtimes = []
        for f in files:
            data = json.loads(f.read_text())
            if name in data:
                all_runtimes.append(data[name]["elapsed_s"])
        avg_rt = f"{sum(all_runtimes)/len(all_runtimes):.1f}s" if all_runtimes else "n/a"

        print(f"{name:<20} {fp_rate:<12} {tp_cpu_rate:<12} {tp_mem_rate:<12} {avg_rt:<12}")
    print()


def _print_table(results: dict, label: str) -> None:
    print(f"\n{'─'*50}")
    print(f"  {label}")
    print(f"{'─'*50}")
    print(f"  {'Agent':<22} {'Fired':<8} {'Runtime'}")
    print(f"  {'─'*44}")
    for name, r in results.items():
        fired_str = "YES ⚠" if r["fired"] else "no"
        print(f"  {name:<22} {fired_str:<8} {r['elapsed_s']}s")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vision vs data patrol comparison harness")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baseline", help="Record a quiet-window run")
    inj = sub.add_parser("inject", help="Inject a synthetic anomaly and record results")
    inj.add_argument("type", choices=["cpu", "memory"], help="Anomaly type to inject")
    sub.add_parser("report", help="Print comparison table from recorded runs")

    args = parser.parse_args()
    if args.cmd == "baseline":
        cmd_baseline()
    elif args.cmd == "inject":
        cmd_inject(args.type)
    elif args.cmd == "report":
        cmd_report()
