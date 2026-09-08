"""Stage 6: assemble the deliverable a market actually receives.

Every stage before this makes an ingredient. Stage 2 makes a dub stem, stage 3
a subtitle file, stage 4 an audio description track, stage 5 the storefront
record. All measured, all versioned, all repairable -- and none of them is a
thing you can hand to a distributor or press play on.

    python scripts/stage6.py --scene S03 --market de-DE

This mixes them into one file: the picture, the dubbed audio, the audio
description as its own selectable track, and the subtitles as a real subtitle
stream with a language tag -- which is what "we localised this for Germany"
actually means to the platform receiving it.

## Why this is the last stage and not the first

It could not have been built earlier, because a package is only as current as
its inputs and this system spends most of its time changing them. The package
records every ingredient's hash as a parent, so the moment a repair rewrites
the dub stem the package is stale against it and the market is blocked until it
is rebuilt. That is the staleness rule doing exactly the job it was designed
for, on the asset where being out of date matters most.

It also means the package is the leaf of the blast radius. Repairing anything
in a market invalidates it, which is the honest answer to "what does this
repair affect" -- and before this stage existed, that answer was always a
subtitle file nobody ships on its own.

## What ffmpeg is asked to do

The video is copied, never re-encoded: this stage assembles, it does not
transcode, and a re-encode here would silently change the picture that was
certified. Audio becomes AAC because MP4 cannot carry the WAV the pipeline
works in. Subtitles become mov_text for the same reason.

The audio description track is the programme mix PLUS the narration, not the
narration alone. A viewer selecting "audio description" expects to hear the
film with description over it; handing them a track of narration in silence
would be a technically valid file and a useless one.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.qc.profiles import load_profiles  # noqa: E402
from media.qc.types import ProbeError  # noqa: E402
from media.store import Asset, ParentRef, Store  # noqa: E402

log = logging.getLogger("continuity.stage6")

# ISO 639-2/B, which is what MP4 language tags use. A market code is not a
# language code -- pt-BR and pt-PT are the same tag and very different
# deliverables -- so the mapping is explicit rather than derived by splitting
# on the hyphen.
LANGUAGE_TAG = {
    "de-DE": ("deu", "German"),
    "fr-FR": ("fra", "French"),
    "pt-BR": ("por", "Portuguese (Brazil)"),
    "hi-IN": ("hin", "Hindi"),
    "ja-JP": ("jpn", "Japanese"),
}


class PackageError(RuntimeError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise PackageError("ffmpeg is not on PATH")
    return found


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        raise PackageError("ffmpeg failed:\n  " + "\n  ".join(tail))


def _current(store: Store, kind: str, *, scene: str, market: str) -> Asset | None:
    """The CURRENT version of one ingredient, or None.

    Read from the index rather than by globbing the output directory, which
    would happily pick up `S03_stem_v4.wav` after a repair had moved on to v6.
    The index is the only thing that knows which bytes are current.
    """
    for asset in store.all_assets():
        if (asset.kind == kind and asset.scene_id == scene
                and asset.market == market):
            return asset
    return None


def build(store: Store, *, title: str, scene: str, market: str,
          video: Path, dest: Path, wants_ad: bool) -> tuple[Path, list[Asset]]:
    """Mux one market's deliverable. Returns the file and what went into it."""
    tag, language = LANGUAGE_TAG.get(market, ("und", market))

    dub = _current(store, "DUB_STEM", scene=scene, market=market)
    subtitle = _current(store, "SUBTITLE", scene=scene, market=market)
    described = _current(store, "AUDIO_DESCRIPTION", scene=scene, market=market)

    if dub is None:
        raise PackageError(
            f"{market} has no dub stem for {scene}. Run stage 2 first -- a "
            f"package without the dub is not a localised release."
        )
    if subtitle is None:
        raise PackageError(
            f"{market} has no subtitle for {scene}. Run stage 3 first."
        )
    if wants_ad and described is None:
        raise PackageError(
            f"{market} requires AUDIO_DESCRIPTION and none has been built. "
            f"Run stage 4 first."
        )

    used = [a for a in (dub, subtitle, described) if a is not None]
    for asset in used:
        if not Path(asset.uri).exists():
            raise PackageError(
                f"{asset.id} is in the index at {asset.sha256[:12]} but its "
                f"file is missing: {asset.uri}"
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["-i", str(video), "-i", str(dub.uri), "-i", str(subtitle.uri)]
    if described is not None:
        inputs += ["-i", str(described.uri)]

    args = [_ffmpeg(), "-y", *inputs]
    if described is not None:
        # Track 2 is the film WITH description over it, which is what a viewer
        # selecting "audio description" expects to hear. `dropout_transition=0`
        # stops amix ducking the dub back up between narration cues, which
        # sounds like the mix breathing.
        args += ["-filter_complex",
                 "[1:a][3:a]amix=inputs=2:duration=first:"
                 "dropout_transition=0:normalize=0[described]"]
        maps = ["-map", "0:v", "-map", "1:a", "-map", "[described]",
                "-map", "2:s"]
    else:
        maps = ["-map", "0:v", "-map", "1:a", "-map", "2:s"]

    args += maps + [
        # Copied, not re-encoded. This stage assembles; a re-encode here would
        # change the picture that was certified.
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-c:s", "mov_text",
        "-metadata:s:a:0", f"language={tag}",
        # MP4 has no per-track `title` atom the way Matroska does -- a track's
        # name lives in its handler. Setting only `title` produced a file whose
        # tracks were all called "SoundHandler", so a viewer choosing between
        # two German audio tracks had nothing to choose by. Both are set: the
        # handler for MP4 players, `title` for anything that remuxes to a
        # container which has one.
        "-metadata:s:a:0", f"title={language} dub",
        "-metadata:s:a:0", f"handler_name={language} dub",
        "-metadata:s:s:0", f"language={tag}",
        "-metadata:s:s:0", f"handler_name={language} subtitles",
        "-metadata", f"title={title} ({market})",
        "-movflags", "+faststart",
    ]
    if described is not None:
        args += ["-metadata:s:a:1", f"language={tag}",
                 "-metadata:s:a:1", f"title={language} audio description",
                 "-metadata:s:a:1",
                 f"handler_name={language} audio description",
                 # The accessibility flag a player reads to offer the track as
                 # a described version rather than a second language.
                 "-disposition:a:1", "descriptions"]
    args.append(str(dest))

    _run(args)
    return dest, used


def describe_streams(path: Path) -> list[dict]:
    """What actually ended up in the file.

    Probed rather than assumed. Every other stage measures what it produced
    instead of trusting that it produced it, and a mux that silently dropped a
    subtitle track would otherwise pass as a complete package.
    """
    proc = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-show_entries",
         "stream=index,codec_type,codec_name:stream_tags=language,title",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise ProbeError(f"could not probe {path.name}")
    return json.loads(proc.stdout or "{}").get("streams", [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--scene", default="S03")
    ap.add_argument("--title", default="SINTEL")
    ap.add_argument("--store", default="out/store")
    ap.add_argument("--scenes", default="out/scenes")
    ap.add_argument("--out", default="out/deliverables")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    profile = load_profiles().get(args.market)
    if profile is None:
        raise PackageError(f"no profile for {args.market}")
    wants_ad = "AUDIO_DESCRIPTION" in profile.get("requires", [])

    store = Store(Path(args.store))
    video = Path(args.scenes) / f"{args.scene}.mp4"
    if not video.exists():
        raise PackageError(f"no scene video at {video}; run stage 1")

    dest = (Path(args.out) / args.market /
            f"{args.title}_{args.scene}_{args.market}.mp4")
    dest, used = build(store, title=args.title, scene=args.scene,
                       market=args.market, video=video, dest=dest,
                       wants_ad=wants_ad)

    streams = describe_streams(dest)
    kinds = [s.get("codec_type") for s in streams]
    if "video" not in kinds or "audio" not in kinds or "subtitle" not in kinds:
        raise PackageError(
            f"the muxed file is missing a stream type: got {kinds}. A package "
            f"that lost its subtitles is worse than one that failed to build, "
            f"because it looks finished."
        )

    # The scene video is a parent too: re-cut the picture and every package
    # built from it is stale, which is the whole point of recording hashes.
    scene_asset = _current(store, "SCENE_VIDEO", scene=args.scene, market=None)
    parents = [ParentRef(a.id, a.sha256, a.kind.lower()) for a in used]
    if scene_asset is not None:
        parents.append(
            ParentRef(scene_asset.id, scene_asset.sha256, "picture"))

    sha, _stored = store.put_file(dest)
    asset_id = f"{args.title}:{args.scene}:package:{args.market}"
    store.record(Asset(
        id=asset_id, kind="PACKAGE", sha256=sha, uri=str(dest),
        bytes=dest.stat().st_size, title_id=args.title, scene_id=args.scene,
        market=args.market, parents=parents,
        produced_by={
            "stage": "stage6",
            "streams": len(streams),
            "audio_tracks": kinds.count("audio"),
            "described": wants_ad,
        },
    ))

    # A delivery manifest beside the file. What a distributor is handed with
    # the media, and what makes the package auditable without this codebase.
    manifest = dest.with_suffix(".manifest.json")
    manifest.write_text(json.dumps({
        "title": args.title, "scene": args.scene, "market": args.market,
        "package_sha256": sha,
        "built_from": [
            {"id": a.id, "kind": a.kind, "version": a.version,
             "sha256": a.sha256} for a in used
        ],
        "streams": streams,
    }, indent=2), encoding="utf-8")

    print(f"\npackage  {dest}")
    print(f"         {sha[:16]}  {dest.stat().st_size / 1e6:.1f} MB")
    for stream in streams:
        tags = stream.get("tags") or {}
        print(f"  {stream.get('codec_type'):<9} {stream.get('codec_name'):<12}"
              f" {tags.get('language', '--'):<5} {tags.get('title', '')}")
    print(f"\nbuilt from {len(used)} measured asset(s):")
    for asset in used:
        print(f"  {asset.kind:<18} v{asset.version}  {asset.sha256[:12]}")
    print(f"\nmanifest {manifest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PackageError, ProbeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
