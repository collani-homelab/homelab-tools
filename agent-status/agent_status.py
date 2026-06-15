"""
agent_status.py — Homelab Status CLI
Phase 2: Full device inventory with observability matrix (Logs / Metrics / OTel).
Phase 2b: Full client (station) enumeration via get_unifi_clients.

Architecture:
  Phase 1  — Fetch UniFi devices + clients from homelab-mcp over SSE
  Phase 2  — Fetch Prometheus targets directly (metrics status)
  Phase 3  — Fetch Loki label values (logs status)
  Phase 4  — OTel status is static/known from infrastructure knowledge
  Phase 5  — Merge all sources into unified HomelabStatusReport
  Phase 6  — LLM writes free-text summary (no schema pressure)
"""
import asyncio
import json
import os
from enum import Enum
from typing import List, Optional, Set

import aiohttp
import yaml
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel

# ---------------------------------------------------------------------------
# Load Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
if not os.path.exists(CONFIG_PATH):
    raise FileNotFoundError(
        f"config.yaml not found. Copy config.example.yaml to config.yaml and fill in your node IPs."
    )
with open(CONFIG_PATH, "r") as f:
    config = yaml.safe_load(f)

infra_config = config.get("infrastructure", {})
STATIC_NODES = infra_config.get("static_nodes", [])
for node in STATIC_NODES:
    if "interfaces" not in node:
        node["interfaces"] = []
    if "uptime" not in node:
        node["uptime"] = ""

OTEL_ENABLED_IPS = set(infra_config.get("observability", {}).get("otel_enabled_ips", []))
LOKI_SHIPPING_IPS = set(infra_config.get("observability", {}).get("loki_shipping_ips", []))
UNRAID_NODES = infra_config.get("unraid_nodes", [])
# IP of the node running Docker container exporters (node-exporter, cadvisor).
# Used to resolve container hostnames in Prometheus target labels to an IP.
_LOCAL_DOCKER_NODE_IP = os.getenv("LOCAL_DOCKER_NODE_IP", "")
# ---------------------------------------------------------------------------
# LiteLLM proxy configuration (SRE Machine Proxy)
# ---------------------------------------------------------------------------
os.environ["OPENAI_BASE_URL"] = os.getenv("LITELLM_API_BASE", "http://localhost:4000/v1")
os.environ["OPENAI_API_KEY"] = "sk-local"

model = OpenAIChatModel(os.getenv("STATUS_MODEL", "ollama/qwen2.5-coder:14b"))

MCP_SSE_URL     = os.getenv("MCP_SSE_URL",    "http://localhost:8083/sse")
PROMETHEUS_URL  = os.getenv("PROMETHEUS_URL", "http://localhost:9091")
LOKI_URL        = os.getenv("LOKI_URL",       "http://localhost:3100")

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class ObsStatus(str, Enum):
    WORKING         = "working"
    NOT_WORKING     = "not_working"
    NOT_CONFIGURED  = "not_configured"
    NA              = "n/a"

class NetworkInterface(BaseModel):
    name: str
    status: str
    clients_connected: int = 0

class UnraidDetails(BaseModel):
    os_version: str
    array_state: str
    array_healthy: bool
    cpu_usage_pct: float
    memory_usage_pct: float
    hottest_disk_temp: Optional[float] = None
    total_disk_errors: int
    array_utilization_pct: Optional[float] = None
    containers_active: str
    pending_updates: int
    ups_status: str
    ups_health_state: str

class DeviceStatus(BaseModel):
    name: str
    mac: str
    device_type: str        # uap / usw / udm / server / workstation
    version: str
    ip_address: str
    uptime: str
    state: str
    interfaces: List[NetworkInterface] = Field(default_factory=list)
    # Observability columns
    logs: ObsStatus
    metrics: ObsStatus
    otel: ObsStatus
    unraid_details: Optional[UnraidDetails] = None

class HomelabStatusReport(BaseModel):
    summary: str = Field(description="LLM-generated narrative of homelab status.")
    devices: List[DeviceStatus]
    clients: List["ClientInfo"] = Field(default_factory=list)

