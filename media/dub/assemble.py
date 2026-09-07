"""Laying dubbed lines onto a timeline.

This file decides what kind of defect the pipeline produces, so it is worth
being explicit rather than treating it as plumbing.

A first-pass translation is faithful, not short. German against English runs
about 1.2x, so a line written for a 1.8-second hole in the picture frequently
does not fit one. What that overrun *looks like* in the finished stem depends
entirely on the mux policy, and both real policies are implemented here:

  CUE         every line starts at its cue, exactly as written. Overruns bleed
              over the next line. Onsets are correct by construction, so an
              onset-drift probe sees nothing wrong -- the damage is audible
              overlap, not timing.

  SEQUENTIAL  no line may start before the previous one finishes. An overrun
              therefore pushes everything after it late, and the lateness
              accumulates down the scene.

SEQUENTIAL is the default because it is what a conform actually does when
overlap is not allowed, and because of what it does to the diagnosis. The
accumulated lateness is *progressive*, not uniform -- so `sync.py`'s
`_is_systematic()` correctly reports scattered drift, which means shifting the
stem cannot fix it. The agent has to work out that the symptom (lines landing
late) and the cause (lines being too long) are different things, and that the
repair is to shorten the text rather than move the audio.

That is a real diagnostic problem produced by real audio, not an injected
offset dressed up as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from media.dub.segment import TTS_RATE, pcm_duration_ms
from media.qc.ffmpeg import write_wav

from .segment import DubSegment

BYTES_PER_SAMPLE = 2


class Policy(str, Enum):
    CUE = "cue"
    SEQUENTIAL = "sequential"


@dataclass
class Placement:
    """Where a line ended up, against where it was supposed to be."""

    index: int
    start_ms: float           # where it actually starts in the stem
    reference_start_ms: float  # where the picture wants it
    duration_ms: float
    reference_ms: float

    @property
    def onset_drift_ms(self) -> float:
        """Late is positive. This is the symptom the sync probe measures."""
        return self.start_ms - self.reference_start_ms

    @property
    def overrun_ms(self) -> float:
        """How far the line runs past its slot. This is the cause."""
        return self.duration_ms - self.reference_ms


@dataclass
class Stem:
    pcm: bytes
    placements: list[Placement]
    policy: Policy
    rate: int = TTS_RATE

    @property
    def duration_ms(self) -> float:
        return pcm_duration_ms(self.pcm, self.rate)

    @property
    def worst_drift_ms(self) -> float:
        return max((abs(p.onset_drift_ms) for p in self.placements), default=0.0)

    @property
    def worst_overrun_ms(self) -> float:
        return max((p.overrun_ms for p in self.placements), default=0.0)

    @property
    def overrunning(self) -> list[Placement]:
        """The lines that do not fit. The agent's actual repair targets."""
        return [p for p in self.placements if p.overrun_ms > 0]

    def write(self, dest: Path) -> Path:
        return write_wav(self.pcm, dest, rate=self.rate)


def _ms_to_bytes(ms: float, rate: int) -> int:
    """Byte offset of a timestamp, snapped to a whole sample.

    Snapping matters: an odd byte offset would shift the sign bit into the low
    byte and turn the whole stem into noise, which is a spectacular way to fail
    a QC probe for a reason that has nothing to do with dubbing.
    """
    return int(round(ms / 1000 * rate)) * BYTES_PER_SAMPLE


def place(
    segments: list[DubSegment],
    *,
    policy: Policy = Policy.SEQUENTIAL,
    min_gap_ms: float = 0.0,
) -> list[Placement]:
    """Decide where each line starts. No audio is touched here.

    Separated from `assemble` so the timing decision can be unit-tested, and
    inspected by the agent, without synthesising anything.
    """
    placements: list[Placement] = []
    cursor = 0.0
    for segment in segments:
        if policy is Policy.CUE:
            start = segment.reference_start_ms
        else:
            start = max(segment.reference_start_ms, cursor)
        placements.append(Placement(
            index=segment.index,
            start_ms=start,
            reference_start_ms=segment.reference_start_ms,
            duration_ms=segment.duration_ms,
            reference_ms=segment.reference_ms,
        ))
        cursor = start + segment.duration_ms + min_gap_ms
    return placements


def assemble(
    segments: list[DubSegment],
    *,
    policy: Policy = Policy.SEQUENTIAL,
    min_gap_ms: float = 0.0,
    total_ms: float | None = None,
    rate: int = TTS_RATE,
) -> Stem:
    """Mix the lines onto one silent timeline.

    Overlapping samples are summed with saturation rather than averaged or
    clipped by wraparound. Averaging would quietly duck both lines and make an
    overlap sound like a mix decision; wraparound would produce a crack. A
    saturated sum is what a console does, so the artefact a listener hears is
    the artefact the probe measures.
    """
    placements = place(segments, policy=policy, min_gap_ms=min_gap_ms)
    end_ms = max(
        (p.start_ms + p.duration_ms for p in placements), default=0.0
    )
    if total_ms is not None:
        end_ms = max(end_ms, total_ms)

    timeline = bytearray(_ms_to_bytes(end_ms, rate))
    for segment, placement in zip(segments, placements):
        offset = _ms_to_bytes(placement.start_ms, rate)
        chunk = segment.pcm
        if offset + len(chunk) > len(timeline):
            timeline.extend(b"\x00" * (offset + len(chunk) - len(timeline)))
        window = timeline[offset:offset + len(chunk)]
        if any(window):
            timeline[offset:offset + len(chunk)] = _mix(bytes(window), chunk)
        else:
            timeline[offset:offset + len(chunk)] = chunk

    return Stem(pcm=bytes(timeline), placements=placements, policy=policy, rate=rate)


def _mix(a: bytes, b: bytes) -> bytes:
    """Sum two s16le buffers, saturating at the format's limits."""
    out = bytearray(len(a))
    for i in range(0, len(a) - 1, 2):
        left = int.from_bytes(a[i:i + 2], "little", signed=True)
        right = int.from_bytes(b[i:i + 2], "little", signed=True)
        total = max(-32768, min(32767, left + right))
        out[i:i + 2] = total.to_bytes(2, "little", signed=True)
    return bytes(out)
