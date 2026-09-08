"""Stage 5: localise the storefront record, and analyse the picture for
on-screen text.

Two deliverables nobody films, both of which block a real release.

    python scripts/stage5.py --market de-DE          # metadata
    python scripts/stage5.py --analyse-picture       # forced-narrative need

The metadata half is a length-budgeted translation, which is the same shape as
the dialogue adaptation: the model proposes and the character count disposes.
Storefront limits are hard -- a synopsis two characters over is rejected at
ingest rather than truncated -- so a record is re-written shorter when it
overruns rather than trusted.

The picture half asks Gemini to watch the cut and report whether it contains
text or foreign dialogue that a viewer with subtitles switched off would miss.
That finding is recorded against the master's hash, because re-cutting changes
what is on screen and an analysis of an earlier cut says nothing about this one.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.dub.cache import Cache  # noqa: E402
from media.dub.quota import call as quota_call  # noqa: E402
from media.metadata import (  # noqa: E402
    FIELD_LIMITS,
    ForcedNarrativeNeed,
    LocalisedMetadata,
    MetadataStore,
    check_metadata,
)
from media.qc.profiles import profile  # noqa: E402
from media.store import Store  # noqa: E402
from telemetry.genai import GenAI  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import asset_span, load_env, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.stage5")

from media.model import model_for  # noqa: E402

MODEL = model_for("text")
TITLE = "SINTEL"

SOURCE = {
    "title": "Sintel",
    "short_synopsis": "A lone warrior searches a hostile world for the "
                      "dragon she raised from a hatchling.",
    "synopsis": "Sintel crosses deserts and frozen mountains hunting for "
                "Scales, the wounded dragon she nursed back to health and "
                "then lost. Her search costs her more than she expects, and "
                "what she finds at the end of it is not what she left behind.",
    "genre": ["Animation", "Fantasy", "Adventure"],
}

_METADATA_SYSTEM = """You localise storefront metadata for a film catalogue.

This is the title, short synopsis and synopsis a viewer reads before pressing
play. It is marketing copy, not a subtitle: it should read naturally in the
target language rather than tracking the English word order.

Rules you always follow:
- Character limits are HARD. A field over its limit is rejected at ingest, not
  truncated. Count characters, not words.
- Translate the title as a distributor would. A title that stays in English
  when the market's language is not English is a record nobody translated.
- Do not invent plot. Everything you write must be in the source text.
- Keep proper nouns as they are.

Answer with JSON only:
{"title": "...", "short_synopsis": "...", "synopsis": "...", "chars": {"title": N, "short_synopsis": N, "synopsis": N}}"""

_PICTURE_SYSTEM = """You analyse film footage for forced-narrative requirements.

A forced narrative is a subtitle that must be burned in or force-displayed even
when the viewer has subtitles switched OFF, because the information is only on
screen and only in text -- signage, a letter, a readable inscription, a caption,
or dialogue spoken in a language other than the film's main one.

Report only what a viewer would MISS without it. Ignore credits, logos and
decorative or illegible marks. If there is nothing, say so: an unnecessary
forced narrative writes on the picture for no reason.

