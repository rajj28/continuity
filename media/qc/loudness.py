"""Programme loudness probe (EBU R128 / ITU-R BS.1770 family).

This is the cleanest auto-fixable blocker in the system: loudness is objective,
the remedy is a single deterministic ffmpeg pass, and the result is verifiable
by re-measuring. It is the first repair strategy to earn L1 autonomy.
"""

from __future__ import annotations

from pathlib import Path

from .ffmpeg import loudness as _loudness
from .types import Measurement

METHOD = "ffmpeg_loudnorm_r128_v1"


def measure_loudness(path: Path) -> list[Measurement]:
    raw = _loudness(path)
    return [
        Measurement(
            key="delivery.audio_loudness_lufs",
            value=round(raw["integrated_lufs"], 2),
            unit="LUFS",
            method=METHOD,
            detail={
                "lra_lu": round(raw["lra_lu"], 2),
                "gating_threshold_lufs": round(raw["threshold_lufs"], 2),
            },
        ),
        Measurement(
            key="delivery.audio_true_peak_dbtp",
            value=round(raw["true_peak_dbtp"], 2),
            unit="dBTP",
            method=METHOD,
            detail={},
        ),
    ]
