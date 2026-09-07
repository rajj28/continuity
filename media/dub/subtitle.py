"""Subtitles, authored from the lines that were actually spoken.

The localised subtitle is not a separate translation. It is the dubbing script
laid back onto the picture, which is how a real localisation package is built:
one adapted text, two deliverables. That has a consequence worth being explicit
about -- the subtitle's parent is the adapted line, so re-adapting a line for
length invalidates the subtitle that quoted it, automatically, through the same
hash comparison that governs everything else here. Nobody has to remember to
regenerate it.

It also means the subtitle inherits the dub's problem. A line that had to grow
to carry the meaning produces a cue that has to be read faster, and Japan's
11 characters per second is a far tighter bar than Germany's 20. So the same
adaptation can pass as audio and fail as text, in one market and not another,
which is exactly the kind of cross-dimension interaction a release system
exists to catch and a dubbing tool never sees.

Line breaking follows the delivery specs' usual shape: at most two lines,
balanced, broken at a space rather than mid-word. It is deliberately simple --
a real conformer breaks on syntactic boundaries -- and the probe measures the
result rather than trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    lines: list[str]


def _timestamp(ms: int) -> str:
    ms = max(0, int(round(ms)))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def break_lines(text: str, max_chars: int, max_lines: int = 2) -> list[str]:
    """Split a line for reading, balanced across at most `max_lines`.

    Balanced rather than greedy: a 30-character cue reads better as 15/15 than
    as 24/6, and the delivery specs that bother to say so all say the same
    thing. Words longer than the limit are left intact -- breaking inside a
    word is worse than a long line, and the probe will report the length
    honestly either way.
    """
    words = text.split()
    if not words:
        return [""]
    if len(text) <= max_chars:
        return [text]

    target = len(text) / min(max_lines, 2)
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > target and len(lines) < max_lines - 1:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines[:max_lines] if lines else [text]


def build_cues(
    lines: list[dict[str, Any]], *, max_line_chars: int, max_lines: int = 2,
    min_gap_ms: int = 0,
) -> list[SubtitleCue]:
    """Cues from adapted lines and their reference timings.

    Timed to the ORIGINAL cue, not to the synthesised audio. A subtitle
    belongs to the picture: it appears when the character speaks on screen,
    and if the dub's audio drifts, that is the dub's fault to fix rather than
    a reason to move the text away from the shot it belongs to.
    """
    cues: list[SubtitleCue] = []
    for position, line in enumerate(sorted(lines, key=lambda x: x["start_ms"]), 1):
        start = int(line["start_ms"])
        end = int(line["end_ms"])
        if cues and min_gap_ms and start - cues[-1].end_ms < min_gap_ms:
            # Pull the previous cue out early rather than pushing this one
            # late: the incoming cue is tied to a shot, the outgoing one is
            # already on screen and has been read.
            cues[-1].end_ms = max(cues[-1].start_ms + 1, start - min_gap_ms)
        cues.append(SubtitleCue(
            index=position, start_ms=start, end_ms=end,
            lines=break_lines(str(line["text"]).strip(), max_line_chars, max_lines),
        ))
    return cues


def write_srt(cues: list[SubtitleCue], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        f"{cue.index}\n"
        f"{_timestamp(cue.start_ms)} --> {_timestamp(cue.end_ms)}\n"
        + "\n".join(cue.lines)
        for cue in cues
    ]
    # Trailing newline: parsers that split on a blank line need the last block
    # terminated like every other one.
    dest.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return dest
