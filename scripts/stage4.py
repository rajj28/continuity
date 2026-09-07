"""Stage 4: build the audio description track a market is blocked without.

The other repair strategies change an asset that exists. This one BUILDS an
asset that does not, which is the second of the three responses a release
blocker can have -- and the reason `Completeness.repairable` is True where
`Clearance.repairable` is False.

Four steps, and the model is only trusted in two of them:

  1. Gemini watches the scene video and writes English description for the
     silences. This is the one place in the system where a model looks at the
     picture, because no probe can say what is happening on screen.
  2. Each description is localised into the market's language against the gap
     it has to fit -- the same adaptation machinery the dialogue uses, with the
     gap standing in for the dialogue slot.
  3. TTS speaks it.
  4. ffmpeg measures whether it fits. A description that overruns its gap talks
     over the next line, and that is a fact about audio rather than an opinion
     about writing.

    python scripts/stage4.py --scene S03 --market de-DE
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.ad import (  # noqa: E402
    collision_ms,
    coverage,
    describe_scene,
    find_gaps,
    localise_description,
)
from media.dub.assemble import Policy, assemble  # noqa: E402
from media.dub.cache import Cache  # noqa: E402
from media.dub.quota import DailyQuotaExhausted  # noqa: E402
from media.dub.segment import DubSegment  # noqa: E402
from media.dub.translate import text_cache  # noqa: E402
from media.dub.tts import VOICE, audio_cache, synthesise  # noqa: E402
from media.qc.loudness import measure_loudness  # noqa: E402
from media.qc.report import QCReport, QCStore  # noqa: E402
from media.qc.types import Interval  # noqa: E402
from media.store import Asset, ParentRef, Store  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import asset_span, record_measurement, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.stage4")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", default="S03")
    parser.add_argument("--market", required=True)
    parser.add_argument("--manifest", default="out/scenes/manifest.json")
    parser.add_argument("--store", default="out/store")
    parser.add_argument("--out", default="out/ad")
    parser.add_argument("--attempts", type=int, default=2,
                        help="rewrite attempts per cue when the narration "
                             "overruns its gap")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    scene = next(s for s in manifest["scenes"] if s["id"] == args.scene)
    title, market = manifest["title_id"], args.market
    origin, scene_ms = scene["in_ms"], scene["duration_ms"]

    video = Path("out/scenes") / f"{args.scene}.mp4"
    if not video.exists():
        raise SystemExit(f"{video} is missing; run scripts/stage1.py first")

    from google import genai

    from telemetry.otel import load_env
    key = load_env().get("GEMINI_API_KEY", "")
    if not key:
        raise SystemExit("no GEMINI_API_KEY in .env.local")
    client = genai.Client(api_key=key)

    store = Store(Path(args.store))
    qc = QCStore(Path(args.store))
    scene_video = store.load(f"{title}:{args.scene}:scene_video")
    root = Path(args.store)
    ad_cache = Cache(root / "ad", suffix=".json")
    audio = audio_cache(root)
    lines = text_cache(root)

    gaps = find_gaps(scene["utterances"], origin_ms=origin, scene_ms=scene_ms)
    log.info("%d usable gap(s) in %s", len(gaps), args.scene)

    # 1 -- watch the picture (English, market-independent, cached once)
    descriptions = describe_scene(
        client, video, scene["utterances"],
        origin_ms=origin, scene_ms=scene_ms, cache=ad_cache,
    )
    if not descriptions:
        raise SystemExit("the model described nothing; no track to build")

    tracer, meter = setup("continuity-stage4")
    instruments = Instruments(meter)
    out_dir = Path(args.out) / market
    voice = VOICE[market]

    try:
        with asset_span(
            tracer, f"audio_description.{market}", stage="stage4",
            title_id=title, scene_id=args.scene, market=market,
            parents=[(scene_video.id, scene_video.sha256)],
            extra={"continuity.ad.gaps": len(gaps),
                   "continuity.ad.described": len(descriptions),
                   "gen_ai.system": "gcp.gemini"},
        ) as root_span:
            segments: list[DubSegment] = []
            placements: list[tuple] = []

            partial = False
            for description in descriptions:
                gap = description.gap
                # 2 -- localise, speak, measure, and rewrite shorter if it does
                #      not fit. Same discipline as the dialogue: the loop
                #      condition is the measured duration of real audio, never
                #      the model's opinion of its own brevity.
                narration = ""
                pcm = b""
                try:
                    for attempt in range(1, args.attempts + 1):
                        narration = localise_description(
                            client, description.text, market=market, gap=gap,
                            cache=lines,
                            over_by_ms=None if attempt == 1 else spoken - gap.duration_ms,
                        )
                        pcm = synthesise(client, narration, voice=voice,
                                         cache=audio)
                        spoken = len(pcm) / 2 / 24000 * 1000.0
                        if spoken <= gap.duration_ms:
                            break
                except Exception as exc:
                    if not isinstance(exc.__cause__, DailyQuotaExhausted):
                        raise
                    # A thin track is a real intermediate state, not a crash.
                    # Everything synthesised so far is cached, ad_coverage_ratio
                    # will show visibly how little of the silence is described,
                    # and tomorrow's run fills in the rest for the cost of the
                    # cues that are still missing.
                    log.warning(
                        "daily TTS quota spent after %d of %d cue(s); building "
                        "a partial track and recording it as partial",
                        len(segments), len(descriptions),
                    )
                    partial = True
                    break
                segment = DubSegment(
                    index=gap.index, pcm=pcm,
                    source_text=description.text, target_text=narration,
                    reference_ms=gap.duration_ms,
                    reference_start_ms=gap.start_ms,
                    detail={"gap_index": gap.index, "voice": voice},
                )
                segments.append(segment)
                placements.append((gap, segment.duration_ms))
                log.info(
                    "  gap %d  %5.0f ms of narration in a %.0f ms gap (%+.0f)  %s",
                    gap.index, segment.duration_ms, gap.duration_ms,
                    segment.duration_ms - gap.duration_ms, narration[:44],
                )

            # 4 -- lay it on the timeline. CUE placement, because a description
            # belongs to the silence it was written for; sliding it later would
            # push it into the dialogue it was written to avoid.
            if not segments:
                raise SystemExit(
                    "no narration could be synthesised; nothing to assemble"
                )
            track = assemble(segments, policy=Policy.CUE, total_ms=scene_ms)
            path = track.write(out_dir / f"{args.scene}_ad.wav")
            sha, _ = store.put_file(path)
            asset_id = f"{title}:{args.scene}:audio_description:{market}"

            dialogue = [
                Interval(float(u["start_ms"] - origin), float(u["end_ms"] - origin))
                for u in scene["utterances"]
            ]
            measurements = [
                collision_ms(placements, dialogue),
                # Coverage is measured over the cues that EXIST, not the ones
                # that were written. A track described but not synthesised is
                # not a track.
                coverage([d for d in descriptions
                          if d.gap.index in {s.index for s in segments}], gaps),
            ]
            measurements += measure_loudness(path)

            with asset_span(
                tracer, "ad.track", stage="stage4", title_id=title,
                scene_id=args.scene, market=market, asset_id=asset_id,
                asset_kind="AUDIO_DESCRIPTION", asset_sha=sha,
                parents=[(scene_video.id, scene_video.sha256)],
            ) as span:
                for measurement in measurements:
                    record_measurement(span, measurement)
                    try:
                        instruments.record_measurement(
                            measurement, title=title, scene=args.scene,
                            market=market,
                        )
                    except KeyError:
                        pass

            store.record(Asset(
                id=asset_id, kind="AUDIO_DESCRIPTION", sha256=sha, uri=str(path),
                bytes=path.stat().st_size, title_id=title, scene_id=args.scene,
                market=market, duration_ms=track.duration_ms,
                parents=[ParentRef(scene_video.id, scene_video.sha256, "scene_video")],
                produced_by={
                    "stage": "stage4",
                    "description_model": descriptions[0].detail.get("model", ""),
                    "voice": voice,
                    "cues": len(segments),
                    "partial": partial,
                },
            ))
            qc.put(QCReport(
                sha256=sha, asset_id=asset_id, title_id=title,
                scene_id=args.scene, market=market, measurements=measurements,
                detail={"cues": len(segments), "gaps": len(gaps),
                        "described": len(descriptions), "partial": partial},
            ))
            root_span.set_attribute("continuity.ad.sha256", sha)

        print()
        for measurement in measurements:
            print(f"  {measurement.key:<38} {measurement.value:>9.2f} "
                  f"{measurement.unit}")
        print(f"\ntrack {asset_id}\n      {path}")
        print(f"\ncache -- audio: {audio.summary} | lines: {lines.summary}")
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
