"""Audio description: the deliverable the agent has to build rather than fix.

Audio description is spoken narration of what is visually essential, placed in
the silences between dialogue, for viewers who cannot see the picture. In the
EU it is not a nicety -- the European Accessibility Act makes it a condition of
release, which is why fr-FR and de-DE currently block on its absence.

It is also the only stage in this system where a model has to look at the
picture. Everything else measures audio or reads text. Describing what happens
on screen cannot be done by a probe, so Gemini watches the actual scene video,
and it is the one place where "multimodal" means something other than a claim.

## The constraint that makes it hard, and measurable

AD must fit in the gaps. Sintel S03 offers one gap of 5.3 seconds, three near a
second, and one of 350 ms that will hold nothing at all. At a narrator's ~160
words per minute a one-second gap is four words. So the interesting problem is
not "describe the scene", it is "describe what matters in the time the film
actually leaves you", and a description that overruns is not a stylistic
complaint -- it talks over the next line of dialogue.

That gives a hard, deterministic check: `ad_collision_ms`, how far the narration
extends past its gap into a dialogue slot. The model proposes a description; the
synthesiser makes it real; ffmpeg measures whether it fits. Same discipline as
the dub, applied to a different failure.

## What the model is and is not asked

It is given the video, the gap windows and the dialogue, and asked to describe
only what is visually essential and not already carried by the soundtrack. It is
not told the market is blocked, and it is not asked whether its description
fits -- a model asked that question answers yes. Fit is measured.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from media.dub.cache import Cache
from media.dub.quota import call as quota_call
from media.qc.types import Interval, Measurement, ProbeError

log = logging.getLogger("continuity.ad")

MODEL = "gemini-3.5-flash"
METHOD = "gemini_video_description_v1"

# Measured, not assumed. The textbook figure for AD narration is about 160
# words per minute, and budgeting at that rate produced "Sintel runzelt." --
# two words -- as 1920 ms of audio in a 1000 ms gap, a 920 ms collision.
#
# Gemini TTS reads narration at roughly 85 wpm once its ~290 ms of leading
# silence is counted, so the budget is set from what the synthesiser actually
# does rather than from what a human narrator does. This is still only a
# starting estimate: the loop in stage 4 re-writes shorter when the measured
# audio overruns, because a budget is a guess and the file is a fact.
WORDS_PER_MINUTE = 85.0

# Gaps shorter than this hold nothing worth saying. 700 ms is about two words
# at narration pace, and two words that talk over the next line are worse than
# silence.
MIN_USABLE_GAP_MS = 700.0


class DescriptionError(ProbeError):
    pass


@dataclass
class Gap:
    """A silence the narration can live in."""

    index: int
    start_ms: float
    end_ms: float

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

    @property
    def word_budget(self) -> int:
        """How many words actually fit. The number the model is held to."""
        return max(1, int(self.duration_ms / 1000.0 * WORDS_PER_MINUTE / 60.0))


@dataclass
class Description:
    gap: Gap
    text: str
    detail: dict[str, Any] = field(default_factory=dict)


def find_gaps(
    utterances: list[dict[str, Any]], *, origin_ms: float, scene_ms: float,
    min_gap_ms: float = MIN_USABLE_GAP_MS,
) -> list[Gap]:
    """Silences between dialogue, relative to the scene cut.

    Includes the head and tail of the scene: a description before the first
    line is often the most useful one, because it establishes where we are.
    """
    spans = sorted(
        (float(u["start_ms"] - origin_ms), float(u["end_ms"] - origin_ms))
        for u in utterances
    )
    gaps: list[Gap] = []
    cursor = 0.0
    for start, end in spans:
        if start - cursor >= min_gap_ms:
            gaps.append(Gap(len(gaps), cursor, start))
        cursor = max(cursor, end)
    if scene_ms - cursor >= min_gap_ms:
        gaps.append(Gap(len(gaps), cursor, scene_ms))
    return gaps


_SYSTEM = """You write audio description for film, to broadcast standards.

Audio description is narration for viewers who cannot see the picture. It is
spoken in the gaps between dialogue and must never overlap it.

Rules you always follow:
- Describe only what is VISUALLY essential and not already carried by the
  dialogue or the sound. If a viewer can hear it, do not say it.
- Present tense, third person, plain language.
- Describe what is visible, not what it means. "She lowers the blade", not
  "she gives up".
