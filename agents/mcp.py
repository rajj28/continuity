"""MCP transport, as a library.

`scripts/mcp_probe.py` is the hand-tool version of this and stays a script --
when a connection misbehaves mid-build you want to bisect against something you
can read in one screen. This is the same protocol handling with the edges a
long-running process needs: real exceptions instead of `SystemExit`, tool
results unwrapped from MCP's content envelope, and a lazily-established session
that survives being reused.

The one thing worth stating loudly: this class has no idea whether the server
it is talking to can write. That is not its job. The read-only guarantee is
enforced by starting mcp-grafana with `--disable-write`, which registers zero
create/update/patch/delete tools -- so a write is not refused at runtime, it is
absent from the protocol. See docs/MCP_BOUNDARIES.md. A boundary that lives in
process arguments cannot be talked around by a model, which is exactly why it
is not implemented as a check in here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from base64 import b64encode
from typing import Any

PROTOCOL = "2025-06-18"

# mcp-grafana discovers and proxies external MCP servers (Tempo) after the
# handshake returns, so an immediate tools/list can legitimately come back
# empty. Measured at roughly 6 s against Grafana Cloud on 3 September 2026.
PROXY_SETTLE_S = 10.0


class McpError(RuntimeError):
    """A protocol- or transport-level failure talking to an MCP server."""


class McpToolError(McpError):
    """The server ran the tool and it failed. Distinct from McpError because
    the two demand different responses: a transport failure is worth retrying,
    a bad argument is not."""


class McpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 *, timeout_s: float = 30.0, settle_s: float = 0.0) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.settle_s = settle_s
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        self._id = 0
        self.session_id: str | None = None
        self.server: dict[str, Any] = {}
        self._connected = False

    # -- transport ---------------------------------------------------------

    def _post(self, payload: dict) -> tuple[dict | None, dict[str, str]]:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise McpError(f"HTTP {exc.code} from {self.url}: {body}") from exc
        except OSError as exc:
            raise McpError(f"cannot reach {self.url}: {exc}") from exc

        if not raw.strip():
            return None, resp_headers

        # A Streamable-HTTP server may answer with text/event-stream carrying
        # SEVERAL frames -- server-initiated notifications interleaved with the
        # actual response. Match on the request id; the first frame is often
        # `notifications/tools/list_changed` and has no id at all.
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
        result, headers = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": method,
            "params": params or {},
        })
        if sid := headers.get("mcp-session-id"):
            self.session_id = sid
        if result is None:
            raise McpError(f"empty response to {method}")
        if "error" in result:
            raise McpError(f"{method} failed: {result['error']}")
        return result.get("result", {})

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    def connect(self) -> dict:
        if self._connected:
            return self.server
        self.server = self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "continuity", "version": "0.1.0"},
        }).get("serverInfo", {})
        self.notify("notifications/initialized")
        if self.settle_s:
            time.sleep(self.settle_s)
        self._connected = True
        return self.server

    # -- tools -------------------------------------------------------------

    def tools(self) -> list[dict]:
        self.connect()
        return self.request("tools/list").get("tools", [])

    def tool_names(self) -> list[str]:
        return sorted(t["name"] for t in self.tools())

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a tool and return its payload, JSON-decoded where possible.

        MCP wraps results in a content envelope; callers want the data. The
        `isError` flag is raised rather than returned, because a tool result
        that failed and a tool result that is empty look identical once
        unwrapped, and confusing them would let an agent treat "the query
        broke" as "nothing matched" -- which is the difference between
        investigating and concluding.
        """
        self.connect()
        result = self.request("tools/call", {
            "name": name, "arguments": arguments or {},
        })
        texts = [
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        ]
        joined = "\n".join(t for t in texts if t)
        if result.get("isError"):
            raise McpToolError(f"{name} failed: {joined[:500]}")
        if not joined:
            return result.get("structuredContent")
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return joined


def grafana_client(env: dict[str, str]) -> McpClient:
    """The read-only Grafana surface, via a locally-run mcp-grafana.

    Local rather than the hosted mcp.grafana.com because the hosted server
    speaks OAuth 2.1 only -- there is no service-account path -- and because
    running it ourselves is what lets us pass `--disable-write`.
    """
    url = env.get("MCP_GRAFANA_URL", "http://localhost:8000/mcp")
    headers = {}
    if token := env.get("MCP_GRAFANA_SERVER_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return McpClient(url, headers, settle_s=PROXY_SETTLE_S)


def tempo_client(env: dict[str, str]) -> McpClient:
    """Grafana Cloud's first-party Tempo MCP, direct.

    mcp-grafana already proxies these tools under a `tempo_` prefix, so this
    exists for the cases where we want to bypass the proxy -- chiefly to prove
    that a result came from Tempo itself and not from something the proxy
    reshaped. HTTP Basic, not Bearer.
    """
    cred = f"{env['GRAFANA_TEMPO_USER_ID']}:{env['GRAFANA_TEMPO_TOKEN']}"
    basic = b64encode(cred.encode()).decode()
    return McpClient(env["GRAFANA_TEMPO_MCP_URL"],
                     {"Authorization": f"Basic {basic}"})
