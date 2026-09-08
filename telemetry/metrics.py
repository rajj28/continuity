"""The metric registry -- one source of truth for every series name.

Metric names are referenced from four places that must agree exactly: this
code, the Mimir recording rules, the Grafana alert rules and dashboards, and
the PromQL the agent writes at runtime. A silent rename breaks the verdict, so
the names live here and nowhere else.

## Why every instrument declares unit=""

Verified empirically against Grafana Cloud on 3 September 2026 -- the
OTel-to-Prometheus conversion appends a full-word unit suffix:

    instrument "nt_offset", unit "ms"   ->  nt_offset_milliseconds
    instrument "nt_ready",  unit "1"    ->  nt_ready_ratio
    instrument "nt_stale",  unit ""     ->  nt_stale          (unchanged)

Several of our units (LUFS, dBTP, cps, wpm) have no OTel mapping anyway. So we
carry the unit in the metric NAME, set the instrument unit empty, and get names
that are exactly what we wrote. The machine-readable unit is not lost: it lives
on the QC `Measurement` that produced the value.

## Cardinality

Labels are {title, scene, market} only -- deliberately not asset_version, which
would multiply series on every repair. At 1 title x 5 scenes x 5 markets x the
metrics below we sit near 500 active series, well inside the 10k free-tier
budget with room for the demo to run many times.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics

# ---- canonical series names ----------------------------------------------

SYNC_OFFSET = "dub_sync_offset_ms"
LINE_OVERRUN = "dub_line_overrun_ms"
AD_COLLISION = "ad_collision_ms"
AD_COVERAGE = "ad_coverage_ratio"
SPEECH_RATE = "speech_rate_wpm"
SUBTITLE_RATE = "subtitle_reading_rate_cps"
LOUDNESS = "audio_loudness_lufs"
TRUE_PEAK = "audio_true_peak_dbtp"
FIDELITY = "semantic_fidelity_score"

ASSET_STALE = "asset_stale"
REQUIREMENT_MET = "market_requirement_met"
RELEASE_READY = "market_release_ready"          # produced by a recording rule

REPAIRS = "continuity_repairs_total"
MCP_CALLS = "continuity_mcp_calls_total"
BRANCHES = "continuity_mcp_decision_branches_total"
ASSETS_PRESERVED = "continuity_assets_preserved_total"
ASSETS_REGENERATED = "continuity_assets_regenerated_total"

# QC measurement key -> series name. Adding a probe without adding it here is a
# hard error, so a measurement can never be silently unexported.
MEASUREMENT_SERIES: dict[str, str] = {
    "delivery.dub_sync_offset_ms": SYNC_OFFSET,
    "delivery.line_overrun_ms": LINE_OVERRUN,
    "accessibility.ad_collision_ms": AD_COLLISION,
    "accessibility.ad_coverage_ratio": AD_COVERAGE,
    "delivery.subtitle_reading_rate_cps": SUBTITLE_RATE,
    "delivery.audio_loudness_lufs": LOUDNESS,
    "delivery.audio_true_peak_dbtp": TRUE_PEAK,
    "quality.speech_rate_wpm": SPEECH_RATE,
    "quality.semantic_fidelity_score": FIDELITY,
}


class UnexportedMeasurement(KeyError):
    """A measurement with no series mapping. Fail loudly rather than drop it."""


class Instruments:
    """Lazily-created instruments, one per canonical name."""

    def __init__(self, meter: metrics.Meter) -> None:
        self._meter = meter
        self._gauges: dict[str, Any] = {}
        self._counters: dict[str, Any] = {}

    def gauge(self, name: str, description: str = "") -> Any:
        if name not in self._gauges:
            self._gauges[name] = self._meter.create_gauge(
                name, unit="", description=description
            )
        return self._gauges[name]

    def counter(self, name: str, description: str = "") -> Any:
        if name not in self._counters:
            self._counters[name] = self._meter.create_counter(
                name, unit="", description=description
            )
        return self._counters[name]

    # -- domain helpers ----------------------------------------------------

    def record_measurement(
        self, measurement: Any, *, title: str, scene: str, market: str
    ) -> str:
        """Export one QC Measurement under its canonical series name."""
        key = measurement.key
        if key not in MEASUREMENT_SERIES:
            raise UnexportedMeasurement(
                f"no series mapping for {key!r} -- add it to MEASUREMENT_SERIES"
            )
        series = MEASUREMENT_SERIES[key]
        self.gauge(series, f"{key} ({measurement.unit}), via {measurement.method}").set(
            measurement.value, {"title": title, "scene": scene, "market": market}
        )
        return series

    def set_stale(self, stale: bool, *, title: str, scene: str, market: str) -> None:
        self.gauge(
            ASSET_STALE,
            "1 when an asset's recorded parent hash no longer matches the parent",
        ).set(1 if stale else 0, {"title": title, "scene": scene, "market": market})

    def set_requirement(
        self, met: bool, *, title: str, market: str, requirement: str
    ) -> None:
        self.gauge(
            REQUIREMENT_MET, "1 when a market requirement is currently satisfied"
        ).set(
            1 if met else 0,
            {"title": title, "market": market, "requirement": requirement},
        )

    def repair(self, *, strategy: str, outcome: str, market: str) -> None:
        """The series that decides how much autonomy a strategy has earned."""
        self.counter(
            REPAIRS, "Repair attempts by strategy and verified outcome"
        ).add(1, {"strategy": strategy, "outcome": outcome, "market": market})


# ---- diagnostics ----------------------------------------------------------
# Signals that explain a failure without themselves being pass/fail criteria.
#
# They are kept strictly apart from MEASUREMENT_SERIES because nothing joins
# them in the recording rules and nothing should: "is the drift systematic" is
# not a requirement a market can fail, it is the fact that decides WHICH repair
# could possibly work. Publishing them anyway matters because it keeps the
# agent reasoning from facts that are in Grafana, visible on a dashboard and
# checkable by a human, rather than from a detail dict only it can see.
#
# Adding one here has no effect on market_required_checks, so the coverage gate
# is unaffected by diagnostic depth.

DRIFT_SYSTEMATIC = "dub_drift_systematic"
SYNC_P95 = "dub_sync_p95_ms"
# Positive means the dub arrives LATE. The unsigned dub_sync_offset_ms says
# how far off it is; this says which way, and RETIME cannot choose a shift
# direction without it.
SYNC_SIGNED = "dub_sync_signed_ms"
END_OVERHANG = "dub_end_overhang_ms"
UTTERANCES = "dub_utterances"
WORST_OVERRUN = "dub_worst_overrun_ms"
OVERRUNNING_LINES = "dub_overrunning_lines"

# Key inside a Measurement.detail -> series name.
DIAGNOSTIC_SERIES: dict[str, str] = {
    "drift_is_systematic": DRIFT_SYSTEMATIC,
    "overrunning_lines": OVERRUNNING_LINES,
    "p95_onset_ms": SYNC_P95,
    "mean_signed_onset_ms": SYNC_SIGNED,
    "max_end_overhang_ms": END_OVERHANG,
    "utterances": UTTERANCES,
}

# Key inside a QCReport.detail -> series name.
REPORT_DIAGNOSTIC_SERIES: dict[str, str] = {
    "worst_overrun_ms": WORST_OVERRUN,
    "overrunning_lines": OVERRUNNING_LINES,
}


def _numeric(value: Any) -> float | None:
    """Coerce a diagnostic to a number, or decline.

    Booleans become 1/0 and lists become their length, because "which lines
    overran" is a list on disk and "how many overran" is what a time series can
    carry. Anything else -- a string, a nested dict -- is skipped rather than
    stringified into a label, which is how cardinality explodes.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple, set)):
        return float(len(value))
    return None


def record_diagnostics(instruments: "Instruments", detail: dict, mapping: dict,
                    labels: dict) -> int:
    published = 0
    for key, series in mapping.items():
        if key not in detail:
            continue
        value = _numeric(detail[key])
        if value is None:
            continue
        instruments.gauge(series, f"diagnostic: {key}").set(value, labels)
        published += 1
    return published