- Never name a character the audience has not been told the name of.
- Respect the word budget absolutely. It is derived from how long the silence
  actually is, and going over means talking over the next line.
- If a gap holds nothing worth describing, return an empty string for it. A
  silence is better than filler.

Answer with JSON only:
{"descriptions": [{"gap": <index>, "text": "<narration>", "words": <integer>}]}"""


def _prompt(gaps: list[Gap], utterances: list[dict[str, Any]],
            origin_ms: float) -> str:
    lines = ["The dialogue in this scene, with timings relative to its start:"]
    for u in utterances:
        lines.append(
            f"  {u['start_ms'] - origin_ms:.0f}-{u['end_ms'] - origin_ms:.0f} ms: "
            f"{u['text']}"
        )
    lines.append("")
    lines.append("Write description for these silences, and only these:")
    for gap in gaps:
        lines.append(
            f"  gap {gap.index}: {gap.start_ms:.0f}-{gap.end_ms:.0f} ms "
            f"({gap.duration_ms:.0f} ms available, at most {gap.word_budget} words)"
        )
    return "\n".join(lines)


def _parse(raw: str, gaps: list[Gap]) -> list[Description]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise DescriptionError(f"no JSON object in model response: {raw[:200]}")
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DescriptionError(f"unparseable description response: {exc}") from exc

    by_index = {g.index: g for g in gaps}
    out: list[Description] = []
    for item in obj.get("descriptions", []):
        index = item.get("gap")
        if index not in by_index:
            # A description for a gap we did not ask about cannot be placed
            # anywhere, and guessing where it belongs would put narration over
            # dialogue. Dropped, and said out loud.
            log.warning("description for unknown gap %r, dropped", index)
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue  # the model judged the gap not worth describing
        out.append(Description(
            gap=by_index[index], text=text,
            detail={"model": MODEL, "words_claimed": item.get("words")},
        ))
    return out


def describe_scene(
    client: Any, video: Path, utterances: list[dict[str, Any]],
    *, origin_ms: float, scene_ms: float, cache: Cache | None = None,
) -> list[Description]:
    """Watch the scene and write narration for its silences.

    The video goes inline rather than through the Files API: a scene cut is a
    few megabytes, and an inline part keeps the call to one request with no
    upload lifecycle to manage or clean up.
    """
    gaps = find_gaps(utterances, origin_ms=origin_ms, scene_ms=scene_ms)
    if not gaps:
        return []

    prompt = _prompt(gaps, utterances, origin_ms)
    ckey = (MODEL, video.name, str(video.stat().st_size), prompt)
    if cache is not None and (hit := cache.get_json(*ckey)) is not None:
        by_index = {g.index: g for g in gaps}
        return [
            Description(gap=by_index[d["gap"]], text=d["text"],
                        detail=d.get("detail", {}))
            for d in hit if d["gap"] in by_index
        ]

    from google.genai import types

    try:
        response = quota_call(
            MODEL,
            lambda: client.models.generate_content(
                model=MODEL,
                contents=[
                    types.Part.from_bytes(
                        data=video.read_bytes(), mime_type="video/mp4"
                    ),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    temperature=0.3,
                    response_mime_type="application/json",
                ),
            ),
            label=f"describe({video.name})",
        )
    except Exception as exc:
        raise DescriptionError(f"description call failed: {exc}") from exc

    if not response.text:
        raise DescriptionError("description returned no text")
    descriptions = _parse(response.text, gaps)

    if cache is not None:
        cache.put_json([
            {"gap": d.gap.index, "text": d.text, "detail": d.detail}
            for d in descriptions
        ], *ckey)
    return descriptions


# ---------------------------------------------------------------------------
# Localisation
# ---------------------------------------------------------------------------

# Narration is not dialogue, and reusing the dubbing adaptor's prompt for it
# produced empty responses -- that prompt insists on "only the spoken words" of
# a character, and "Sintel frowns." is not a line anyone says. Same machinery,
# different instruction, because the two tasks genuinely differ: dubbing
# preserves a performance, description reports what is on screen.
_LOCALISE_SYSTEM = """You localise audio description for film.

Audio description is narration for viewers who cannot see the picture. You are
translating the narrator's words, not a character's dialogue.

Rules you always follow:
- Present tense, third person, plain language, spoken register.
- Keep it at or under the word budget. It has to be read aloud inside a
  measured silence, and going over means talking over the next line.
- Do not add detail the English does not carry, and do not interpret. Translate
  what is described, not what it might mean.
