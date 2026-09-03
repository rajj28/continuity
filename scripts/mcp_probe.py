#!/usr/bin/env python
"""Minimal Streamable-HTTP MCP client, for verifying a server by hand.

Deliberately dependency-free and ~100 lines: when an MCP connection misbehaves
mid-build we want to bisect against something we fully understand, not against
a framework. Handles the two response encodings a Streamable-HTTP server may
use (plain JSON, or a text/event-stream frame) and the session-id handshake.

    python scripts/mcp_probe.py tempo tools
    python scripts/mcp_probe.py tempo call get-attribute-names '{"scope":"span"}'
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


class McpClient:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        }
        self._id = 0
        self.session_id: str | None = None

    def _post(self, payload: dict) -> tuple[dict | None, dict[str, str]]:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise SystemExit(f"HTTP {exc.code} from {self.url}\n{body}") from exc

        if not raw.strip():
            return None, resp_headers

        # A Streamable-HTTP server may answer with text/event-stream carrying
        # SEVERAL frames -- server-initiated notifications (tools/list_changed
        # fires here, because proxied tool discovery finishes asynchronously)
        # interleaved with the actual response. Match on the request id rather
        # than taking the first frame, which is a notification and has no id.
        if "text/event-stream" in resp_headers.get("content-type", ""):
            fallback: dict | None = None
            for line in raw.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if frame.get("id") == payload.get("id"):
                    return frame, resp_headers
                fallback = fallback or frame
            return fallback, resp_headers

        return json.loads(raw), resp_headers

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        result, headers = self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": method,
             "params": params or {}}
        )
        if sid := headers.get("mcp-session-id"):
            self.session_id = sid
        if result is None:
            raise SystemExit(f"empty response to {method}")
        if "error" in result:
            raise SystemExit(f"{method} failed: {result['error']}")
        return result.get("result", {})

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    def connect(self, settle_s: float = 0.0) -> dict:
        info = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "continuity-probe", "version": "0.1.0"},
        })
        self.notify("notifications/initialized")
        # mcp-grafana discovers and proxies external MCP servers (Tempo) after
        # the handshake returns, so an immediate tools/list can legitimately
        # come back empty. Give discovery a moment before asking.
        if settle_s:
            time.sleep(settle_s)
        return info


def build(target: str, env: dict[str, str]) -> McpClient:
    if target == "tempo":
        cred = f"{env['GRAFANA_TEMPO_USER_ID']}:{env['GRAFANA_TEMPO_TOKEN']}"
        basic = b64encode(cred.encode()).decode()
        return McpClient(env["GRAFANA_TEMPO_MCP_URL"],
                         {"Authorization": f"Basic {basic}"})
    if target in ("grafana", "scribe"):
        default_port = 8000 if target == "grafana" else 8001
        url = env.get(
            f"MCP_{target.upper()}_URL", f"http://localhost:{default_port}/mcp"
        )
        headers = {}
        if token := env.get("MCP_GRAFANA_SERVER_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        return McpClient(url, headers)
    raise SystemExit(f"unknown target {target!r}; use 'tempo' or 'grafana'")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)

    target, command = sys.argv[1], sys.argv[2]
    client = build(target, load_env())
    info = client.connect(settle_s=10.0 if target != 'tempo' else 0.0)
    server = info.get("serverInfo", {})
    print(f"connected: {server.get('name')} v{server.get('version')} "
          f"(protocol {info.get('protocolVersion')})\n")

    if command == "tools":
        tools = client.request("tools/list").get("tools", [])
        print(f"{len(tools)} tools:\n")
        for t in sorted(tools, key=lambda x: x["name"]):
            desc = (t.get("description") or "").split("\n")[0][:88]
            print(f"  {t['name']:<28} {desc}")
    elif command == "call":
        name = sys.argv[3]
        args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        out = client.request("tools/call", {"name": name, "arguments": args})
        for block in out.get("content", []):
            print(block.get("text", json.dumps(block))[:4000])
        if out.get("isError"):
            print("\n[tool reported an error]")
    else:
        raise SystemExit(f"unknown command {command!r}; use 'tools' or 'call'")


if __name__ == "__main__":
    main()
