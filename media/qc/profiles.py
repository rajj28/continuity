"""Market profiles and the Measurement -> Check comparison.

The threshold a release is judged by must be loadable, diffable and versioned.
It is deliberately not derivable from a model and not editable at runtime.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .types import Check, Measurement

_PROFILES = Path(__file__).resolve().parents[2] / "assets" / "market_profiles.json"

# Which threshold each measurement is judged against, and in which direction.
# Adding a probe without adding a rule here is a hard error, not a silent pass.
_RULES: dict[str, tuple[str, str]] = {
    "delivery.dub_sync_offset_ms":        ("delivery.sync_tolerance_ms", "lte"),
    "delivery.subtitle_reading_rate_cps": ("delivery.subtitle_max_cps", "lte"),
    "delivery.subtitle_min_duration_ms":  ("delivery.subtitle_min_duration_ms", "gte"),
    "delivery.subtitle_max_lines":        ("delivery.subtitle_max_lines", "lte"),
    "delivery.subtitle_max_line_chars":   ("delivery.subtitle_max_line_chars", "lte"),
    "delivery.subtitle_min_gap_ms":       ("delivery.subtitle_min_gap_ms", "gte"),
    "delivery.audio_true_peak_dbtp":      ("delivery.true_peak_max_dbtp", "lte"),
    "quality.speech_rate_wpm":            ("quality.speech_rate_max_wpm", "lte"),
    "quality.semantic_fidelity_score":    ("quality.semantic_fidelity_floor", "gte"),
}


class UnknownMeasurement(KeyError):
    """A probe emitted a key with no threshold rule. Fail loudly: an
    unjudged measurement would otherwise look like a pass."""


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, Any]:
    return json.loads(_PROFILES.read_text(encoding="utf-8"))["markets"]


def profile(market: str) -> dict[str, Any]:
    profiles = load_profiles()
    if market not in profiles:
        raise KeyError(f"unknown market {market!r}; known: {sorted(profiles)}")
    return profiles[market]


def _dig(obj: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        obj = obj[part]
    return obj


def check(measurement: Measurement, market: str) -> Check:
    """Judge one measurement against one market's rules."""
    if measurement.key not in _RULES:
        raise UnknownMeasurement(
            f"no threshold rule for {measurement.key!r} -- add it to _RULES"
        )
    path, comparator = _RULES[measurement.key]
    return Check(
        measurement=measurement,
        threshold=float(_dig(profile(market), path)),
        comparator=comparator,  # type: ignore[arg-type]
    )


def check_loudness(measurement: Measurement, market: str) -> Check:
    """Loudness is a two-sided tolerance band, so it gets its own comparison:
    we judge absolute deviation from target against the allowed tolerance."""
    d = profile(market)["delivery"]
    deviation = abs(measurement.value - float(d["loudness_target_lufs"]))
    return Check(
        measurement=Measurement(
            key="delivery.audio_loudness_deviation_lu",
            value=round(deviation, 2),
            unit="LUFS",
            method=measurement.method,
            detail={
                "measured_lufs": measurement.value,
                "target_lufs": d["loudness_target_lufs"],
                **measurement.detail,
            },
        ),
        threshold=float(d["loudness_tolerance_lu"]),
        comparator="lte",
    )


def check_all(measurements: list[Measurement], market: str) -> list[Check]:
    """Judge a probe's full output. Loudness is routed to its band comparison."""
    out: list[Check] = []
    for m in measurements:
        if m.key == "delivery.audio_loudness_lufs":
            out.append(check_loudness(m, market))
        else:
            out.append(check(m, market))
    return out