- Keep proper names as they are.

Answer with JSON only:
{"text": "<the narration>", "words": <integer>}"""


def localise_description(
    client: Any, text: str, *, market: str, gap: Gap,
    cache: Cache | None = None, temperature: float = 0.3,
    over_by_ms: float | None = None,
) -> str:
    """The narration, in the market's language, inside the gap's word budget.

    `over_by_ms` carries how far a previous attempt actually overran once
    synthesised. It is a measurement rather than a critique, and it is the only
    feedback the model gets -- the same arrangement the dialogue adaptation
    uses, for the same reason.
    """
    from media.dub.translate import LANGUAGE, TranslationError

    if market not in LANGUAGE:
        raise DescriptionError(f"no language for market {market!r}")
    _, language = LANGUAGE[market]

    ckey = (MODEL, "ad_localise", market, text, str(gap.word_budget),
            f"{over_by_ms or 0:.0f}")
    if cache is not None and (hit := cache.get_json(*ckey)) is not None:
        return str(hit["text"])

    prompt = "\n".join([
        f"Target language: {language}",
        f"Word budget: at most {gap.word_budget} words "
        f"({gap.duration_ms:.0f} ms of silence).",
        "",
        f"English narration: {text}",
    ] + ([
        "",
        f"A previous attempt was synthesised and ran {over_by_ms:.0f} ms past "
        f"the silence, which means it talks over the next line. Say less. Drop "
        f"the least important visual detail rather than compressing all of them.",
    ] if over_by_ms and over_by_ms > 0 else []))

    from google.genai import types

    try:
        response = quota_call(
            MODEL,
            lambda: client.models.generate_content(
                model=MODEL, contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_LOCALISE_SYSTEM,
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            ),
            label=f"localise_ad({market})",
        )
    except Exception as exc:
        raise DescriptionError(f"description localisation failed: {exc}") from exc

    match = re.search(r"\{.*\}", response.text or "", re.DOTALL)
    if not match:
        raise DescriptionError(
            f"no JSON in localisation response: {(response.text or '')[:200]}"
        )
    try:
        localised = str(json.loads(match.group(0))["text"]).strip()
    except (json.JSONDecodeError, KeyError) as exc:
        raise DescriptionError(f"unusable localisation response: {exc}") from exc
    if not localised:
        raise DescriptionError(
            f"the model returned no narration for {text!r} in {market}"
        )

    if cache is not None:
        cache.put_json({"text": localised}, *ckey)
    return localised


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def collision_ms(
    placements: list[tuple[Gap, float]], dialogue: list[Interval]
) -> Measurement:
    """How far the narration talks over dialogue. Zero is the only pass.

    `placements` are (gap, spoken duration). A cue starts at its gap's start,
    so anything longer than the gap runs into whatever comes next -- and what
    comes next is, by construction, a line of dialogue.

    This is the whole quality bar for AD, and it is a fact about audio rather
    than an opinion about writing.
    """
    worst = 0.0
    worst_gap: int | None = None
    per_cue: list[float] = []
    for gap, spoken_ms in placements:
        overrun = max(0.0, spoken_ms - gap.duration_ms)
        per_cue.append(round(overrun, 1))
        if overrun > worst:
            worst, worst_gap = overrun, gap.index

    return Measurement(
        key="accessibility.ad_collision_ms",
        value=round(worst, 1),
        unit="ms",
        method=METHOD,
        detail={
            "cues": len(placements),
            "colliding_cues": sum(1 for o in per_cue if o > 0),
            "worst_gap_index": worst_gap,
            "per_cue_overrun_ms": per_cue,
            "dialogue_lines": len(dialogue),
        },
    )


def coverage(descriptions: list[Description], gaps: list[Gap]) -> Measurement:
    """What fraction of describable silence actually carries description.

    Not a pass/fail bar -- a gap the model judged not worth describing is a
    legitimate editorial choice, and filler is worse than silence. Published so
    a human can see at a glance whether a track is thin, which is the kind of
    thing that only shows up when someone listens.
    """
    described = sum(d.gap.duration_ms for d in descriptions)
    available = sum(g.duration_ms for g in gaps) or 1.0
    return Measurement(
        key="accessibility.ad_coverage_ratio",
        value=round(described / available, 3),
        unit="ratio",
        method=METHOD,
        detail={
            "gaps_available": len(gaps),
            "gaps_described": len(descriptions),
            "describable_ms": round(available, 1),
        },
    )
