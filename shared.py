"""Shared utilities for homelab-tools agents."""
import json
import os
import subprocess
import sys


def send_ntfy(notify_sh: str, title: str, tags: str, priority: str, message: str) -> None:
    if not os.path.exists(notify_sh):
        print(f"[WARN] notify.sh not found at {notify_sh}", file=sys.stderr)
        return
    subprocess.run([notify_sh, title, tags, priority, message], capture_output=True, timeout=10)


def parse_mcp_tool(result) -> dict | list:
    """Parse a CallToolResult (result.content) into a dict or list."""
    try:
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return json.loads(text)
    except Exception:
        return {"error": "parse_failed"}


def parse_mcp_resource(result) -> dict | list:
    """Parse a ReadResourceResult (result.contents) into a dict or list."""
    try:
        text = "\n".join(c.text for c in result.contents if c.type == "text")
        return json.loads(text)
    except Exception:
        return {"error": "parse_failed"}
