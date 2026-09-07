"""The Signal Agent: the system's senses, and its only reader of Grafana.

Every other agent is blind. If the Conductor wants to know whether de-DE can
ship, it asks this; if the Verifier wants to know whether a repair moved a
number, it asks this. That concentration is deliberate and has three effects
worth the coupling:

  - **One place holds MCP credentials.** The read-only guarantee is enforced by
    running mcp-grafana with `--disable-write` (66 tools registered, zero of
    them create/update/patch/delete), and it only has to be true here.
  - **No agent can invent an observation.** Every method returns `Evidence`,
    never a bare float. A number that reaches a decision therefore always
    arrives attached to the query that produced it, and a judge can paste that
    query into Grafana and get the same answer.
  - **The queries are reviewable.** They live in one file, in PromQL and
    TraceQL, rather than being assembled inside prompts where nobody can diff
    them.

The methods that return `None` do so on purpose. An absent series is not zero
and not a failure -- it is "we did not measure this", which the verdict already
treats as blocking. Returning 0.0 here would quietly convert a missing
measurement into a passing one somewhere downstream.
"""

from __future__ import annotations

from typing import Any, Iterable

from agents.contracts import (
    AffectedAsset,
    Evidence,
    Finding,
    ImpactSet,
)
from agents.mcp import McpClient, McpToolError
from telemetry.metrics import RELEASE_READY, REPAIRS

PROM_UID = "grafanacloud-prom"
TEMPO_UID = "grafanacloud-traces"

# Span attribute carrying the hash of the parent an asset was built from. The
# whole provenance graph hangs off this one key; see telemetry/otel.py.
ATTR_PARENT_SHA = "continuity.asset.parent_sha256"


def _labels(**kw: str) -> str:
    """PromQL label selector from non-empty keywords."""
    parts = [f'{k}="{v}"' for k, v in sorted(kw.items()) if v]
    return "{" + ",".join(parts) + "}" if parts else ""


class Signal:
    def __init__(
        self,
        client: McpClient,
        *,
        datasource_uid: str = PROM_UID,
        tempo_uid: str = TEMPO_UID,
    ) -> None:
        self.client = client
        self.uid = datasource_uid
        self.tempo_uid = tempo_uid

    # -- Prometheus --------------------------------------------------------

    def samples(self, expr: str) -> list[dict[str, Any]]:
        """Raw instant-query samples. Prefer the Evidence-returning methods.

        `endTime` is required even for an instant query -- omitting it fails
        with a time-parse error rather than defaulting to now, which cost an
        afternoon to discover and is why the probe grew a `schema` command.
        """
        result = self.client.call("query_prometheus", {
            "datasourceUid": self.uid,
            "expr": expr,
            "queryType": "instant",
            "startTime": "now",
            "endTime": "now",
        })
        if isinstance(result, dict):
            return result.get("data") or []
        return []

    def observe(self, expr: str, *, detail: dict | None = None) -> Evidence | None:
        """One scalar, as Evidence. `None` when the series is absent.

        Absent is a distinct answer from zero. The caller has to decide what a
        missing measurement means; this refuses to decide for it.
        """
        samples = self.samples(expr)
        if not samples:
            return None
        value = samples[0].get("value")
        if not value or len(value) < 2:
            return None
        return Evidence(
            kind="metric", query=expr, value=float(value[1]),
            source=self.uid, detail=detail or {},
        )

    def observe_all(self, expr: str) -> list[Evidence]:
        """One Evidence per returned series, each carrying its own labels."""
        out: list[Evidence] = []
        for sample in self.samples(expr):
            value = sample.get("value")
            if not value or len(value) < 2:
                continue
            metric = sample.get("metric", {})
            out.append(Evidence(
                kind="metric", query=expr, value=float(value[1]),
                source=self.uid,
                detail={k: v for k, v in metric.items()
                        if k in ("market", "title", "scene", "requirement",
                                 "strategy", "outcome")},
            ))
        return out

    # -- the questions the agents actually ask ------------------------------

    def verdict(self, title: str, market: str) -> Evidence | None:
        """Can this market ship? Grafana's answer, not ours."""
        return self.observe(f"{RELEASE_READY}{_labels(title=title, market=market)}")

    def measurement(
        self, series: str, *, title: str, market: str, scene: str = ""
    ) -> Evidence | None:
        return self.observe(
            f"{series}{_labels(title=title, market=market, scene=scene)}"
        )

    def threshold(self, market: str, requirement: str) -> Evidence | None:
        """The bar this market is judged against.

        Worth its own method because an agent asking what standard applies --
        rather than being told in a prompt -- is a legitimate investigative
        step, and one the thresholds-as-metrics design exists to enable.
        """
        return self.observe(
            f"market_threshold{_labels(market=market, requirement=requirement)}"
        )

    def failing_requirements(self, title: str, market: str) -> list[Finding]:
        """Which checks are failing, each with the measurement AND the bar.

        Two pieces of evidence per finding, because "480" is not a diagnosis.
        "480 against a 120 limit" is.
        """
        selector = _labels(title=title, market=market)
        findings: list[Finding] = []
        for evidence in self.observe_all(f"market_requirement_met{selector} == 0"):
            requirement = evidence.detail.get("requirement", "unknown")
            support = [evidence]
            if bar := self.threshold(market, _THRESHOLD_FOR.get(requirement, "")):
                support.append(bar)
            findings.append(Finding(
                claim=f"{market} fails {requirement} for {title}",
                evidence=support,
                subject=requirement,
            ))
        return findings

    def missing_coverage(self, title: str, market: str) -> Finding | None:
        """The check the verdict added: is anything simply unmeasured?

        A market can have nothing failing and still be unshippable, and that
        case reads as healthy on every dashboard unless something asks.
        """
        selector = _labels(title=title, market=market)
        present = self.observe(f"market_checks_present{selector}")
        owed = self.observe(f"market_required_checks{_labels(market=market)}")
        if present is None or owed is None:
            return None
        if float(present.value) >= float(owed.value):
            return None
        return Finding(
            claim=(
                f"{market} has only {present.value:g} of {owed.value:g} required "
                f"checks for {title}; the rest were never measured"
            ),
            evidence=[present, owed],
            subject="coverage",
        )

    def repair_history(self, strategy: str, market: str) -> dict[str, int]:
        """Outcome counts for a strategy, for `earned_tier`.

        Read from Prometheus at decision time rather than carried in agent
        memory. The ledger is external, append-only and visible, so autonomy
        cannot be argued upward inside a prompt -- and a judge can watch a
        strategy lose its privileges live by making it fail twice.
        """
        expr = (
            f"sum by (outcome) ({REPAIRS}"
            f'{_labels(strategy=strategy, market=market)})'
        )
        counts = {"succeeded": 0, "lucky": 0, "failed": 0}
        for evidence in self.observe_all(expr):
            outcome = evidence.detail.get("outcome", "")
            if outcome in counts:
                counts[outcome] = int(float(evidence.value))
        return counts

    # -- Tempo -------------------------------------------------------------

    def blast_radius(
        self,
        parent_sha: str,
        *,
        considered: int,
        limit: int = 100,
    ) -> ImpactSet:
        """Everything built from a given parent hash, from the trace graph.

        This is why traces carry `continuity.asset.parent_sha256`. Membership
        is a TraceQL search rather than a stored edge list, so nothing has to
        be invalidated when a master changes -- the query simply returns
        different spans.

        `considered` is passed in rather than derived because the denominator
        belongs to the caller's asset store. "We regenerated 4 of 27" is the
        claim that makes this valuable, and Tempo cannot know the 27.
        """
        query = f'{{ .{ATTR_PARENT_SHA} = "{parent_sha}" }}'
        try:
            result = self.client.call("tempo_traceql-search", {
                "datasourceUid": self.tempo_uid,
                "query": query,
                "limit": limit,
            })
        except McpToolError as exc:
            raise McpToolError(
                f"blast radius query failed for {parent_sha[:12]}: {exc}"
            ) from exc

        affected = list(_spans_to_assets(result))
        return ImpactSet(
            root_asset_id="",
            root_sha256=parent_sha,
            affected=affected,
            evidence=[Evidence(
                kind="trace", query=query, value=len(affected),
                source=self.tempo_uid,
            )],
            considered=max(considered, len(affected)),
        )


