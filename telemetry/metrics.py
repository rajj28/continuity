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
