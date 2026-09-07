"""Stage 2: build a dubbed stem for a market, measure it, and publish it.

Stage 1 turned the master into scenes and a dialogue list. This turns a scene
into a real dub -- Gemini Live Translate on the real Sintel dialogue audio --
stores every artefact by content hash with its lineage, measures the result
with the deterministic probes, and emits the trace and metrics that let Grafana
form a verdict.

Nothing here decides whether the dub is acceptable. It measures and publishes.
The judgement happens in the Mimir recording rules, where no agent can reach
it, and this script is not permitted to have an opinion about the outcome --
which is why it exits 0 on a stem that fails every threshold. A build step that
refused to publish bad numbers would be a build step that could hide them.

    python scripts/stage2.py --scene S01 --market de-DE

Each line becomes its own asset:

    SINTEL:S01:dub_line:de-DE:03      parent = the scene audio, at its hash
    SINTEL:S01:dub_stem:de-DE         parents = every line above

so a repair can regenerate one line and leave eleven alone, and the blast
radius of a re-graded master stops exactly where the hashes stop matching.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.dub.assemble import Policy, assemble  # noqa: E402
from media.dub.segment import TTS_RATE  # noqa: E402
from media.dub.tts import BUILD_ATTEMPTS, audio_cache, dub_scene  # noqa: E402
from media.dub.translate import text_cache  # noqa: E402
from media.qc.ffmpeg import write_wav  # noqa: E402
from media.qc.loudness import measure_loudness  # noqa: E402
from media.qc.report import QCReport, QCStore  # noqa: E402
from media.qc.sync import measure_dub  # noqa: E402
from media.qc.types import Interval  # noqa: E402
from media.store import Asset, ParentRef, Store  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import (  # noqa: E402
    asset_span,
    load_env,
    record_measurement,
    setup,
    shutdown,
)

log = logging.getLogger("continuity.stage2")

ROOT = Path(__file__).resolve().parents[1]


def scene_from_manifest(manifest: dict, scene_id: str) -> dict:
    for scene in manifest["scenes"]:
        if scene["id"] == scene_id:
            return scene
    known = ", ".join(s["id"] for s in manifest["scenes"])
    raise SystemExit(f"no scene {scene_id!r} in the manifest; have: {known}")


def reference_intervals(scene: dict) -> list[Interval]:
    """Where each line sits, relative to the start of the scene cut.

    Relative because the probe measures the stem, and the stem starts at the
    scene's in-point rather than at the film's zero.
    """
    origin = scene["in_ms"]
    return [
        Interval(
            start=(u["start_ms"] - origin) / 1000.0,
            end=(u["end_ms"] - origin) / 1000.0,
        )
        for u in scene["utterances"]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", default="S01")
    parser.add_argument("--market", required=True)
    parser.add_argument("--manifest", default="out/scenes/manifest.json")
    parser.add_argument("--store", default="out/store")
    parser.add_argument("--out", default="out/dub")
    parser.add_argument("--policy", default=Policy.SEQUENTIAL.value,
                        choices=[p.value for p in Policy])
    parser.add_argument("--gap-ms", type=float, default=0.0,
                        help="minimum silence between lines under SEQUENTIAL")
    parser.add_argument("--attempts", type=int, default=BUILD_ATTEMPTS,
                        help="adaptation attempts per line. 1 is a first-pass "
                             "build; raising it is the REWRITE repair")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    scene = scene_from_manifest(manifest, args.scene)
    title = manifest["title_id"]
    market, policy = args.market, Policy(args.policy)

    store = Store(Path(args.store))
    qc = QCStore(Path(args.store))
    scene_audio = store.load(f"{title}:{args.scene}:scene_audio")
    source = ROOT / "out" / "scenes" / f"{args.scene}.wav"
    if not source.exists():
        raise SystemExit(
            f"{source} is missing; run scripts/stage1.py first"
        )

    tracer, meter = setup("continuity-stage2")
    instruments = Instruments(meter)
    out_dir = Path(args.out) / market
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        with asset_span(
            tracer, f"dub.{market}", stage="stage2", title_id=title,
            scene_id=args.scene, market=market,
            extra={"continuity.dub.policy": policy.value,
                   "continuity.dub.lines": len(scene["utterances"])},
        ) as root:
            # ---- synthesise ------------------------------------------------
            log.info("translating %d lines into %s (%d attempt(s) each)",
                     len(scene["utterances"]), market, args.attempts)
            # The client is built here rather than inside media/dub: that
            # package has no business reaching into .env.local, and in Cloud
            # Run the key arrives as a real environment variable anyway.
            from google import genai
            key = load_env().get("GEMINI_API_KEY", "")
            if not key:
                raise SystemExit(
                    "no GEMINI_API_KEY in .env.local. Create one at "
                    "https://aistudio.google.com/apikey -- the Gemini API free "
                    "tier needs no Cloud Billing."
                )
            # The free tier allows ten TTS calls per project per day and a
            # scene needs twelve, so the caches are what make a run resumable
            # rather than a gamble on finishing before the quota does.
            audio = audio_cache(Path(args.store))
            lines = text_cache(Path(args.store))
            segments = dub_scene(
                genai.Client(api_key=key), scene["utterances"],
                market=market, origin_ms=scene["in_ms"],
                max_attempts=args.attempts, cache=audio, lines=lines,
            )
            log.info("cache -- audio: %s | lines: %s",
                     audio.summary, lines.summary)

            line_parents: list[ParentRef] = []
            for segment in segments:
                pcm_path = write_wav(
                    segment.pcm,
                    out_dir / f"{args.scene}_line{segment.index:02d}.wav",
                    rate=TTS_RATE,
                )
                sha, _ = store.put_file(pcm_path)
                asset_id = (
                    f"{title}:{args.scene}:dub_line:{market}:{segment.index:02d}"
                )
                with asset_span(
                    tracer, "dub.line", stage="stage2", title_id=title,
                    scene_id=args.scene, market=market, asset_id=asset_id,
                    asset_kind="ADAPTED_LINE", asset_sha=sha,
                    parents=[(scene_audio.id, scene_audio.sha256)],
                    extra={
                        "continuity.line.index": segment.index,
                        "continuity.line.overrun_ms": round(segment.overrun_ms, 1),
                        "continuity.line.expansion": round(segment.expansion, 3),
                        "gen_ai.system": "gcp.gemini",
                        "gen_ai.request.model": segment.detail.get("model", ""),
                    },
                ):
                    store.record(Asset(
                        id=asset_id, kind="ADAPTED_LINE", sha256=sha,
                        uri=str(pcm_path), bytes=pcm_path.stat().st_size,
                        title_id=title, scene_id=args.scene, market=market,
                        duration_ms=segment.duration_ms,
                        parents=[ParentRef(scene_audio.id, scene_audio.sha256,
                                           "scene_audio")],
                        produced_by={
                            "model": segment.detail.get("model", ""),
                            "source_text": segment.source_text,
                            "target_text": segment.target_text,
                            "slot_ms": segment.reference_ms,
                        },
                    ))
                line_parents.append(ParentRef(asset_id, sha, "line"))
                log.info(
                    "  line %02d  %6.0f ms in a %.0f ms slot  (%+.0f)  %s",
                    segment.index, segment.duration_ms, segment.reference_ms,
                    segment.overrun_ms, segment.target_text[:48],
                )

            # ---- assemble --------------------------------------------------
            stem = assemble(
                segments, policy=policy, min_gap_ms=args.gap_ms,
                total_ms=scene["duration_ms"],
            )
            stem_path = stem.write(out_dir / f"{args.scene}_stem.wav")
            stem_sha, _ = store.put_file(stem_path)
            stem_id = f"{title}:{args.scene}:dub_stem:{market}"
            log.info(
                "stem: %.0f ms, worst drift %.0f ms, %d line(s) overrunning",
                stem.duration_ms, stem.worst_drift_ms, len(stem.overrunning),
            )

            # ---- measure ---------------------------------------------------
            # Deterministic probes only. The model made the audio; it does not
            # get to grade it.
            words = sum(u["words"] for u in scene["utterances"])
            measurements = measure_dub(
                stem_path, reference_intervals(scene), words
            )
            measurements += measure_loudness(stem_path)

            with asset_span(
                tracer, "dub.stem", stage="stage2", title_id=title,
                scene_id=args.scene, market=market, asset_id=stem_id,
                asset_kind="DUB_STEM", asset_sha=stem_sha,
                parents=[(p.asset_id, p.sha256) for p in line_parents]
                        + [(scene_audio.id, scene_audio.sha256)],
                extra={
                    "continuity.dub.worst_drift_ms": round(stem.worst_drift_ms, 1),
                    "continuity.dub.worst_overrun_ms":
                        round(stem.worst_overrun_ms, 1),
                    "continuity.dub.overrunning_lines": len(stem.overrunning),
                },
            ) as span:
                for measurement in measurements:
                    record_measurement(span, measurement)
                    instruments.record_measurement(
                        measurement, title=title, scene=args.scene, market=market
                    )

            store.record(Asset(
                id=stem_id, kind="DUB_STEM", sha256=stem_sha, uri=str(stem_path),
                bytes=stem_path.stat().st_size, title_id=title,
                scene_id=args.scene, market=market,
                duration_ms=stem.duration_ms, parents=line_parents,
                produced_by={"policy": policy.value, "gap_ms": args.gap_ms},
            ))
            instruments.set_stale(
                False, title=title, scene=args.scene, market=market
            )
            qc.put(QCReport(
                sha256=stem_sha, asset_id=stem_id, title_id=title,
                scene_id=args.scene, market=market, measurements=measurements,
                detail={
                    "policy": policy.value,
                    "worst_overrun_ms": round(stem.worst_overrun_ms, 1),
                    "overrunning_lines": [p.index for p in stem.overrunning],
                },
            ))
            root.set_attribute("continuity.dub.stem_sha256", stem_sha)

        print()
        for measurement in measurements:
            print(f"  {measurement.key:<38} {measurement.value:>9.2f} "
                  f"{measurement.unit}")
        print(f"\nstem  {stem_id}\n      {stem_sha}\n      {stem_path}")
        print("\nNot judged here. Grafana decides:")
        print(f"  market_release_ready{{title=\"{title}\",market=\"{market}\"}}")
    finally:
        # Mandatory for a short-lived process: the exporters hold an unflushed
        # window, and skipping this silently drops the whole run's telemetry.
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