Answer with JSON only:
{"needed": true|false, "cues": N, "note": "<= 25 words on what and where"}"""


def _json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise SystemExit(f"no JSON in model response: {(raw or '')[:200]}")
    return json.loads(match.group(0))


def localise(client, market: str, *, over: dict | None = None,
             cache: Cache | None = None) -> LocalisedMetadata:
    language = profile(market)["language"]
    lines = [
        f"Target language: {language}",
        "",
        "Character limits (hard):",
        *[f"  {name}: {limit}" for name, limit in FIELD_LIMITS.items()],
        "",
        f"Source title: {SOURCE['title']}",
        f"Source short synopsis: {SOURCE['short_synopsis']}",
        f"Source synopsis: {SOURCE['synopsis']}",
    ]
    if over:
        # The measured overrun, not a complaint. Same feedback discipline as
        # the dialogue adaptation.
        lines += ["", "A previous attempt was too long: " + ", ".join(
            f"{name} was {got} characters against a {FIELD_LIMITS[name]} limit"
            for name, got in over.items()
        ) + ". Write it shorter."]
    prompt = "\n".join(lines)

    ckey = (MODEL, "metadata", market, str(sorted((over or {}).items())))
    if cache is not None and (hit := cache.get_json(*ckey)) is not None:
        return LocalisedMetadata(market=market, language=language[:2].lower(),
                                 genre=SOURCE["genre"], **hit)

    from google.genai import types
    response = quota_call(
        MODEL,
        lambda: client.models.generate_content(
            model=MODEL, contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_METADATA_SYSTEM, temperature=0.4,
                response_mime_type="application/json",
            ),
        ),
        label=f"metadata({market})",
    )
    obj = _json(response.text)
    fields = {name: str(obj.get(name, "")).strip() for name in FIELD_LIMITS}
    if cache is not None:
        cache.put_json(fields, *ckey)
    return LocalisedMetadata(market=market, language=language[:2].lower(),
                             genre=SOURCE["genre"], **fields)


def analyse_picture(client, video: Path, master_sha256: str,
                    cache: Cache | None = None) -> ForcedNarrativeNeed:
    ckey = (MODEL, "forced_narrative", master_sha256, video.name)
    if cache is not None and (hit := cache.get_json(*ckey)) is not None:
        return ForcedNarrativeNeed(master_sha256=master_sha256, **hit)

    from google.genai import types
    response = quota_call(
        MODEL,
        lambda: client.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=video.read_bytes(),
                                      mime_type="video/mp4"),
                "Does this footage contain on-screen text or foreign-language "
                "dialogue that needs a forced narrative?",
            ],
            config=types.GenerateContentConfig(
                system_instruction=_PICTURE_SYSTEM, temperature=0.2,
                response_mime_type="application/json",
            ),
        ),
        label="forced_narrative",
    )
    obj = _json(response.text)
    found = {"needed": bool(obj.get("needed")),
             "cues": int(obj.get("cues") or 0),
             "note": str(obj.get("note", ""))[:200]}
    if cache is not None:
        cache.put_json(found, *ckey)
    return ForcedNarrativeNeed(master_sha256=master_sha256, **found)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", action="append", default=[],
                    help="localise this market's storefront record; repeatable")
    ap.add_argument("--analyse-picture", action="store_true",
                    help="ask whether the cut needs forced narratives")
    ap.add_argument("--scene", default="S03")
    ap.add_argument("--store", default="out/store")
    ap.add_argument("--attempts", type=int, default=2)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    env = load_env()
    from media.model import client as build_client, describe
    log.info("model backend: %s", describe())
    client = build_client()

    root = Path(args.store)
    store = Store(root)
    metadata = MetadataStore(root)
    cache = Cache(root / "meta", suffix=".json")
    tracer, meter = setup("continuity-stage5")
    instruments = Instruments(meter)
    genai = GenAI(tracer, instruments, "metadata")

    try:
        for market in args.market:
            over = None
            for attempt in range(1, args.attempts + 1):
                with genai.call(MODEL, extra={"continuity.market": market}) as span:
                    record = localise(client, market, over=over, cache=cache)
                result = check_metadata(
                    market, record, source_title=SOURCE["title"],
                    source_synopsis=SOURCE["short_synopsis"],
                )
                log.info("  %s attempt %d: %s", market, attempt, result.reason)
                if result.complete:
                    break
                over = result.detail.get("over")
                if not over:
                    break        # a failure re-writing cannot fix
            path = metadata.put(record, title=TITLE)
            with asset_span(
                tracer, "metadata.localise", stage="stage5", title_id=TITLE,
                market=market, asset_kind="METADATA",
                extra={"gen_ai.system": "gcp.gemini",
                       "gen_ai.request.model": MODEL},
            ):
                pass
            print(f"\n{market}  {path}")
            print(f"  title           {record.title}")
            print(f"  short synopsis  {record.short_synopsis[:70]}")
            for name in FIELD_LIMITS:
                print(f"  {name:<15} {len(getattr(record, name)):>4} / "
                      f"{FIELD_LIMITS[name]}")

        if args.analyse_picture:
            video = Path("out/scenes") / f"{args.scene}.mp4"
            master = store.load(f"{TITLE}:master").sha256
            with genai.call(MODEL, extra={"continuity.stage": "picture"}):
                need = analyse_picture(client, video, master, cache=cache)
            (root / "forced_narrative.json").write_text(
                json.dumps({"master_sha256": need.master_sha256,
                            "needed": need.needed, "cues": need.cues,
                            "note": need.note}, indent=2),
                encoding="utf-8",
            )
            print(f"\nforced narratives needed: {need.needed}"
                  f"  cues: {need.cues}")
            print(f"  {need.note}")
            print(f"  recorded against master {master[:16]}")
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
