"""Thin, deterministic wrappers over ffmpeg/ffprobe.

Everything here shells out and parses text. No models, no heuristics beyond
documented ffmpeg filter behaviour, so every result is reproducible from the
same input bytes -- which is what lets these numbers carry an SLO.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .types import Interval, ProbeError

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise ProbeError(f"{tool} not found on PATH")
    return path


def _run(args: list[str]) -> str:
    """ffmpeg writes its analysis to stderr; we merge and return everything."""
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return (proc.stdout or "") + (proc.stderr or "")


def duration_ms(path: Path) -> float:
    """Container duration via ffprobe."""
    out = _run([
        _require("ffprobe"), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    try:
        return float(json.loads(out)["format"]["duration"]) * 1000.0
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise ProbeError(f"could not read duration of {path.name}: {exc}") from exc


def silence_intervals(
    path: Path, noise_db: float = -35.0, min_silence_s: float = 0.15
) -> list[Interval]:
    """Silent spans, via the ffmpeg `silencedetect` filter.

    Defaults follow common dialogue-editing practice: -35 dBFS noise floor and a
    150 ms minimum, which is short enough to sit inside a natural inter-word gap
    without splitting a held vowel.
    """
    out = _run([
        _require("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path),
        "-af", f"silencedetect=n={noise_db}dB:d={min_silence_s}",
        "-f", "null", "-",
    ])

    starts = [float(m) * 1000.0 for m in _SILENCE_START.findall(out)]
    ends = [float(m) * 1000.0 for m in _SILENCE_END.findall(out)]

    if not starts and not ends:
        return []

    total = duration_ms(path)
    # A silence open at EOF has a start with no matching end.
    if len(starts) == len(ends) + 1:
        ends = ends + [total]
    if len(starts) != len(ends):
        raise ProbeError(
            f"unbalanced silencedetect output for {path.name}: "
            f"{len(starts)} starts, {len(ends)} ends"
        )

    return [Interval(max(0.0, s), min(total, e)) for s, e in zip(starts, ends)]


def voiced_intervals(
    path: Path,
    noise_db: float = -35.0,
    min_silence_s: float = 0.15,
    min_utterance_ms: float = 120.0,
) -> list[Interval]:
    """Speech spans -- the complement of the silences, within the file duration.

    `min_utterance_ms` discards clicks and breath artifacts that would otherwise
    be counted as utterances and corrupt the alignment in sync.py.
    """
    total = duration_ms(path)
    silences = silence_intervals(path, noise_db, min_silence_s)

    if not silences:
        return [Interval(0.0, total)]

    voiced: list[Interval] = []
    cursor = 0.0
    for gap in silences:
        if gap.start_ms > cursor:
            voiced.append(Interval(cursor, gap.start_ms))
        cursor = max(cursor, gap.end_ms)
    if cursor < total:
        voiced.append(Interval(cursor, total))

    return [v for v in voiced if v.duration_ms >= min_utterance_ms]


def loudness(path: Path) -> dict[str, float]:
    """EBU R128 integrated loudness, LRA and true peak.

    Uses `loudnorm` in analysis mode with JSON output rather than parsing the
    `ebur128` summary block, because the JSON is stable across ffmpeg releases.
    """
    out = _run([
        _require("ffmpeg"), "-nostdin", "-hide_banner", "-i", str(path),
        "-af", "loudnorm=print_format=json",
        "-f", "null", "-",
    ])

    match = _LOUDNORM_JSON.search(out)
    if not match:
        raise ProbeError(f"loudnorm produced no JSON for {path.name}")

    try:
        raw = json.loads(match.group(0))
        return {
            "integrated_lufs": float(raw["input_i"]),
            "true_peak_dbtp": float(raw["input_tp"]),
            "lra_lu": float(raw["input_lra"]),
            "threshold_lufs": float(raw["input_thresh"]),
        }
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise ProbeError(f"malformed loudnorm JSON for {path.name}: {exc}") from exc