class ClientInfo(BaseModel):
    hostname: str
    mac: str
    ip_address: str
    connection_type: str        # "wired" or "wireless"
    uplink: str                 # AP name or switch name
    vlan: str
    signal_rssi: Optional[int] = None   # dBm, wireless only
    satisfaction: Optional[int] = None  # 0-100 experience score
    uptime: str
    tx_bytes_r: Optional[float] = None
    rx_bytes_r: Optional[float] = None

HomelabStatusReport.model_rebuild()

# ---------------------------------------------------------------------------
# LLM agent — free-text summary only
# ---------------------------------------------------------------------------

summary_agent = Agent(
    model,
    system_prompt=(
        "You are a homelab SRE analyst. "
        "Given a JSON status report of all homelab devices and connected clients, "
        "including observability posture (logs, metrics, otel), write a concise 3-5 sentence "
        "executive summary highlighting overall health, client count, any observability gaps, "
        "and the most notable facts.\n"
        "CRITICAL: If any device reports `ups_health_state: 'POWER_EVENT'`, you MUST emphasize this active physical power issue (battery or load) as a CRITICAL ALERT at the very beginning of your summary.\n"
        "WARNING: If a device reports `ups_health_state: 'TELEMETRY_FAILURE'`, note it as an observability warning (e.g., apcupsd daemon off or USB disconnected).\n"
        "HOWEVER, if this telemetry failure is known and listed in the Backlog Context below, mention it is a known backlogged issue rather than a new warning, and include the reason or ETA."
    ),
)

# ---------------------------------------------------------------------------
# Phase 1 helpers — UniFi via MCP SSE
# ---------------------------------------------------------------------------

