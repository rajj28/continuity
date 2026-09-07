"""Stage 3: author subtitles from the lines that were dubbed, and measure them.

Costs nothing in model calls. The adapted text already exists in the store as
ADAPTED_LINE assets, and a subtitle is that text laid back onto the picture --
so this stage is pure derivation, and its cost is a hash and a file write.

The lineage matters more than the saving. Each subtitle records the hash of
every adapted line it quotes, so re-adapting one line for length marks the
subtitle stale through the same comparison that governs every other asset. The
agent does not need a rule saying "regenerate subtitles after a rewrite"; the
staleness falls out of the content addressing.

    python scripts/stage3.py --scene S03 --market de-DE
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.dub.subtitle import build_cues, write_srt  # noqa: E402
from media.qc.profiles import profile  # noqa: E402
from media.qc.report import QCReport, QCStore  # noqa: E402
from media.qc.subtitles import measure_subtitles  # noqa: E402
from media.store import Asset, ParentRef, Store  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import asset_span, record_measurement, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.stage3")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", default="S03")
    parser.add_argument("--market", required=True)
    parser.add_argument("--manifest", default="out/scenes/manifest.json")
    parser.add_argument("--store", default="out/store")
    parser.add_argument("--out", default="out/subs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    scene = next(s for s in manifest["scenes"] if s["id"] == args.scene)
    title, market = manifest["title_id"], args.market
    spec = profile(market)["delivery"]

    store = Store(Path(args.store))
    qc = QCStore(Path(args.store))

    # The adapted lines this market actually produced, in scene order.
    prefix = f"{title}:{args.scene}:dub_line:{market}:"
    lines = sorted(
        (a for a in store.all_assets() if a.id.startswith(prefix)),
        key=lambda a: a.id,
    )
    if not lines:
        raise SystemExit(
            f"no adapted lines for {market} {args.scene}; run scripts/stage2.py "
            f"first. A subtitle is derived from the dubbing script, not "
            f"translated separately."
        )

    by_index = {u["index"]: u for u in scene["utterances"]}
    payload = []
    for asset in lines:
        index = int(asset.id.rsplit(":", 1)[1])
        utterance = by_index[index]
        payload.append({
            "start_ms": utterance["start_ms"] - scene["in_ms"],
            "end_ms": utterance["end_ms"] - scene["in_ms"],
            "text": asset.produced_by.get("target_text", ""),
        })

    cues = build_cues(
        payload,
        max_line_chars=int(spec["subtitle_max_line_chars"]),
        max_lines=int(spec["subtitle_max_lines"]),
        min_gap_ms=int(spec.get("subtitle_min_gap_ms", 0)),
    )
    dest = Path(args.out) / market / f"{args.scene}.srt"
    write_srt(cues, dest)

    tracer, meter = setup("continuity-stage3")
    instruments = Instruments(meter)
    try:
        sha, _ = store.put_file(dest)
        asset_id = f"{title}:{args.scene}:subtitle:{market}"
        measurements = measure_subtitles(dest)

        with asset_span(
            tracer, "subtitle.author", stage="stage3", title_id=title,
            scene_id=args.scene, market=market, asset_id=asset_id,
            asset_kind="SUBTITLE", asset_sha=sha,
            parents=[(a.id, a.sha256) for a in lines],
            extra={"continuity.subtitle.cues": len(cues)},
        ) as span:
            for measurement in measurements:
                record_measurement(span, measurement)
                try:
                    instruments.record_measurement(
                        measurement, title=title, scene=args.scene, market=market
                    )
                except KeyError:
                    # Not every subtitle measurement has a canonical series --
                    # line counts and gaps are conformance detail rather than
                    # things a market is judged on. The span still carries them.
                    pass

        store.record(Asset(
            id=asset_id, kind="SUBTITLE", sha256=sha, uri=str(dest),
            bytes=dest.stat().st_size, title_id=title, scene_id=args.scene,
            market=market,
            parents=[ParentRef(a.id, a.sha256, "adapted_line") for a in lines],
            produced_by={"stage": "stage3", "cues": len(cues),
                         "max_line_chars": spec["subtitle_max_line_chars"]},
        ))
        qc.put(QCReport(
            sha256=sha, asset_id=asset_id, title_id=title,
            scene_id=args.scene, market=market, measurements=measurements,
            detail={"cues": len(cues)},
        ))
    finally:
        shutdown()

    print(f"\n{dest}  ({len(cues)} cues)")
    for measurement in measurements:
        print(f"  {measurement.key:<40} {measurement.value:>8.2f} {measurement.unit}")
    print(f"\n  bar for {market}: "
          f"{spec['subtitle_max_cps']} cps, "
          f"{spec['subtitle_max_line_chars']} chars/line")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
