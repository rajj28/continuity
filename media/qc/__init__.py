"""Deterministic media QC probes.

Every function in this package is a pure measurement over bytes on disk.
No model produces a number here -- models propose, measurements dispose.
"""

from .types import Check, Interval, Measurement, ProbeError
from .ffmpeg import duration_ms, loudness, silence_intervals, voiced_intervals
from .sync import measure_dub, speech_rate_wpm, sync_offset
from .subtitles import Cue, measure_subtitles, parse_srt
from .loudness import measure_loudness

__all__ = [
    "Check", "Interval", "Measurement", "ProbeError",
    "duration_ms", "loudness", "silence_intervals", "voiced_intervals",
    "measure_dub", "speech_rate_wpm", "sync_offset",
    "Cue", "measure_subtitles", "parse_srt",
    "measure_loudness",
]
