"""Dub synchronisation measurement.

Named `dub_sync_offset_ms`, not "lip-sync score", because it measures what it
actually measures: how far each dubbed utterance's boundaries drift from the
corresponding utterance in the source master. That is *isochrony* -- the thing
dubbing editors actually fit to.

True viseme-level lip-sync would require a phoneme aligner. We do not have one,
we do not claim one, and it is listed in docs/LIMITATIONS.md as future work.

The measurement is fully deterministic: same bytes in, same number out.
"""

from __future__ import annotations

import statistics
from pathlib import Path

from .ffmpeg import voiced_intervals
from .types import Interval, Measurement, ProbeError

METHOD = "ffmpeg_silencedetect_isochrony_v1"


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Stdlib-only and stable for tiny samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100.0 * len(ordered))))
    return ordered[rank - 1]


def align(
    reference: list[Interval], measured: list[Interval]
) -> tuple[list[tuple[Interval, Interval]], str | None]:
    """Pair reference utterances with dubbed ones, by index.

    Index alignment is legitimate here because we *generated* the dub from the
    reference, one utterance at a time -- we know how many there should be. A
    count mismatch is therefore not something to paper over with fuzzy matching;
    it is a finding in its own right, and it is returned as an anomaly.
    """
    anomaly: str | None = None
    if len(reference) != len(measured):
        anomaly = (
            f"utterance_count_mismatch: reference={len(reference)} "
            f"measured={len(measured)}"
        )
    pairs = list(zip(reference, measured))
    if not pairs:
        raise ProbeError("no utterances to align")
    return pairs, anomaly


def sync_offset(
    reference: list[Interval], measured: list[Interval]
) -> Measurement:
    """Worst-case absolute onset drift across the scene, in milliseconds.

    We report the max because a release blocks on its worst moment, not its
    average one. p95 and the per-utterance detail ride along so the agent can
    tell "one bad line" from "the whole scene has slipped" -- which selects a
    completely different repair strategy.
    """
    pairs, anomaly = align(reference, measured)

    onsets = [abs(m.start_ms - r.start_ms) for r, m in pairs]
    overhangs = [m.end_ms - r.end_ms for r, m in pairs]

    worst_idx = onsets.index(max(onsets))

    return Measurement(
        key="delivery.dub_sync_offset_ms",
        value=round(max(onsets), 1),
        unit="ms",
        method=METHOD,
        detail={
            "utterances": len(pairs),
            "p95_onset_ms": round(_percentile(onsets, 95), 1),
            "mean_onset_ms": round(statistics.fmean(onsets), 1),
            "max_end_overhang_ms": round(max(overhangs, key=abs), 1),
            "worst_utterance_index": worst_idx,
            "worst_utterance_ref": repr(pairs[worst_idx][0]),
            "worst_utterance_dub": repr(pairs[worst_idx][1]),
            "drift_is_systematic": _is_systematic(onsets),
            "anomaly": anomaly,
        },
    )


def _is_systematic(onsets: list[float]) -> bool:
    """True when every utterance drifts by a similar amount.

    Systematic drift means the whole stem is offset -- a muxing or padding fault,
    fixable by shifting the track. Scattered drift means individual lines do not
    fit their slots, which is a script-length problem no amount of shifting will
    solve. The agent branches on exactly this.
    """
    if len(onsets) < 3:
        return False
    spread = statistics.pstdev(onsets)
    return spread < 0.25 * statistics.fmean(onsets) if statistics.fmean(onsets) else False


def speech_rate_wpm(measured: list[Interval], word_count: int) -> Measurement:
    """Words per minute over voiced time only.

    Silence is excluded deliberately: a line delivered fast with long pauses
    around it is still delivered fast, and that is what makes it sound rushed.
    This is the signal that reveals why naive retiming fails on German.
    """
    voiced_ms = sum(i.duration_ms for i in measured)
    if voiced_ms <= 0:
        raise ProbeError("no voiced audio; cannot compute speech rate")

    wpm = word_count / (voiced_ms / 60_000.0)
    return Measurement(
        key="quality.speech_rate_wpm",
        value=round(wpm, 1),
        unit="wpm",
        method=METHOD,
        detail={
            "word_count": word_count,
            "voiced_ms": round(voiced_ms, 1),
            "utterances": len(measured),
        },
    )


def measure_dub(
    dub_path: Path,
    reference: list[Interval],
    word_count: int,
    noise_db: float = -35.0,
) -> list[Measurement]:
    """Full sync probe for one dubbed stem. Returns every measurement it made."""
    measured = voiced_intervals(dub_path, noise_db=noise_db)
    return [
        sync_offset(reference, measured),
        speech_rate_wpm(measured, word_count),
    ]
