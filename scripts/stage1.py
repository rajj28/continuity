#!/usr/bin/env python
"""Stage 1 -- master ingest to scene manifest.

    master.mp4 + dialogue_list.srt
      -> content-addressed into the store
      -> segmented into scenes on dialogue gaps
      -> each scene cut from the real master with ffmpeg
      -> reference utterance boundaries derived per scene
      -> manifest written, every asset recording its parents' hashes

Everything downstream (dubbing, QC, lineage, blast radius) reads this manifest.
Re-running is idempotent: identical input bytes produce identical hashes and
no duplicate objects.

    python scripts/stage1.py --master <path> --dialogue <path> [--scene S01]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.dialogue import Scene, load_dialogue, segment_scenes  # noqa: E402
from media.qc.ffmpeg import duration_ms  # noqa: E402
from media.store import Asset, ParentRef, Store  # noqa: E402

TITLE_ID = "SINTEL"


def cut_scene(master: Path, scene: Scene, out_dir: Path) -> tuple[Path, Path]:
    """Cut one scene's video and a mono analysis track from the real master.

    Re-encodes rather than stream-copying: keyframe-aligned copy would silently
    shift the in-point by up to a GOP, and a sync probe measuring against a
    reference that moved is worse than no probe at all.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / f"{scene.id}.mp4"
    audio = out_dir / f"{scene.id}.wav"
    start_s = scene.in_ms / 1000.0
    dur_s = scene.duration_ms / 1000.0

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start_s:.3f}", "-i", str(master),
         "-t", f"{dur_s:.3f}", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-b:a", "192k", str(video)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start_s:.3f}", "-i", str(master),
         "-t", f"{dur_s:.3f}", "-vn", "-ac", "1", "-ar", "48000", str(audio)],
        check=True,
    )
    return video, audio


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", required=True, type=Path)
    ap.add_argument("--dialogue", required=True, type=Path)
    ap.add_argument("--store", type=Path, default=Path("out/store"))
    ap.add_argument("--work", type=Path, default=Path("out/scenes"))
    ap.add_argument("--scene", help="cut only this scene id (e.g. S01)")
    args = ap.parse_args()

    t0 = time.time()
    store = Store(args.store)

    # 1 -- ingest the master, content-addressed -----------------------------
    master_sha, master_obj = store.put_file(args.master)
    master = Asset(
        id=f"{TITLE_ID}:master",
        kind="MASTER",
        sha256=master_sha,
        uri=str(master_obj),
        bytes=args.master.stat().st_size,
        title_id=TITLE_ID,
        duration_ms=duration_ms(args.master),
        produced_by={"stage": "ingest", "source": args.master.name},
    )
    store.record(master)
    print(f"master   {master_sha[:16]}..  {master.duration_ms/1000:.1f}s")

    # 2 -- ingest the dialogue list ----------------------------------------
    dl_sha, dl_obj = store.put_file(args.dialogue)
    dialogue = Asset(
        id=f"{TITLE_ID}:dialogue_list",
        kind="DIALOGUE_LIST",
        sha256=dl_sha,
        uri=str(dl_obj),
        bytes=args.dialogue.stat().st_size,
        title_id=TITLE_ID,
        produced_by={"stage": "ingest", "source": args.dialogue.name},
    )
    store.record(dialogue)

    cues = load_dialogue(args.dialogue)
    scenes = segment_scenes(cues, limit_ms=int(master.duration_ms or 0))
    print(f"dialogue {dl_sha[:16]}..  {len(cues)} cues -> {len(scenes)} scenes\n")

    # 3 -- cut scenes and record each as an asset with its parents ---------
    manifest: dict = {
        "title_id": TITLE_ID,
        "master": {"asset_id": master.id, "sha256": master_sha,
                   "duration_ms": master.duration_ms},
        "dialogue_list": {"asset_id": dialogue.id, "sha256": dl_sha},
        "scene_gap_ms": 40_000,
        "scenes": [],
    }

    for scene in scenes:
        entry = scene.to_dict()
        wanted = args.scene is None or args.scene == scene.id

        if wanted:
            video, audio = cut_scene(args.master, scene, args.work)
            parents = [
                ParentRef(master.id, master_sha, "master"),
                ParentRef(dialogue.id, dl_sha, "dialogue_list"),
            ]
            for kind, path in (("SCENE_VIDEO", video), ("SCENE_AUDIO", audio)):
                sha, obj = store.put_file(path)
                asset = Asset(
                    id=f"{TITLE_ID}:{scene.id}:{kind.lower()}",
                    kind=kind,  # type: ignore[arg-type]
                    sha256=sha,
                    uri=str(obj),
                    bytes=path.stat().st_size,
                    title_id=TITLE_ID,
                    scene_id=scene.id,
                    duration_ms=duration_ms(path),
                    parents=parents,
                    produced_by={"stage": "cut", "in_ms": scene.in_ms,
                                 "out_ms": scene.out_ms, "tool": "ffmpeg"},
                )
                store.record(asset)
                entry.setdefault("assets", {})[kind] = {
                    "asset_id": asset.id, "sha256": sha,
                    "duration_ms": asset.duration_ms, "uri": asset.uri,
                }

        entry["reference_intervals_ms"] = [
            [i.start_ms, i.end_ms] for i in scene.reference_intervals()
        ]
        manifest["scenes"].append(entry)

        flag = "cut" if wanted else "   "
        print(f"  {scene.id} {flag}  {scene.in_ms/1000:7.2f}-{scene.out_ms/1000:7.2f}s  "
              f"{len(scene.utterances):>2} utt  {scene.words:>3} words  "
              f"{scene.speech_ms/1000:5.1f}s speech")

    out = args.work / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest -> {out}   ({time.time()-t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