def _uptime_str(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    parts  = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

def parse_unifi_clients(raw: any) -> List[ClientInfo]:
    """Parse stat/sta payload into ClientInfo models."""
    if isinstance(raw, dict):
        items = raw.get("data", [])
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    clients: List[ClientInfo] = []
    for c in items:
        if not isinstance(c, dict):
            continue

        is_wired = c.get("is_wired", False)
        connection_type = "wired" if is_wired else "wireless"

        # Uplink name: AP name (wireless) or switch name (wired)
        uplink = c.get("ap_mac", "") or c.get("last_uplink_name", "") or c.get("sw_mac", "")

        # VLAN: use network name as the human-readable label
        vlan = c.get("last_connection_network_name", c.get("network", ""))

        # Hostname: prefer dnsname > hostname > mac
        hostname = (
            c.get("dnsname")
            or c.get("hostname")
            or c.get("name")
            or c.get("mac", "unknown")
        )

        clients.append(ClientInfo(
            hostname=hostname,
            mac=c.get("mac", ""),
            ip_address=c.get("ip", c.get("last_ip", "")),
            connection_type=connection_type,
            uplink=uplink,
            vlan=vlan,
            signal_rssi=c.get("rssi") if not is_wired else None,
            satisfaction=c.get("satisfaction"),
            uptime=_uptime_str(c.get("uptime", 0)),
            tx_bytes_r=c.get("tx_bytes-r") or c.get("wired-tx_bytes-r"),
            rx_bytes_r=c.get("rx_bytes-r") or c.get("wired-rx_bytes-r"),
        ))

    # Sort: wired first, then by hostname
    clients.sort(key=lambda c: (0 if c.connection_type == "wired" else 1, c.hostname))
    return clients

def parse_unifi_devices(raw: any) -> List[dict]:
    """Return raw UniFi device dicts, normalised from API response shape."""
    if isinstance(raw, dict):
        return raw.get("data", raw.get("result", []))
    if isinstance(raw, list):
        return raw
    return []

def build_unifi_device(
    d: dict,
    metrics_up_ips: Set[str],
    loki_host_ips: Set[str],
) -> DeviceStatus:
    interfaces: List[NetworkInterface] = []
    for port in d.get("port_table", []):
        interfaces.append(NetworkInterface(
            name=port.get("name", str(port.get("port_idx", "?"))),
            status="up" if port.get("up") else "down",
        ))
    for vap in d.get("vap_table", []):
        interfaces.append(NetworkInterface(
            name=f"{vap.get('radio', '?')} ({vap.get('essid', '?')})",
            status="up",
            clients_connected=vap.get("num_sta", 0),
        ))

    return DeviceStatus(
        name=d.get("name", d.get("mac", "unknown")),
        mac=d.get("mac", ""),
        device_type=d.get("type", "unknown"),
        version=d.get("version", ""),
        ip_address=d.get("ip", ""),
        uptime=_uptime_str(d.get("uptime", 0)),
        state="running" if d.get("state") == 1 else str(d.get("state", "unknown")),
        interfaces=interfaces,
        # UniFi network devices have no node-exporter / log-shipper
        logs=ObsStatus.NA,
        metrics=ObsStatus.NA,
        otel=ObsStatus.NA,
    )

# ---------------------------------------------------------------------------
# Phase 2 — Prometheus: which IPs have an 'up' node-exporter scrape?
# ---------------------------------------------------------------------------

# Docker container hostnames that indicate the SRE machine is being scraped
_SRE_CONTAINER_JOBS = {"node-exporter", "cadvisor", "prometheus"}
_SRE_CONTAINER_INSTANCES = {"node-exporter", "cadvisor", "localhost"}

async def fetch_prometheus_up_ips(session: aiohttp.ClientSession) -> Set[str]:
    """Return set of IPs with at least one healthy Prometheus target.

    Handles two instance formats:
    - IP-based:  '10.0.0.10:9100' → extract IP directly
    - Hostname:  'node-exporter:9100'  → map to LOCAL_DOCKER_NODE_IP via known jobs
    """
    try:
        async with session.get(
            f"{PROMETHEUS_URL}/api/v1/targets", timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            data = await resp.json(content_type=None)
        up_ips: Set[str] = set()
        for t in data.get("data", {}).get("activeTargets", []):
            if t.get("health") != "up":
                continue
            labels   = t.get("labels", {})
            instance = labels.get("instance", "")
            job      = labels.get("job", "")
            host     = instance.split(":")[0]
            # If the host looks like an IP address, use it directly
            if host and host[0].isdigit():
                up_ips.add(host)
            # If it's a known Docker container job on the SRE machine, map it
            elif (job in _SRE_CONTAINER_JOBS or host in _SRE_CONTAINER_INSTANCES) and _LOCAL_DOCKER_NODE_IP:
                up_ips.add(_LOCAL_DOCKER_NODE_IP)
        return up_ips
    except Exception as e:
        print(f"[WARN] Prometheus query failed: {e}")
        return set()


async def fetch_node_uptimes(session: aiohttp.ClientSession) -> dict:
    """Query node_boot_time_seconds to derive uptime strings per IP."""
    import time
    try:
        async with session.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": "node_boot_time_seconds"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json(content_type=None)
        result = data.get("data", {}).get("result", [])
        now = time.time()
        uptimes: dict = {}
        for r in result:
            instance = r.get("metric", {}).get("instance", "")
            job = r.get("metric", {}).get("job", "")
            host = instance.split(":")[0]
            
            ip = host
            if not (host and host[0].isdigit()):
                if (job in _SRE_CONTAINER_JOBS or host in _SRE_CONTAINER_INSTANCES) and _LOCAL_DOCKER_NODE_IP:
                    ip = _LOCAL_DOCKER_NODE_IP
            
            boot_time = float(r["value"][1])
            uptimes[ip] = _uptime_str(int(now - boot_time))
        return uptimes
    except Exception as e:
        print(f"[WARN] Prometheus uptime query failed: {e}")
        return {}

# ---------------------------------------------------------------------------
# Phase 3 — Loki: which hosts have sent logs recently?
# ---------------------------------------------------------------------------

async def fetch_loki_host_ips(session: aiohttp.ClientSession) -> Set[str]:
    """
    Return set of IPs/hostnames that have shipped logs to Loki recently.
    We check the 'host' label values plus do a quick stream query to confirm.
    """
    try:
        async with session.get(
            f"{LOKI_URL}/loki/api/v1/label/host/values",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json(content_type=None)
        return set(data.get("data", []))
    except Exception as e:
        print(f"[WARN] Loki query failed: {e}")
        return set()

# ---------------------------------------------------------------------------
# Phase 4 — Static nodes (Unraid + SRE) with known observability posture
# ---------------------------------------------------------------------------

# Static nodes and observability lists are now loaded from config.yaml

def build_static_node(
    node: dict,
    metrics_up_ips: Set[str],
    loki_host_ips: Set[str],
) -> DeviceStatus:
    ip = node["ip_address"]

    metrics = (
        ObsStatus.WORKING if ip in metrics_up_ips
        else ObsStatus.NOT_WORKING
    )
    # Loki check: either the IP itself or the known static set
    logs = (
        ObsStatus.WORKING
        if ip in LOKI_SHIPPING_IPS or ip in loki_host_ips
        else ObsStatus.NOT_CONFIGURED
    )
    otel = ObsStatus.WORKING if ip in OTEL_ENABLED_IPS else ObsStatus.NA

    return DeviceStatus(
        name=node["name"],
        mac=node["mac"],
        device_type=node["device_type"],
        version=node["version"],
        ip_address=ip,
        uptime=node["uptime"],
        state=node["state"],
        interfaces=[NetworkInterface(**i) for i in node["interfaces"]],
        logs=logs,
        metrics=metrics,
        otel=otel,
    )

def parse_mcp_json(result) -> dict:
    if isinstance(result, Exception):
        return {"error": f"Failed to call tool: {result}"}
    try:
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return json.loads(text)
    except Exception as e:
        return {"error": f"Failed to parse JSON: {e}"}

def build_unraid_device_status(
    server_name: str,
    ip: str,
    stats_data: dict,
    array_data: dict,
    docker_data: dict,
    ups_data: dict,
    metrics_up_ips: Set[str],
    loki_host_ips: Set[str],
    node_uptimes: dict,
) -> DeviceStatus:
    # 1. Parse system stats
    os_distro = stats_data.get("info", {}).get("os", {}).get("distro", "Unraid OS")
    os_release = stats_data.get("info", {}).get("os", {}).get("release", "")
    os_version = f"{os_distro} {os_release}".strip() or "Unraid"
    
    cpu_usage = stats_data.get("metrics", {}).get("cpu", {}).get("percentTotal", 0.0)
    mem_usage = stats_data.get("metrics", {}).get("memory", {}).get("percentTotal", 0.0)
    
    uptime_val = node_uptimes.get(ip, "unknown")
    
    # 2. Parse array status
    array_state = array_data.get("array", {}).get("state", "UNKNOWN")
    disks = array_data.get("array", {}).get("disks", [])
    
    hottest_temp = None
    total_errors = 0
    total_space = 0
    used_space = 0
    for d in disks:
        errors = d.get("numErrors", 0)
        total_errors += errors
        t = d.get("temp")
        if t is not None and str(t) != "NaN":
            try:
                temp_val = float(t)
                if hottest_temp is None or temp_val > hottest_temp:
                    hottest_temp = temp_val
            except (ValueError, TypeError):
                pass
                
        size = d.get('fsSize', 0)
        free = d.get('fsFree', 0)
        total_space += size
        used_space += (size - free)
                
    array_healthy = (array_state == "STARTED") and (total_errors == 0)
    
    array_utilization_pct = None
    if total_space > 0:
        array_utilization_pct = round((used_space / total_space) * 100, 2)
    
    # 3. Parse docker containers
    containers = docker_data.get("docker", {}).get("containers", [])
    running_count = sum(1 for c in containers if c.get("state", "").lower() == "running")
    total_count = len(containers)
    active_str = f"{running_count} / {total_count} active"
    pending_updates = sum(1 for c in containers if c.get("isUpdateAvailable"))
    
    # 4. Parse UPS status
    ups_health_state = "HEALTHY"
    if "error" in ups_data:
        ups_status = ups_data["error"]
        ups_health_state = "TELEMETRY_FAILURE"
    else:
        ups_list = ups_data.get("upsDevices", [])
        if not ups_list:
            ups_status = "N/A"
            ups_health_state = "NOT_CONFIGURED"
        else:
            ups = ups_list[0]
            status_str = ups.get("status", "Unknown")
            charge = ups.get("battery", {}).get("chargeLevel", 0)
            runtime_sec = ups.get("battery", {}).get("estimatedRuntime", 0)
            load = ups.get("power", {}).get("loadPercentage", 0)
            
            runtime_str = _uptime_str(runtime_sec) if runtime_sec else "unknown"
            ups_status = f"{status_str} ({charge}%, {runtime_str} runtime, {load}% load)"

            if "commlost" in status_str.lower():
                ups_health_state = "TELEMETRY_FAILURE"
            elif charge < 100 or "online" not in status_str.lower() or load > 85:
                ups_health_state = "POWER_EVENT"

    details = UnraidDetails(
        os_version=os_version,
        array_state=array_state,
        array_healthy=array_healthy,
        cpu_usage_pct=cpu_usage,
        memory_usage_pct=mem_usage,
        hottest_disk_temp=hottest_temp,
        total_disk_errors=total_errors,
        array_utilization_pct=array_utilization_pct,
        containers_active=active_str,
        pending_updates=pending_updates,
        ups_status=ups_status,
        ups_health_state=ups_health_state,
    )
    
    metrics = ObsStatus.WORKING if ip in metrics_up_ips else ObsStatus.NOT_WORKING
    logs = ObsStatus.WORKING if ip in loki_host_ips else ObsStatus.NOT_CONFIGURED
    otel = ObsStatus.WORKING if ip in OTEL_ENABLED_IPS else ObsStatus.NA

    return DeviceStatus(
        name=server_name.capitalize(),
        mac="",
        device_type="server",
        version="Unraid",
        ip_address=ip,
        uptime=uptime_val,
        state="running" if array_state == "STARTED" else array_state.lower(),
        interfaces=[],
        logs=logs,
        metrics=metrics,
        otel=otel,
        unraid_details=details,
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print(f"[1/6] Connecting to Homelab MCP via SSE at {MCP_SSE_URL}...")

    async with aiohttp.ClientSession() as http:
        # Phase 2, 3 & uptime — fire off observability queries concurrently
        prom_task   = asyncio.create_task(fetch_prometheus_up_ips(http))
        loki_task   = asyncio.create_task(fetch_loki_host_ips(http))
        uptime_task = asyncio.create_task(fetch_node_uptimes(http))

        # Phase 1 — UniFi devices/clients + Unraid server stats concurrently
        async with sse_client(MCP_SSE_URL) as streams:
            async with ClientSession(streams[0], streams[1]) as mcp:
                await mcp.initialize()
                print("[2/6] MCP connected. Fetching UniFi and Unraid telemetry...")
                
                # Fire UniFi calls
                unifi_dev_task = asyncio.create_task(mcp.call_tool("get_unifi_devices", {}))
                unifi_cli_task = asyncio.create_task(mcp.call_tool("get_unifi_clients", {}))
                
                # Fire calls for dynamically loaded Unraid nodes
                unraid_tasks = {}
                for node in UNRAID_NODES:
                    name = node["name"]
                    unraid_tasks[name] = {
                        "stats": asyncio.create_task(mcp.call_tool(f"get_unraid_system_stats_{name}", {})),
                        "array": asyncio.create_task(mcp.call_tool(f"get_unraid_array_status_{name}", {})),
                        "docker": asyncio.create_task(mcp.call_tool(f"get_unraid_containers_{name}", {})),
                        "ups": asyncio.create_task(mcp.call_tool(f"get_unraid_ups_status_{name}", {})),
                    }
                
                # Gather everything concurrently
                tasks_to_gather = [unifi_dev_task, unifi_cli_task]
                for node in UNRAID_NODES:
                    name = node["name"]
                    tasks_to_gather.extend([
                        unraid_tasks[name]["stats"],
                        unraid_tasks[name]["array"],
                        unraid_tasks[name]["docker"],
                        unraid_tasks[name]["ups"],
                    ])
                
                results = await asyncio.gather(*tasks_to_gather, return_exceptions=True)
                
                # Extract results
                devices_result = results[0]
                clients_result = results[1]
                
                raw_devices_text = ""
                if not isinstance(devices_result, Exception):
                    raw_devices_text = "\n".join(
                        c.text for c in devices_result.content if c.type == "text"
                    )
                
                raw_clients_text = ""
                if not isinstance(clients_result, Exception):
                    raw_clients_text = "\n".join(
                        c.text for c in clients_result.content if c.type == "text"
                    )

                unraid_results = {}
                idx = 2
                for node in UNRAID_NODES:
                    name = node["name"]
                    unraid_results[name] = {
                        "stats": parse_mcp_json(results[idx]),
                        "array": parse_mcp_json(results[idx+1]),
                        "docker": parse_mcp_json(results[idx+2]),
                        "ups": parse_mcp_json(results[idx+3]),
                    }
                    idx += 4

        # Await observability data
        print("[3/6] Collecting Prometheus targets...")
        metrics_up_ips = await prom_task
        print(f"      → {len(metrics_up_ips)} IPs with active metrics scrape: {metrics_up_ips}")

        print("[4/6] Collecting Loki host labels...")
        loki_host_ips = await loki_task
        print(f"      → Loki hosts: {loki_host_ips}")

        node_uptimes = await uptime_task
        print(f"      → Node uptimes from Prometheus: {node_uptimes}")

    # Parse UniFi devices
    print("[5/6] Parsing devices and building report...")
    try:
        raw_data = json.loads(raw_devices_text)
    except json.JSONDecodeError:
        raw_data = {}

    try:
        raw_clients_data = json.loads(raw_clients_text)
    except json.JSONDecodeError:
        raw_clients_data = {}

    unifi_raw = parse_unifi_devices(raw_data)
    devices: List[DeviceStatus] = [
        build_unifi_device(d, metrics_up_ips, loki_host_ips)
        for d in unifi_raw
        if isinstance(d, dict)
    ]

    clients: List[ClientInfo] = parse_unifi_clients(raw_clients_data)
    print(f"      → {len(clients)} clients ({sum(1 for c in clients if c.connection_type == 'wired')} wired, {sum(1 for c in clients if c.connection_type == 'wireless')} wireless)")

    # Build dynamic Unraid statuses
    for node in UNRAID_NODES:
        name = node["name"]
        ip = node["ip"]
        stats = unraid_results[name]["stats"]
        array = unraid_results[name]["array"]
        docker = unraid_results[name]["docker"]
        ups = unraid_results[name]["ups"]

        if "error" not in stats and "error" not in array:
            devices.append(build_unraid_device_status(
                name, ip,
                stats, array, docker, ups,
                metrics_up_ips, loki_host_ips, node_uptimes
            ))
        else:
            print(f"[WARN] {name.capitalize()} data incomplete, stats_err={stats.get('error')}, array_err={array.get('error')}")

    # Add remaining static nodes (SRE machine), enriched with real uptime from Prometheus
    for node in STATIC_NODES:
        ip = node["ip_address"]
        node["uptime"] = node_uptimes.get(ip, "unknown")
        devices.append(build_static_node(node, metrics_up_ips, loki_host_ips))

    # Sort: servers first, then network gear
    order = {"server": 0, "udm": 1, "usw": 2, "uap": 3}
    devices.sort(key=lambda d: (order.get(d.device_type, 9), d.name))

    # Phase 6 — LLM summary (free text, no schema)
    print("[6/6] Generating LLM summary...")
    
    devices_payload = []
    for d in devices:
        item = {
            "name": d.name, "type": d.device_type, "state": d.state,
            "ip": d.ip_address,
            "logs": d.logs.value, "metrics": d.metrics.value, "otel": d.otel.value,
        }
        if d.unraid_details:
            item["unraid_details"] = {
                "os_version": d.unraid_details.os_version,
                "array_state": d.unraid_details.array_state,
                "array_healthy": d.unraid_details.array_healthy,
                "cpu_usage_pct": d.unraid_details.cpu_usage_pct,
                "memory_usage_pct": d.unraid_details.memory_usage_pct,
                "hottest_disk_temp": d.unraid_details.hottest_disk_temp,
                "total_disk_errors": d.unraid_details.total_disk_errors,
                "array_utilization_pct": d.unraid_details.array_utilization_pct,
                "containers_active": d.unraid_details.containers_active,
                "pending_updates": d.unraid_details.pending_updates,
                "ups_status": d.unraid_details.ups_status,
                "ups_health_state": d.unraid_details.ups_health_state,
            }
        devices_payload.append(item)

    device_snapshot = json.dumps(
        {
            "devices": devices_payload,
            "client_summary": {
                "total": len(clients),
                "wired": sum(1 for c in clients if c.connection_type == "wired"),
                "wireless": sum(1 for c in clients if c.connection_type == "wireless"),
            },
        },
        indent=2,
    )
    from roadmap_parser import get_backlog_context
    backlog_context = get_backlog_context()

    summary_result = await summary_agent.run(
        f"Here is the current homelab device inventory with observability status and dynamic Unraid server health details:\n\n{device_snapshot}\n\n=== Backlog Context ===\n{backlog_context}"
    )

    report = HomelabStatusReport(summary=summary_result.output, devices=devices, clients=clients)

    print("\n--- Homelab Status Report ---")
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
