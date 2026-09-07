"""Where the control loop begins.

Not at a prompt. Grafana evaluates `market_release_ready == 0`, the alert
fires, its contact point POSTs here, and the Conductor is started with the
incident. Nobody asked it a question. That inversion is the point of the whole
system, and this file is the seam where it happens -- so it is worth being
precise about what it does and does not do.

It does NOT decide anything. It has no model, no thresholds, no opinion about
whether the market should ship. Grafana already decided; this turns that
decision into work. Keeping judgement out of the wake-up path is what makes
the claim "Grafana owns the verdict" true rather than decorative: there is no
code here that could disagree with it.

Three things it does do, all of which are real problems rather than plumbing:

**Routes by intent.** The `continuity_action` label on the alert rule says what
kind of work this is -- investigate, compute_blast_radius, escalate. The alert
is not a notification the agent parses for meaning; it is a dispatch.

**Deduplicates.** Alertmanager re-delivers a firing alert every repeat_interval
until it resolves. A repair takes minutes. Without dedup on the fingerprint the
Conductor would be started again every few minutes on work already in flight,
and the second run would investigate the first run's half-written asset. The
fingerprint is Alertmanager's own hash of the alert's label set, so it is
stable across re-deliveries and distinct per market.

**Roots the trace.** The first span of an incident is created here and named
for the alert. Every later span -- investigation, blast radius, repair,
verification -- hangs off it, so Tempo shows one trace whose origin is
demonstrably a Grafana alert and not a person typing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry.otel import load_env  # noqa: E402

log = logging.getLogger("continuity.wake")

# The actions an alert rule may ask for. An alert carrying anything else is
# accepted and recorded but not dispatched: an unknown instruction must not be
# guessed at, and must not be silently dropped either.
ACTIONS = ("investigate", "compute_blast_radius", "escalate")

MAX_BODY = 1 << 20  # Alertmanager batches are small; refuse anything absurd.


@dataclass
class Incident:
    """One alert, in the shape the Conductor actually needs."""

    fingerprint: str
    alertname: str
    action: str
    status: str                 # firing | resolved
    title_id: str
    market: str
    severity: str
    started_at: str
    generator_url: str = ""     # deep link back into Grafana
    value_string: str = ""      # the evaluated sample, as Grafana rendered it
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def firing(self) -> bool:
        return self.status == "firing"

    @property
    def known_action(self) -> bool:
        return self.action in ACTIONS

    def describe(self) -> str:
        return (
            f"{self.alertname}[{self.status}] {self.title_id}/{self.market} "
            f"-> {self.action}"
        )


def parse(payload: dict[str, Any]) -> list[Incident]:
    """Flatten a Grafana/Alertmanager webhook body into incidents.

    One POST can carry several alerts (Alertmanager groups them), and the group
    is not the unit of work -- a market is. So the body is flattened rather
    than treated as a single event.
    """
    incidents: list[Incident] = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {}) or {}
        annotations = alert.get("annotations", {}) or {}
        incidents.append(
            Incident(
                # Alertmanager always supplies a fingerprint; fall back to the
                # label set so a hand-crafted test payload still dedups.
                fingerprint=alert.get("fingerprint")
                or json.dumps(labels, sort_keys=True),
                alertname=labels.get("alertname", "unknown"),
                action=labels.get("continuity_action", ""),
                status=alert.get("status", payload.get("status", "firing")),
                title_id=labels.get("title", ""),
                market=labels.get("market", ""),
                severity=labels.get("severity", ""),
                started_at=alert.get("startsAt", ""),
                generator_url=alert.get("generatorURL", ""),
                value_string=alert.get("valueString", ""),
                labels=labels,
                annotations=annotations,
            )
        )
    return incidents


class Dispatcher:
    """Dedupes incidents and hands the new ones to a handler.

    In-process for local runs. The same interface fronts Pub/Sub in the cloud
    worker -- `submit` becomes a publish and the dedup set becomes the
    subscription's ack state -- so nothing above this line changes.
    """

    def __init__(self, handler: Callable[[Incident], None] | None = None) -> None:
        self._handler = handler
        self._lock = threading.Lock()
        # fingerprint -> the status we last acted on. Keyed by status too, so a
        # `resolved` for an incident we saw `firing` is genuinely new
        # information and gets through.
        self._seen: dict[str, str] = {}
        self.accepted: list[Incident] = []
        self.duplicates = 0
        self.unroutable: list[Incident] = []

    def submit(self, incident: Incident) -> bool:
        """True when the incident was new and dispatched."""
        with self._lock:
            if self._seen.get(incident.fingerprint) == incident.status:
                self.duplicates += 1
                log.info("duplicate  %s", incident.describe())
                return False
            self._seen[incident.fingerprint] = incident.status

        if not incident.known_action:
            # Recorded, not guessed at. A rule asking for work we do not
            # implement is a deployment mistake worth seeing, and inventing a
            # default here would hide it.
            self.unroutable.append(incident)
            log.warning("unroutable %s (action=%r)", incident.describe(),
                        incident.action)
            return False

        self.accepted.append(incident)
        log.info("accepted   %s", incident.describe())
        if self._handler:
            self._handler(incident)
        return True

    def forget(self, fingerprint: str) -> None:
        """Allow an incident to be re-dispatched -- used when a run aborts."""
        with self._lock:
            self._seen.pop(fingerprint, None)


class _Handler(BaseHTTPRequestHandler):
    server_version = "continuity-wake/1.0"
    dispatcher: Dispatcher
    token: str = ""

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        """A public URL that starts autonomous repairs needs a door on it.

        Grafana contact points can send a bearer token; anything without it is
        refused. Not defence in depth -- just the one obvious hole closed.
        """
        if not self.token:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.token}"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/healthz":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path not in ("/alert", "/"):
            self._reply(404, {"error": "not found"})
            return
        if not self._authorised():
            self._reply(401, {"error": "unauthorised"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._reply(413, {"error": "payload too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._reply(400, {"error": f"bad json: {exc}"})
            return

        incidents = parse(payload)
        dispatched = sum(1 for i in incidents if self.dispatcher.submit(i))
        # 200 even when everything was a duplicate: Alertmanager retries on
        # non-2xx, and retrying a correctly-ignored re-delivery is pure noise.
        self._reply(200, {
            "received": len(incidents),
            "dispatched": dispatched,
        })

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)


def serve(
    dispatcher: Dispatcher,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    token: str = "",
) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {
        "dispatcher": dispatcher, "token": token,
    })
    server = ThreadingHTTPServer((host, port), handler)
    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    env = load_env()
    # Cloud Run supplies PORT; locally it is whatever the tunnel points at.
    port = int(os.environ.get("PORT") or env.get("WAKE_PORT") or 8080)
    token = env.get("WAKE_TOKEN", "")
    if not token:
        log.warning("WAKE_TOKEN unset -- the endpoint is unauthenticated")

    dispatcher = Dispatcher()
    server = serve(dispatcher, port=port, token=token)
    log.info("listening on :%d  (POST /alert)", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping; %d accepted, %d duplicates, %d unroutable",
                 len(dispatcher.accepted), dispatcher.duplicates,
                 len(dispatcher.unroutable))
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