# requirement label -> the `market_threshold` requirement that judges it.
# Separate from the recording rules' own mapping because this one exists to
# make a finding readable, and a missing entry must degrade to one-sided
# evidence rather than raise.
_THRESHOLD_FOR = {
    "dub_sync": "dub_sync_max_ms",
    "line_overrun": "line_overrun_max_ms",
    "subtitle_rate": "subtitle_max_cps",
    "true_peak": "true_peak_max_dbtp",
    "speech_rate": "speech_rate_max_wpm",
    "semantic_fidelity": "semantic_fidelity_floor",
    "loudness": "loudness_target_lufs",
}


def _spans_to_assets(result: Any) -> Iterable[AffectedAsset]:
    """Flatten a TraceQL search result into assets.

    Tempo's search response nests spans inside spanSets inside traces, and the
    attributes we care about ride on the span. Written defensively because the
    Traces MCP server is in public preview and the shape may move; a missing
    field yields an asset with blank identity rather than an exception, so a
    schema change degrades the blast radius instead of aborting the incident.
    """
    traces = []
    if isinstance(result, dict):
        traces = result.get("traces") or result.get("data") or []
    elif isinstance(result, list):
        traces = result

    for trace in traces:
        if not isinstance(trace, dict):
            continue
        trace_id = trace.get("traceID") or trace.get("traceId") or ""
        span_sets = trace.get("spanSets") or (
            [trace["spanSet"]] if trace.get("spanSet") else []
        )
        for span_set in span_sets:
            for span in span_set.get("spans", []) or []:
                attrs = {
                    a.get("key"): _attr_value(a.get("value"))
                    for a in span.get("attributes", []) or []
                }
                yield AffectedAsset(
                    asset_id=attrs.get("continuity.asset.id", ""),
                    sha256=attrs.get("continuity.asset.sha256", ""),
                    kind=attrs.get("continuity.asset.kind", ""),
                    market=attrs.get("continuity.market") or None,
                    via_span_id=span.get("spanID") or span.get("spanId") or "",
                    trace_id=trace_id,
                )


def _attr_value(value: Any) -> str:
    """OTLP attribute values arrive as a one-key type envelope."""
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                return str(value[key])
        return ""
    return "" if value is None else str(value)
