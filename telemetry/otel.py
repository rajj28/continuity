"""OpenTelemetry wiring and Continuity's own semantic conventions.

Two jobs:

1. Emit the media pipeline as a trace whose spans carry asset identity. A
   release build is ONE trace: root = release candidate, children = scene ->
   transcript -> translation -> tts -> mux -> qc. Every span records the asset
   it produced and the hash of the parent it consumed, which is what turns
   Tempo into a queryable provenance graph rather than a latency dashboard.

2. Emit the perceptual measurements as metrics, because Prometheus is the only
   surface that can carry an alert -- TraceQL metrics are capped at a 24 h
   window and are not a Grafana-managed alert source. Traces investigate;
   metrics decide.

The attribute that does the real work is `continuity.asset.parent_sha256`.
Blast radius is a TraceQL search on it:

    { .continuity.asset.parent_sha256 = "<old master hash>" }
"""

from __future__ import annotations

import os
from base64 import b64encode
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

ROOT = Path(__file__).resolve().parents[1]

# ---- Continuity semantic conventions -------------------------------------
# Namespaced so they never collide with OTel's own or with gen_ai.*, and so a
# TraceQL query can select purely on our attributes.
ATTR_TITLE = "continuity.title_id"
ATTR_SCENE = "continuity.scene_id"
ATTR_MARKET = "continuity.market"
ATTR_ASSET_ID = "continuity.asset.id"
ATTR_ASSET_KIND = "continuity.asset.kind"
ATTR_ASSET_SHA = "continuity.asset.sha256"
ATTR_PARENT_SHA = "continuity.asset.parent_sha256"
ATTR_PARENT_ID = "continuity.asset.parent_id"
ATTR_STRATEGY = "continuity.repair.strategy"
ATTR_STAGE = "continuity.stage"

_initialised = False


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


def _auth_header(env: dict[str, str]) -> dict[str, str]:
    """Grafana Cloud OTLP uses HTTP Basic of `<instance id>:<token>`.

    Grafana's onboarding snippet prints the bare base64 without the
    `Authorization=Basic ` prefix the env var actually needs, so we build the
    header ourselves rather than pasting theirs.
    """
    cred = f"{env['OTLP_INSTANCE_ID']}:{env['OTLP_TOKEN']}"
    return {"Authorization": f"Basic {b64encode(cred.encode()).decode()}"}


def setup(
    service_name: str = "continuity-pipeline",
    *,
    export_interval_ms: int = 5_000,
) -> tuple[trace.Tracer, metrics.Meter]:
    """Configure global providers pointed at Grafana Cloud. Idempotent."""
    global _initialised
    env = load_env()

    if not _initialised:
        endpoint = env["OTLP_ENDPOINT"].rstrip("/")
        headers = _auth_header(env)
        resource = Resource.create({
            "service.name": service_name,
            "service.namespace": "continuity",
            "deployment.environment": env.get("ENVIRONMENT", "dev"),
        })

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
            )
        )
        trace.set_tracer_provider(tracer_provider)

        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{endpoint}/v1/metrics", headers=headers
                    ),
                    export_interval_millis=export_interval_ms,
                )
            ],
        )
        metrics.set_meter_provider(meter_provider)
        _initialised = True

    return trace.get_tracer("continuity"), metrics.get_meter("continuity")


def shutdown() -> None:
    """Flush both pipelines. Short-lived Cloud Run jobs MUST call this or their
    telemetry dies with the process."""
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            provider.shutdown()


# ---- span helpers ---------------------------------------------------------

@contextmanager
def asset_span(
    tracer: trace.Tracer,
    name: str,
    *,
    stage: str,
    title_id: str,
    scene_id: str | None = None,
    market: str | None = None,
    asset_id: str | None = None,
    asset_kind: str | None = None,
    asset_sha: str | None = None,
    parents: list[tuple[str, str]] | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """A span that records which asset it produced and what it consumed.

    `parents` is a list of (asset_id, sha256). Multiple parent hashes are
    recorded as an array attribute so a single TraceQL predicate on
    `continuity.asset.parent_sha256` finds this span regardless of which
    parent changed.
    """
    attrs: dict[str, Any] = {ATTR_STAGE: stage, ATTR_TITLE: title_id}
    if scene_id:
        attrs[ATTR_SCENE] = scene_id
    if market:
        attrs[ATTR_MARKET] = market
    if asset_id:
        attrs[ATTR_ASSET_ID] = asset_id
    if asset_kind:
        attrs[ATTR_ASSET_KIND] = asset_kind
    if asset_sha:
        attrs[ATTR_ASSET_SHA] = asset_sha
    if parents:
        attrs[ATTR_PARENT_ID] = [p[0] for p in parents]
        attrs[ATTR_PARENT_SHA] = [p[1] for p in parents]
    if extra:
        attrs.update(extra)

    with tracer.start_as_current_span(name, attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


def record_measurement(span: Span, measurement: Any) -> None:
    """Attach a QC Measurement to the span that produced it, so a trace alone
    explains why an asset failed -- no join required."""
    span.set_attribute(f"continuity.measure.{measurement.key}", measurement.value)
    span.set_attribute(f"continuity.measure.{measurement.key}.unit", measurement.unit)
    span.set_attribute(
        f"continuity.measure.{measurement.key}.method", measurement.method
    )
