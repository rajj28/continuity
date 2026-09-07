"""Translation and isochronous adaptation, with Gemini.

This is the first place in the system where a model is the right tool. Every
decision up to here -- what failed, against which bar, whether drift is
systematic, which strategy could work -- was arithmetic over measurements, and
using a model for any of it would have made the answer fluent instead of true.

Saying the same thing in fewer syllables is not arithmetic. It needs judgement
about which words carry the meaning, which can be dropped, whether a shorter
synonym changes the register, and whether a line can lose a clause without
losing the beat. No probe can do that. So the model does it here, and only
here, under two constraints that keep it honest:

**It proposes; the probe disposes.** The model is asked for a shorter line and
returns one it believes is shorter. Belief is not measurement: the line is then
synthesised and the resulting audio is timed. `adapt_to_fit` loops on the
measured duration, not on the model's opinion of it, and gives up after a fixed
number of attempts rather than arguing.

**It never sees the verdict.** The model is told the slot length and the source
line. It is not told whether the market is blocked, what the threshold is, or
that a repair is in progress -- because a model that knows it is being judged
on whether the line fits will tell you the line fits.

The prompt asks for JSON with a syllable count the model is not trusted on. It
is kept because a wildly wrong self-estimate is a useful signal that the model
misread the task, and it costs nothing to record.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from media.dub.cache import Cache
from media.dub.quota import call as quota_call
from media.qc.types import ProbeError

MODEL = "gemini-2.5-flash"

# Market -> the language the line is spoken in, and how it is named to the
# model. Kept separate from the market id because a delivery territory and a
# language are not the same namespace.
LANGUAGE = {
    "fr-FR": ("fr", "French (France)"),
    "de-DE": ("de", "German (Germany)"),
    "pt-BR": ("pt", "Portuguese (Brazil)"),
    "hi-IN": ("hi", "Hindi (India)"),
    "ja-JP": ("ja", "Japanese"),
}


class TranslationError(ProbeError):
    """Subclasses ProbeError so a dead model is handled like a dead probe:
    the measurement is absent, and absent blocks the market."""


@dataclass
class AdaptedLine:
    text: str
    syllables_claimed: int | None = None
    note: str = ""
    detail: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise TranslationError("the model returned an empty line")
        if self.detail is None:
            self.detail = {}


_SYSTEM = """You are a dubbing script adaptor for feature film localisation.

You translate a line of dialogue so it can be spoken over the original picture.
Dubbing is not subtitling: the line must be sayable at a natural pace inside
the time the actor's mouth is moving, and it must sound like speech, not like
a translation.

Rules you always follow:
- Preserve what the line MEANS and the register it is said in. A threat stays
  a threat; a joke stays a joke.
- Never add information the source line does not carry.
- Return only the spoken words. No stage directions, no quotation marks, no
  speaker names, no transliteration.
- Contractions and elision are welcome. Written-register formality is not.

Answer with JSON only:
{"text": "<the line>", "syllables": <integer>, "note": "<= 12 words on what you changed>"}"""


def _prompt(source: str, language: str, slot_ms: float,
            shorter_than: float | None) -> str:
    lines = [
        f"Target language: {language}",
        f"The line must be spoken inside {slot_ms:.0f} ms of picture.",
        "",
        f"Source line: {source}",
    ]
    if shorter_than is not None:
        # The previous attempt's MEASURED duration, not the model's estimate.
        # Telling it how far over it actually ran is the only feedback that
        # has ever been checked against reality.
        lines += [
            "",
            f"A previous attempt was synthesised and measured at "
            f"{shorter_than:.0f} ms, which is too long. Say the same thing "
            f"more briefly. Drop qualifiers and connectives before you drop "
            f"meaning; if something must go, lose the least load-bearing part "
            f"of the line.",
        ]
    return "\n".join(lines)


def _parse(raw: str) -> AdaptedLine:
    """Pull the JSON object out of a model response.

    Fenced or prefixed output is common enough that failing on it would be
    fragile, but a response with no object at all is a real failure and is
    raised rather than salvaged -- guessing at what the model meant is how a
    mistranslation reaches an audience.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise TranslationError(f"no JSON object in model response: {raw[:200]}")
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise TranslationError(f"unparseable model response: {exc}") from exc
    if "text" not in obj:
        raise TranslationError(f"model response has no `text`: {raw[:200]}")
    syllables = obj.get("syllables")
    return AdaptedLine(
        text=str(obj["text"]).strip(),
        syllables_claimed=int(syllables) if isinstance(syllables, int) else None,
        note=str(obj.get("note", ""))[:200],
        detail={"model": MODEL},
    )


def translate(
    client: Any,
    source: str,
    *,
    market: str,
    slot_ms: float,
    shorter_than: float | None = None,
    temperature: float = 0.4,
    cache: Cache | None = None,
) -> AdaptedLine:
    """One line, adapted for one market's slot.

    `shorter_than` carries the MEASURED duration of a previous attempt. It is
    the only feedback the model gets, and it is a fact rather than a critique.
    """
    if market not in LANGUAGE:
        raise TranslationError(
            f"no language for market {market!r}; known: {sorted(LANGUAGE)}"
        )
    _, language = LANGUAGE[market]

    # The retry budget is part of the key: "translate this" and "translate this
    # more briefly than 3331 ms" are different requests with different right
    # answers, and collapsing them would serve the too-long line forever.
    ckey = (MODEL, market, source, f"{slot_ms:.0f}", f"{shorter_than or 0:.0f}")
    if cache is not None and (hit := cache.get_json(*ckey)) is not None:
        return AdaptedLine(**hit)

    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        temperature=temperature,
        response_mime_type="application/json",
    )
    prompt = _prompt(source, language, slot_ms, shorter_than)
    try:
        response = quota_call(
            MODEL,
            lambda: client.models.generate_content(
                model=MODEL, contents=prompt, config=config
            ),
            label=f"translate({market})",
        )
    except Exception as exc:  # the SDK raises a wide family of API errors
        raise TranslationError(f"translation call failed: {exc}") from exc

    if not response.text:
        raise TranslationError("translation returned no text")
    line = _parse(response.text)
    line.detail.update({
        "market": market,
        "slot_ms": round(slot_ms, 1),
        "retry_of_ms": shorter_than,
    })
    if cache is not None:
        cache.put_json({
            "text": line.text,
            "syllables_claimed": line.syllables_claimed,
            "note": line.note,
            "detail": line.detail,
        }, *ckey)
    return line


def text_cache(path: Any) -> Cache:
    """Adapted lines, one file per (model, market, source, slot, retry)."""
    from pathlib import Path as _Path
    return Cache(_Path(path) / "lines", suffix=".json")
