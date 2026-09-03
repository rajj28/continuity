"""Dialogue list -> scenes -> reference utterances.

A real localisation vendor does not receive a video and guess where the lines
are; they receive an as-broadcast dialogue list with timecodes. We take the
same input, which is why reference utterance boundaries here come from the
dialogue list rather than from voice-activity detection on the final mix.

That distinction matters: Sintel's mix carries a continuous orchestral score,
so VAD on the mix returns music boundaries, not speech boundaries. VAD is the
right tool for the *generated dub stem*, which is voice-only -- and that is
exactly where sync.py applies it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .qc.subtitles import Cue, parse_srt
from .qc.types import Interval

# A pause longer than this starts a new scene. Chosen from the material: within
# Sintel's shaman conversation the largest inter-line gap is ~3.9 s, while the
# gap to the next dramatic beat is ~51 s. 40 s sits cleanly between the two.
SCENE_GAP_MS = 40_000

_WORD = re.compile(r"[\w'’\-]+", re.UNICODE)


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


@dataclass(frozen=True)
class Utterance:
    index: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def words(self) -> int:
        return word_count(self.text)

    @property
    def interval(self) -> Interval:
        return Interval(float(self.start_ms), float(self.end_ms))


@dataclass
class Scene:
    id: str
    in_ms: int
    out_ms: int
    utterances: list[Utterance] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.out_ms - self.in_ms

    @property
    def words(self) -> int:
        return sum(u.words for u in self.utterances)

    @property
    def speech_ms(self) -> int:
        return sum(u.duration_ms for u in self.utterances)

    def reference_intervals(self, *, relative: bool = True) -> list[Interval]:
        """Utterance boundaries for sync measurement.

        `relative=True` rebases onto the extracted scene clip, which starts at
        `in_ms` in the master -- that is the frame of reference the dub stem
        will be measured in.
        """
        offset = self.in_ms if relative else 0
        return [
            Interval(float(u.start_ms - offset), float(u.end_ms - offset))
            for u in self.utterances
        ]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "in_ms": self.in_ms,
            "out_ms": self.out_ms,
            "duration_ms": self.duration_ms,
            "utterance_count": len(self.utterances),
            "words": self.words,
            "speech_ms": self.speech_ms,
            "utterances": [
                {
                    "index": u.index,
                    "start_ms": u.start_ms,
                    "end_ms": u.end_ms,
                    "duration_ms": u.duration_ms,
                    "words": u.words,
                    "text": u.text,
                }
                for u in self.utterances
            ],
        }


def load_dialogue(path: Path) -> list[Cue]:
    return parse_srt(path)


def segment_scenes(
    cues: list[Cue],
    *,
    gap_ms: int = SCENE_GAP_MS,
    handle_ms: int = 1_000,
    limit_ms: int | None = None,
) -> list[Scene]:
    """Group cues into scenes on dialogue gaps.

    `handle_ms` pads each end so the extracted clip does not clip the first
    consonant or the last breath -- standard practice when pulling selects.
    """
    if not cues:
        return []

    groups: list[list[Cue]] = [[cues[0]]]
    for prev, cue in zip(cues, cues[1:]):
        if cue.start_ms - prev.end_ms > gap_ms:
            groups.append([cue])
        else:
            groups[-1].append(cue)

    scenes: list[Scene] = []
    for n, group in enumerate(groups, start=1):
        in_ms = max(0, group[0].start_ms - handle_ms)
        out_ms = group[-1].end_ms + handle_ms
        if limit_ms is not None:
            out_ms = min(out_ms, limit_ms)
        scenes.append(
            Scene(
                id=f"S{n:02d}",
                in_ms=in_ms,
                out_ms=out_ms,
                utterances=[
                    Utterance(i, c.start_ms, c.end_ms, c.text)
                    for i, c in enumerate(group)
                ],
            )
        )
    return scenes
