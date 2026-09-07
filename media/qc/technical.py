"""Technical delivery conformance -- the checks that reject a package at the door.

A release is not blocked only by how the dub sounds. Every platform and every
territory publishes a technical delivery specification, and a file that misses
it is rejected before anyone watches a frame: wrong resolution, wrong frame
rate, wrong pixel format, wrong audio channel count, wrong sample rate. These
are the most common real-world delivery failures by a wide margin, and none of
them has anything to do with localisation.

Everything here comes from ffprobe reading the actual file. There is no
heuristic and no model: a stream either is 1280x544 at 24 fps in yuv420p or it
is not, and the answer is the same for everyone who runs the command.

Market-level rather than scene-level, deliberately. A scene inherits its
technical characteristics from the master it was cut from, so measuring every
scene separately would multiply series without adding information -- and would
let one correctly-cut scene mask a master that was never conformant.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .ffmpeg import _require
from .types import Measurement, ProbeError

METHOD = "ffprobe_streams_v1"


def probe_streams(path: Path) -> dict[str, Any]:
    """Raw stream metadata. One ffprobe call, parsed as JSON."""
    proc = subprocess.run(
        [_require("ffprobe"), "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {proc.stderr[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"unparseable ffprobe output for {path.name}: {exc}") from exc


def _stream(data: dict[str, Any], kind: str) -> dict[str, Any]:
    for stream in data.get("streams", []):
        if stream.get("codec_type") == kind:
            return stream
    raise ProbeError(f"no {kind} stream")


def _fps(stream: dict[str, Any]) -> float:
    """Frame rate from the rational ffprobe reports.

    `r_frame_rate` rather than `avg_frame_rate`: the average is computed over
    the file and drifts on a short cut with a partial final frame, which would
    make a correctly-conformed scene read as 23.98 fps when the master is 24.
    """
    raw = stream.get("r_frame_rate") or "0/0"
    try:
        num, _, den = raw.partition("/")
        return round(float(num) / float(den), 3) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def measure_technical(path: Path) -> list[Measurement]:
    """Everything a delivery spec checks, from one ffprobe call.

    Categorical facts -- codec name, pixel format -- are carried in `detail`
    rather than as values, because a metric is a number and "h264" is not one.
    They are judged by the conformance check below, which compares strings
    where strings are what matter.
    """
    data = probe_streams(path)
    video = _stream(data, "video")
    audio = _stream(data, "audio")

    return [
        Measurement(
            key="technical.video_height",
            value=float(video.get("height") or 0),
            unit="px", method=METHOD,
            detail={
                "width": video.get("width"),
                "codec": video.get("codec_name", ""),
                "pixel_format": video.get("pix_fmt", ""),
                "profile": video.get("profile", ""),
            },
        ),
        Measurement(
            key="technical.frame_rate",
            value=_fps(video),
            unit="fps", method=METHOD,
            detail={"raw": video.get("r_frame_rate", "")},
        ),
        Measurement(
            key="technical.audio_channels",
            value=float(audio.get("channels") or 0),
            unit="ch", method=METHOD,
            detail={
                "layout": audio.get("channel_layout", ""),
                "codec": audio.get("codec_name", ""),
            },
        ),
        Measurement(
            key="technical.audio_sample_rate",
            value=float(audio.get("sample_rate") or 0),
            unit="Hz", method=METHOD,
            detail={},
        ),
    ]


# Categorical requirements a spec states as names rather than numbers. Compared
# case-insensitively because ffprobe and delivery specs disagree on casing far
# more often than they disagree on substance.
CATEGORICAL = {
    "technical.video_height": ("video_codec", "codec"),
    "technical.audio_channels": ("audio_codec", "codec"),
}


def conformance(
    measurements: list[Measurement], spec: dict[str, Any]
) -> list[tuple[str, bool, str]]:
    """Judge measurements against a market's technical spec.

    Returns (requirement, met, detail) rather than raising, because a
    non-conformant file is a normal finding the verdict should carry -- not an
    error the pipeline should die on. A spec that omits a requirement simply
    does not produce a result for it, and the coverage gate is what notices.
    """
    by_key = {m.key: m for m in measurements}
    results: list[tuple[str, bool, str]] = []

    def numeric(key: str, spec_key: str, requirement: str) -> None:
        if spec_key not in spec or key not in by_key:
            return
        want = float(spec[spec_key])
        got = by_key[key].value
        results.append((requirement, got == want, f"{got:g} vs required {want:g}"))

    numeric("technical.video_height", "video_height", "video_height")
    numeric("technical.frame_rate", "frame_rate", "frame_rate")
    numeric("technical.audio_channels", "audio_channels", "audio_channels")
    numeric("technical.audio_sample_rate", "audio_sample_rate", "audio_sample_rate")

    if "video_codec" in spec and "technical.video_height" in by_key:
        got = str(by_key["technical.video_height"].detail.get("codec", ""))
        want = str(spec["video_codec"])
        results.append((
            "video_codec", got.lower() == want.lower(), f"{got!r} vs required {want!r}",
        ))
    if "pixel_format" in spec and "technical.video_height" in by_key:
        got = str(by_key["technical.video_height"].detail.get("pixel_format", ""))
        want = str(spec["pixel_format"])
        results.append((
            "pixel_format", got.lower() == want.lower(),
            f"{got!r} vs required {want!r}",
        ))
    return results
