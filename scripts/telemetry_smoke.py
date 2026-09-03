#!/usr/bin/env python
"""Emit one real release-build trace and its metrics to Grafana Cloud.

Reads the actual Stage 1 manifest, so the spans carry the real Sintel asset
hashes rather than a synthetic payload. This is the gate that proves the whole
observability substrate works: if these land and are queryable through MCP,
the agent has something true to reason about.

    python scripts/telemetry_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opentelemetry import trace as ot  # noqa: E402

from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import asset_span, setup, shutdown  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "out" / "scenes" / "manifest.json"
MARKETS = ["fr-FR", "de-DE", "pt-BR", "hi-IN", "ja-JP"]

# Stand-in QC results for this smoke run. Real values arrive once the dubbing
# stage exists; the point here is that the SHAPE and the asset identity are real.
SYNC_OFFSET_MS = {"fr-FR": 41.0, "de-DE": 480.0, "pt-BR": 55.0,
                  "hi-IN": 63.0, "ja-JP": 88.0}


def main() -> int:
    if not MANIFEST.exists():
        raise SystemExit("run scripts/stage1.py first")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    title = manifest["title_id"]
    master_sha = manifest["master"]["sha256"]
    s01 = next(s for s in manifest["scenes"] if s["id"] == "S01")
    scene_audio = s01["assets"]["SCENE_AUDIO"]

    tracer, meter = setup("continuity-pipeline")

    inst = Instruments(meter)

    print(f"title={title}  master={master_sha[:16]}..  scene=S01")

    with asset_span(
        tracer, "release.build",
        stage="release_build", title_id=title,
        asset_id=f"{title}:master", asset_kind="MASTER", asset_sha=master_sha,
        extra={"continuity.markets": MARKETS,
               "continuity.master_version": 1},
    ) as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        print(f"trace_id={trace_id}\n")

        with asset_span(
            tracer, "scene.build",
            stage="scene_cut", title_id=title, scene_id="S01",
            asset_id=scene_audio["asset_id"], asset_kind="SCENE_AUDIO",
            asset_sha=scene_audio["sha256"],
            parents=[(f"{title}:master", master_sha)],
            extra={"continuity.scene.in_ms": s01["in_ms"],
                   "continuity.scene.out_ms": s01["out_ms"],
                   "continuity.scene.utterances": s01["utterance_count"]},
        ):
            for market in MARKETS:
                offset = SYNC_OFFSET_MS[market]
                labels = {"title": title, "scene": "S01", "market": market}

                with asset_span(
                    tracer, "dub.synthesize",
                    stage="tts", title_id=title, scene_id="S01", market=market,
                    asset_id=f"{title}:S01:{market}:dub_stem",
                    asset_kind="DUB_STEM",
                    parents=[(scene_audio["asset_id"], scene_audio["sha256"])],
                    extra={"continuity.tts.engine": "chirp3-hd"},
                ) as dub:
                    time.sleep(0.05)

                    with asset_span(
                        tracer, "qc.probe",
                        stage="qc", title_id=title, scene_id="S01", market=market,
                        extra={"continuity.measure.dub_sync_offset_ms": offset},
                    ) as qc:
                        passed = offset <= 120.0
                        qc.set_attribute("continuity.check.passed", passed)
                        dub.set_attribute("continuity.check.passed", passed)

                    inst.gauge("dub_sync_offset_ms").set(offset, labels)
                    inst.set_stale(False, title=title, scene="S01", market=market)
                    print(f"  {market}  sync_offset={offset:6.1f}ms  "
                          f"{'PASS' if offset <= 120 else 'FAIL'}")

    print("\nflushing...")
    shutdown()
    print("done. trace and metrics dispatched to Grafana Cloud.")
    print(f"\nTraceQL to find descendants of this master:\n"
          f'  {{ .continuity.asset.parent_sha256 = "{master_sha}" }}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
