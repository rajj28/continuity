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


# ---- PCM interchange ------------------------------------------------------
# The Gemini Live API speaks raw little-endian signed 16-bit PCM: 16 kHz mono
# in, 24 kHz mono out. Nothing negotiates that, so the conversion lives here
# next to the other ffmpeg calls rather than being inlined at the call site.

LIVE_INPUT_RATE = 16_000
LIVE_OUTPUT_RATE = 24_000


def pcm_s16le(
    path: Path,
    *,
    rate: int = LIVE_INPUT_RATE,
    start_ms: float | None = None,
    duration_ms_: float | None = None,
) -> bytes:
    """Decode (a span of) an audio file to raw mono s16le PCM.

    `-ss` before `-i` seeks the input, which for a WAV is sample-accurate --
    the concern that forced re-encoding when cutting scene video does not
    apply to uncompressed audio.
    """
    args = [_require("ffmpeg"), "-v", "error"]
    if start_ms is not None:
        args += ["-ss", f"{start_ms / 1000:.6f}"]
    args += ["-i", str(path)]
    if duration_ms_ is not None:
        args += ["-t", f"{duration_ms_ / 1000:.6f}"]
    args += ["-f", "s16le", "-acodec", "pcm_s16le",
             "-ac", "1", "-ar", str(rate), "-"]
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ProbeError(f"could not decode PCM from {path.name}: {detail[:300]}")
    return proc.stdout


def pcm_duration_ms(pcm: bytes, rate: int) -> float:
    """Exact duration of a raw PCM buffer. 2 bytes per mono sample."""
    return len(pcm) / 2 / rate * 1000.0


def write_wav(pcm: bytes, dest: Path, *, rate: int) -> Path:
    """Wrap raw PCM in a WAV container so the QC probes can read it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [_require("ffmpeg"), "-y", "-v", "error",
         "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
         str(dest)],
        input=pcm, capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ProbeError(f"could not write {dest.name}: {detail[:300]}")
    return dest
