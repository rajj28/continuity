"""Core measurement types.

Design rule that governs this whole package: a probe returns a Measurement --
a number, with a unit, and the name of the method that produced it. It never
returns a bare bool, and it never returns a model's opinion.

Pass/fail is a separate, explicit comparison against a MarketProfile threshold,
so that the threshold is always visible next to the value that was judged by it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Unit = Literal["ms", "cps", "LUFS", "dBTP", "wpm", "count", "ratio", "s"]


@dataclass(frozen=True)
class Measurement:
    """One deterministic observation of a media artifact."""

    key: str                     # e.g. "delivery.dub_sync_offset_ms"
    value: float
    unit: Unit
    method: str                  # e.g. "ffmpeg_silencedetect_v1"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Check:
    """A Measurement compared against a threshold. This is what blocks a release."""

    measurement: Measurement
    threshold: float
    comparator: Literal["lte", "gte", "lt", "gt"]

    @property
    def passed(self) -> bool:
        v, t = self.measurement.value, self.threshold
        return {
            "lte": v <= t,
            "gte": v >= t,
            "lt": v < t,
            "gt": v > t,
        }[self.comparator]

    @property
    def margin(self) -> float:
        """Signed headroom. Negative means failing, and by how much."""
        v, t = self.measurement.value, self.threshold
        return (t - v) if self.comparator in ("lte", "lt") else (v - t)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.measurement.to_dict(),
            "threshold": self.threshold,
            "comparator": self.comparator,
            "passed": self.passed,
            "margin": round(self.margin, 4),
        }


@dataclass(frozen=True)
class Interval:
    """A half-open time span in milliseconds."""

    start_ms: float
    end_ms: float

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

    def __repr__(self) -> str:  # keeps golden-file diffs readable
        return f"Interval({self.start_ms:.0f}, {self.end_ms:.0f})"


class ProbeError(RuntimeError):
    """A probe could not measure. Never silently degrades to a passing value."""
