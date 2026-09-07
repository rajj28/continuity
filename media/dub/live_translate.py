"""Dubbing with Gemini Live Translate.

`gemini-3.5-live-translate-preview` is an audio-to-audio model: speech in one
language goes in, speech in another comes out. That is dubbing, and it is on
the Gemini API free tier, which matters here because every Google Cloud billing
account available to this project is closed (see docs/LIMITATIONS.md). The
alternative was Chirp 3 HD, which is better but needs a card.

## One session per utterance, with VAD switched off

The obvious approach -- stream a whole scene through one session -- is wrong
for this problem. Two reasons, and both are about knowing which sound belongs
to which line:

  - The dialogue list already states exactly where every line starts and ends.
    Letting the model's voice-activity detector rediscover those boundaries on
    a mixed stem with an orchestral score under it re-introduces an error we
    already have the answer to. So `automatic_activity_detection` is disabled
    and turns are marked with explicit `activity_start` / `activity_end`.
  - Lineage. Each returned segment has exactly one parent utterance, named and
    hashed. That is what lets the agent repair one line instead of a scene,
    and what lets a blast radius stop at the lines that actually changed.

The cost is a WebSocket per line. For a scene of a dozen cues that is fine, and
the determinism is worth more than the handshakes.

## The drift is real, and that is the point

Live Translate produces natural speech at its own pace. It does not know the
line has to land inside a 1.8-second hole in the picture, and it will often
overrun. That mismatch is a genuine dubbing failure of exactly the kind this
whole system exists to catch -- arrived at honestly, by running a real model on
real audio, rather than by injecting an offset and calling it a demo.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from media.qc.ffmpeg import (
    LIVE_INPUT_RATE,
    LIVE_OUTPUT_RATE,
    pcm_duration_ms,
    pcm_s16le,
)
from media.qc.types import ProbeError

MODEL = "gemini-3.5-live-translate-preview"

# The API's own framing. 100 ms at 16 kHz mono s16le.
CHUNK_MS = 100
CHUNK_BYTES = int(LIVE_INPUT_RATE * (CHUNK_MS / 1000) * 2)

# Market -> BCP-47 code the model expects. Kept here rather than derived from
# the market id, because they are not the same namespace: a market is a
# delivery territory and may one day want a language its code does not name.
TARGET_LANGUAGE = {
    "fr-FR": "fr",
    "de-DE": "de",
    "pt-BR": "pt",
    "hi-IN": "hi",
    "ja-JP": "ja",
}


class DubbingError(ProbeError):
    """Raised when synthesis fails. Subclasses ProbeError so the pipeline's
    existing failure handling treats a dead model like a dead probe: the
    measurement is absent, and absent blocks the market."""


@dataclass
class DubSegment:
    """One translated line, with everything needed to judge it later."""

    index: int
    pcm: bytes                      # 24 kHz mono s16le
    source_text: str = ""           # what the model heard
    target_text: str = ""           # what it said
    reference_ms: float = 0.0       # the slot in the picture it must fit
    reference_start_ms: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return pcm_duration_ms(self.pcm, LIVE_OUTPUT_RATE)

    @property
    def overrun_ms(self) -> float:
        """How far past its slot this line runs. Negative means it fits.

        This is the number the whole demo turns on, and it is a property of
        the audio rather than an opinion about it.
        """
        return self.duration_ms - self.reference_ms

    @property
    def expansion(self) -> float:
        """Ratio of dubbed length to source length -- the classic localisation
        problem. German against English typically lands near 1.2."""
        return self.duration_ms / self.reference_ms if self.reference_ms else 0.0


def _client(api_key: str | None = None):
    key = api_key or os.environ.get("GEMINI_API_KEY") or ""
    if not key:
        raise DubbingError(
            "no GEMINI_API_KEY. Create one at https://aistudio.google.com/apikey "
            "-- the Gemini API free tier needs no Cloud Billing -- and put it in "
            ".env.local as GEMINI_API_KEY=..."
        )
    from google import genai  # imported lazily so the QC layer needs no SDK

    return genai.Client(api_key=key)


def _config(target_language: str):
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        # Both transcriptions are requested because they are free evidence:
        # the input one tells us what the model actually heard (a check on our
        # own segmentation), and the output one gives the adapted text a later
        # REWRITE strategy needs to work from.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code=target_language,
            echo_target_language=True,
        ),
        realtime_input_config=types.RealtimeInputConfig(
            # We know where the lines are. See the module docstring.
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True
            ),
        ),
    )


async def _translate_one(
    client: Any, pcm: bytes, target_language: str, *, timeout_s: float
) -> tuple[bytes, str, str]:
    from google.genai import types

    audio_out = bytearray()
    heard: list[str] = []
    said: list[str] = []

    async with client.aio.live.connect(
        model=MODEL, config=_config(target_language)
    ) as session:
        async def feed() -> None:
            await session.send_realtime_input(activity_start=types.ActivityStart())
            for i in range(0, len(pcm), CHUNK_BYTES):
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=pcm[i:i + CHUNK_BYTES],
                        mime_type=f"audio/pcm;rate={LIVE_INPUT_RATE}",
                    )
                )
            await session.send_realtime_input(activity_end=types.ActivityEnd())
            # Tells the server no more audio is coming for this turn, so it
            # finalises rather than waiting on a stream that will never end.
            await session.send_realtime_input(audio_stream_end=True)

        feeder = asyncio.create_task(feed())
        try:
            async with asyncio.timeout(timeout_s):
                async for response in session.receive():
                    content = getattr(response, "server_content", None)
                    if content is None:
                        continue
                    if getattr(content, "input_transcription", None):
                        heard.append(content.input_transcription.text or "")
                    if getattr(content, "output_transcription", None):
                        said.append(content.output_transcription.text or "")
                    turn = getattr(content, "model_turn", None)
                    if turn:
                        for part in turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                audio_out += part.inline_data.data
                    if getattr(content, "turn_complete", False):
                        break
        except TimeoutError as exc:
            raise DubbingError(
                f"Live Translate produced no complete turn within {timeout_s:.0f}s"
            ) from exc
        finally:
            feeder.cancel()

    if not audio_out:
        raise DubbingError(
            "Live Translate returned no audio. Usually the turn was never "
            "closed, or the input was silent."
        )
    return bytes(audio_out), "".join(heard).strip(), "".join(said).strip()


def translate_utterances(
    source: Path,
    utterances: Iterable[tuple[float, float]],
    *,
    market: str,
    api_key: str | None = None,
    timeout_s: float = 120.0,
) -> list[DubSegment]:
    """Dub each (start_ms, end_ms) span of `source` into the market's language.

    Returns one segment per utterance, in order. A failure on any single line
    aborts rather than returning a partial stem: a stem missing a line would
    measure as a sync problem and send the agent chasing the wrong cause.
    """
    if market not in TARGET_LANGUAGE:
        raise DubbingError(
            f"no target language for market {market!r}; "
            f"known: {sorted(TARGET_LANGUAGE)}"
        )
    language = TARGET_LANGUAGE[market]
    client = _client(api_key)

    segments: list[DubSegment] = []
    for index, (start_ms, end_ms) in enumerate(utterances):
        slot_ms = end_ms - start_ms
        pcm_in = pcm_s16le(
            source, rate=LIVE_INPUT_RATE, start_ms=start_ms, duration_ms_=slot_ms
        )
        pcm_out, heard, said = asyncio.run(
            _translate_one(client, pcm_in, language, timeout_s=timeout_s)
        )
        segments.append(DubSegment(
            index=index,
            pcm=pcm_out,
            source_text=heard,
            target_text=said,
            reference_ms=slot_ms,
            reference_start_ms=start_ms,
            detail={"model": MODEL, "target_language": language},
        ))
    return segments
