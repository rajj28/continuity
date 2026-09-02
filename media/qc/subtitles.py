"""Subtitle conformance probe.

Deterministic SRT parsing and linting against per-market delivery rules. The
constraints implemented here (reading rate, minimum duration, line count, line
length, inter-cue gap) are the ones every major streaming delivery spec shares;
the exact numbers live in MarketProfile, not in this file, because they differ
by territory and must be versioned alongside the release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .types import Measurement, ProbeError

METHOD = "srt_lint_v1"

_TIMECODE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


@dataclass(frozen=True)
class Cue:
    index: int
    start_ms: int
    end_ms: int
    lines: list[str]

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def char_count(self) -> int:
        """Characters that count toward reading rate.

        Whitespace between words is excluded, following the convention used by
        the major delivery specs -- a reader does not spend time on the space.
        """
        return len(self.text.replace(" ", ""))

    @property
    def cps(self) -> float:
        if self.duration_ms <= 0:
            return float("inf")
        return self.char_count / (self.duration_ms / 1000.0)


def _to_ms(h: str, m: str, s: str, ms: str) -> int:
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1000 + int(ms)


def parse_srt(path: Path) -> list[Cue]:
    """Parse an SRT file. Raises rather than skipping malformed blocks --
    a subtitle file we cannot fully parse is not a file we can certify."""
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    if not raw:
        raise ProbeError(f"{path.name} is empty")

    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", raw):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            raise ProbeError(f"{path.name}: malformed cue block: {block[:60]!r}")

        tc_line = next((ln for ln in lines if _TIMECODE.search(ln)), None)
        if tc_line is None:
            raise ProbeError(f"{path.name}: no timecode in block: {block[:60]!r}")

        m = _TIMECODE.search(tc_line)
        assert m is not None
        start, end = _to_ms(*m.groups()[:4]), _to_ms(*m.groups()[4:])
        if end <= start:
            raise ProbeError(f"{path.name}: non-positive duration at {tc_line!r}")

        idx_line = lines[0] if lines[0] != tc_line else ""
        index = int(idx_line) if idx_line.strip().isdigit() else len(cues) + 1
        text_lines = lines[lines.index(tc_line) + 1:]

        cues.append(Cue(index, start, end, text_lines))

    if not cues:
        raise ProbeError(f"{path.name}: no cues found")
    return cues


def measure_subtitles(path: Path) -> list[Measurement]:
    """Every subtitle measurement, reported as worst-case across the file."""
    cues = parse_srt(path)

    rates = [c.cps for c in cues]
    worst_rate_idx = rates.index(max(rates))
    gaps = [
        cues[i + 1].start_ms - cues[i].end_ms for i in range(len(cues) - 1)
    ]

    return [
        Measurement(
            key="delivery.subtitle_reading_rate_cps",
            value=round(max(rates), 2),
            unit="cps",
            method=METHOD,
            detail={
                "cue_count": len(cues),
                "worst_cue_index": cues[worst_rate_idx].index,
                "worst_cue_text": cues[worst_rate_idx].text[:80],
                "mean_cps": round(sum(rates) / len(rates), 2),
            },
        ),
        Measurement(
            key="delivery.subtitle_min_duration_ms",
            value=float(min(c.duration_ms for c in cues)),
            unit="ms",
            method=METHOD,
            detail={"cue_count": len(cues)},
        ),
        Measurement(
            key="delivery.subtitle_max_lines",
            value=float(max(len(c.lines) for c in cues)),
            unit="count",
            method=METHOD,
            detail={
                "over_two_lines": [
                    c.index for c in cues if len(c.lines) > 2
                ][:10]
            },
        ),
        Measurement(
            key="delivery.subtitle_max_line_chars",
            value=float(max((len(ln) for c in cues for ln in c.lines), default=0)),
            unit="count",
            method=METHOD,
            detail={},
        ),
        Measurement(
            key="delivery.subtitle_min_gap_ms",
            value=float(min(gaps)) if gaps else 0.0,
            unit="ms",
            method=METHOD,
            detail={"negative_gaps": sum(1 for g in gaps if g < 0)},
        ),
    ]
