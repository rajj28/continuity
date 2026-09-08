"""Speech synthesis with Gemini TTS, and the loop that makes a line fit.

`synthesise` is a thin call. `dub_line` is the interesting part: it is the
place where "models propose, measurements dispose" stops being a slogan and
becomes control flow.

A translated line is synthesised and then TIMED. If it overruns its slot, the
measured duration -- not the model's opinion of its own brevity -- is fed back
as the constraint for the next attempt, and the loop runs again. It stops on a
measured fit, or after a fixed number of attempts, and it reports which. A line
that could not be made to fit is returned overrunning rather than silently
accepted, because an overrun that reaches the stem is a fact the QC probe will
find anyway, and hiding it here would only make the discovery later and
stranger.

Voices are Google's prebuilt set. No voice is cloned from a Sintel performer
and no Instant Custom Voice model is trained; see assets/SOURCES.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from media.dub.cache import Cache
from media.dub.quota import call as quota_call
from media.dub.segment import TTS_RATE, DubSegment, pcm_duration_ms
from media.dub.translate import AdaptedLine, TranslationError, translate
from media.qc.types import ProbeError

log = logging.getLogger("continuity.tts")

# Resolved per backend. On Vertex this is gemini-2.5-flash-tts; on the
# consumer API it is gemini-3.1-flash-tts-preview, whose per-day quota was the
# constraint the whole pipeline was designed around before billing opened.
from media.model import model_for


def _model() -> str:
    return model_for("speech")

# One prebuilt voice per market, fixed so a re-run of the demo sounds the same
# and so a repair does not change the performer mid-scene -- which would be a
# far more jarring continuity error than the timing problem being fixed.
VOICE = {
    "fr-FR": "Aoede",
    "de-DE": "Kore",
    "pt-BR": "Leda",
    "hi-IN": "Callirrhoe",
    "ja-JP": "Autonoe",
}

# How many attempts a normal BUILD gets: one. A first-pass dub is a faithful
# translation recorded to picture, and whether it fits is what QC is for. This
# is not a crippled default chosen to manufacture a demo failure -- it is what
# a first pass is, and the overrun that follows is the real phenomenon of
# German running about 1.2x against English.
#
# Raising it IS the REWRITE repair strategy. Same code, different budget, which
# is also how a studio would express it: a first pass and then an adaptation
# pass with a length constraint. REWRITE_ATTEMPTS stops at three because by the
# third failure the slot is genuinely too short for the meaning, and that is a
# human's call rather than a tightening loop's.
BUILD_ATTEMPTS = 1
REWRITE_ATTEMPTS = 3


# Some lines read as instructions to the TTS model, which then answers
# "Model tried to generate text, but it should only be used for TTS" rather
# than speaking them. Imperatives are the common case -- a line like "Tell me
# where you came from" is indistinguishable from a prompt.
#
# The documented remedy is the style-prompt form, which names the task
# explicitly. It is used as a FALLBACK rather than always, because wrapping
# every line in an instruction risks the model performing the instruction's
# register instead of the line's, and prosody is the thing being measured.
_SPEAK_AS_TEXT = (
    "Read this line aloud in a natural speaking voice, "
    "saying only the words after the colon: "
)
_MISREAD_AS_PROMPT = "should only be used for TTS"


class SynthesisError(ProbeError):
    pass


def synthesise(
    client: Any, text: str, *, voice: str, model: str = "",
    cache: Cache | None = None,
) -> bytes:
    """Text to raw 24 kHz mono s16le PCM."""
    model = model or _model()
    if cache is not None and (hit := cache.get_bytes(model, voice, text)) is not None:
        return hit
    pcm = _synthesise_once(client, text, voice=voice, model=model)
    if cache is not None:
        cache.put_bytes(pcm, model, voice, text)
    return pcm


def _synthesise_once(
    client: Any, text: str, *, voice: str, model: str = ""
) -> bytes:
    model = model or _model()
    from google.genai import types

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
    )
    def speak(payload: str):
        # Through the budget: the free tier allows 3 TTS calls a minute, so a
        # twelve-line scene is a four-minute job and the spacing is scheduled
        # rather than discovered by crashing into it.
        return quota_call(
            model,
            lambda: client.models.generate_content(
                model=model, contents=payload, config=config
            ),
            label=f"tts({voice})",
        )

    try:
        response = speak(text)
    except Exception as exc:
        if _MISREAD_AS_PROMPT not in str(exc):
            raise SynthesisError(f"TTS call failed: {exc}") from exc
        log.info("  line read as a prompt; re-sending with an explicit "
                 "speak instruction")
        try:
            response = speak(_SPEAK_AS_TEXT + text)
        except Exception as retry_exc:
            raise SynthesisError(
                f"TTS refused the line both bare and as an explicit "
                f"instruction: {retry_exc}"
            ) from retry_exc

    try:
        part = response.candidates[0].content.parts[0]
        data = part.inline_data.data
    except (AttributeError, IndexError, TypeError) as exc:
        raise SynthesisError(f"TTS returned no audio: {exc}") from exc
    if not data:
        raise SynthesisError("TTS returned an empty audio buffer")
    return data


def dub_line(
    client: Any,
    source_text: str,
    *,
    market: str,
    slot_ms: float,
    index: int = 0,
    reference_start_ms: float = 0.0,
    max_attempts: int = BUILD_ATTEMPTS,
    voice: str | None = None,
    cache: Cache | None = None,
    lines: Cache | None = None,
) -> DubSegment:
    """Translate, speak, measure, and shorten until it fits or we run out.

    The loop condition is the MEASURED duration of synthesised audio. The model
    is never asked whether its line is short enough, because a model asked that
    question answers yes.
    """
    if market not in VOICE:
        raise SynthesisError(
            f"no voice configured for market {market!r}; known: {sorted(VOICE)}"
        )
    chosen = voice or VOICE[market]

    attempts: list[dict[str, Any]] = []
    line: AdaptedLine | None = None
    pcm = b""
    measured = 0.0
    over: float | None = None

    for attempt in range(1, max_attempts + 1):
        line = translate(
            client, source_text, market=market, slot_ms=slot_ms,
            shorter_than=over, cache=lines,
        )
        pcm = synthesise(client, line.text, voice=chosen, cache=cache)
        measured = pcm_duration_ms(pcm, TTS_RATE)
        attempts.append({
            "attempt": attempt,
            "text": line.text,
            "measured_ms": round(measured, 1),
            "overrun_ms": round(measured - slot_ms, 1),
            "syllables_claimed": line.syllables_claimed,
            "note": line.note,
        })
        log.info(
            "  line %02d attempt %d: %.0f ms in a %.0f ms slot (%+.0f)  %s",
            index, attempt, measured, slot_ms, measured - slot_ms, line.text[:44],
        )
        if measured <= slot_ms:
            break
        # Feed back the fact, not a complaint.
        over = measured

    assert line is not None
    return DubSegment(
        index=index,
        pcm=pcm,
        source_text=source_text,
        target_text=line.text,
        reference_ms=slot_ms,
        reference_start_ms=reference_start_ms,
        attempts=len(attempts),
        detail={
            "model": _model(),
            "translation_model": line.detail.get("model", ""),
            "voice": chosen,
            "market": market,
            "attempts": attempts,
            # Recorded plainly. A line that never fitted is a real outcome and
            # the stem should carry the evidence of it.
            "fitted": measured <= slot_ms,
        },
    )


def dub_scene(
    client: Any,
    utterances: list[dict[str, Any]],
    *,
    market: str,
    origin_ms: float,
    max_attempts: int = BUILD_ATTEMPTS,
    cache: Cache | None = None,
    lines: Cache | None = None,
) -> list[DubSegment]:
    """Every line of a scene, in order.

    `origin_ms` is the scene's in-point, subtracted so placements are relative
    to the scene cut rather than to the film.
    """
    segments: list[DubSegment] = []
    for utterance in utterances:
        slot_ms = float(utterance["end_ms"] - utterance["start_ms"])
        segments.append(dub_line(
            client, utterance["text"], market=market, slot_ms=slot_ms,
            index=int(utterance["index"]),
            reference_start_ms=float(utterance["start_ms"]) - origin_ms,
            max_attempts=max_attempts, cache=cache, lines=lines,
        ))
    return segments


def audio_cache(root: Path) -> Cache:
    """Synthesised speech, one file per (model, voice, text)."""
    return Cache(Path(root) / "tts", suffix=".pcm")
