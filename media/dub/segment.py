"""One dubbed line, and the two numbers that decide its fate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Gemini TTS returns raw little-endian signed 16-bit PCM at 24 kHz mono.
TTS_RATE = 24_000


def pcm_duration_ms(pcm: bytes, rate: int = TTS_RATE) -> float:
    """2 bytes per mono sample."""
    return len(pcm) / 2 / rate * 1000.0


@dataclass
class DubSegment:
    """A synthesised line, with everything needed to judge and repair it."""

    index: int
    pcm: bytes
    source_text: str = ""           # the original English line
    target_text: str = ""           # what was actually spoken
    reference_ms: float = 0.0       # the slot in the picture it must fit
    reference_start_ms: float = 0.0
    attempts: int = 1               # how many adaptations it took to fit
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return pcm_duration_ms(self.pcm)

    @property
    def overrun_ms(self) -> float:
        """How far past its slot this line runs. Negative means it fits.

        A property of the audio rather than an opinion about it -- which is
        why an adaptation that claims to be shorter can still be caught.
        """
        return self.duration_ms - self.reference_ms

    @property
    def fits(self) -> bool:
        return self.overrun_ms <= 0

    @property
    def expansion(self) -> float:
        """Dubbed length over source slot -- the classic localisation problem.
        German against English typically lands near 1.2."""
        return self.duration_ms / self.reference_ms if self.reference_ms else 0.0
