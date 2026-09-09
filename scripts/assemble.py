"""Cut the clips to the narration and produce the finished video.

    python scripts/assemble.py

The narration is the master. Each block of voiceover has a group of shots
assigned to it in `scripts/clips.py`, and this cuts that group to exactly the
length of the block -- so the picture never runs out mid-sentence and never
sits idle waiting for the next one.

Shots are cut long on purpose, so the usual operation is trimming. When a group
is nevertheless short, its last frame is held rather than the clip being sped
up: freezing reads as a deliberate pause, and speeding up a shot of an agent
reasoning would misrepresent how long the system actually takes, which is the
one thing a demo of this project must not do.

Captions are burned in because the rules require English or English subtitles
and a caption track a player might not switch on is a requirement met on
paper. Timing within a block is proportional to sentence length, which is
close enough to read naturally and does not pretend to be a real alignment.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.clips import BLOCKS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "out" / "clips"
VOICE = ROOT / "out" / "voice"
WORK = ROOT / "out" / "edit"
SCRIPT = ROOT / "docs" / "SCRIPT.md"
FINAL = ROOT / "out" / "continuity-demo.mp4"


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def duration(path: Path) -> float:
    result = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def run(args: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(args, capture_output=True, text=True,
                            cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        tail = "\n  ".join((result.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg failed:\n  {tail}")


def spoken() -> dict[str, list[str]]:
    """The sentences of each block, for captions.

    Read from the script rather than kept in a second place, so the words
    burned onto the picture are the words that were spoken.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for match in re.finditer(r"^## (VO \d+)[^\n]*\n(.*?)(?=^## |\Z)",
                             text, re.S | re.M):
        name = match.group(1).lower().replace(" ", "")
        body = " ".join(
            re.sub(r"^>\s?", "", line).strip()
            for line in match.group(2).splitlines()
            if line.strip().startswith(">")
        )
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body)
                     if s.strip()]
        if sentences:
            out[name] = sentences
    return out


def build_block(name: str, shots: list[str], target: float) -> Path:
    """One block of picture, exactly as long as its narration."""
    available = [CLIPS / f"{shot}.mp4" for shot in shots]
    present = [p for p in available if p.exists()]
    missing = [p.stem for p in available if not p.exists()]
    if not present:
        raise RuntimeError(f"{name}: none of {shots} has been shot")
    if missing:
        print(f"  {name}: no clip for {', '.join(missing)} -- "
              f"holding the others longer")

    WORK.mkdir(parents=True, exist_ok=True)
    listing = WORK / f"{name}.txt"
    listing.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in present), encoding="utf-8")

    joined = WORK / f"{name}_joined.mp4"
    run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(joined)])

    have = duration(joined)
    out = WORK / f"{name}.mp4"
    if have >= target:
        # Trim. Re-encoded rather than stream-copied because a copy can only
        # cut at a keyframe, which drifts the cut by up to a second and puts
        # the picture out of step with the voice for the rest of the film.
        run([ffmpeg(), "-y", "-i", str(joined), "-t", f"{target:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-an", str(out)])
    else:
        run([ffmpeg(), "-y", "-i", str(joined),
             "-vf", f"tpad=stop_mode=clone:stop_duration={target - have:.3f}",
             "-t", f"{target:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-an", str(out)])
    return out


def captions(order: list[tuple[str, float, float]]) -> Path:
    """An SRT covering the whole film, one cue per sentence."""
    lines, index = [], 1
    sentences = spoken()

    def stamp(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    for name, start, length in order:
        parts = sentences.get(name, [])
        if not parts:
            continue
        total = sum(len(p) for p in parts) or 1
        at = start
        for part in parts:
            span = length * len(part) / total
            lines.append(f"{index}\n{stamp(at)} --> {stamp(at + span)}\n"
                         f"{part}\n")
            index += 1
            at += span
    path = WORK / "captions.srt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--gap", type=float, default=1.3,
                    help="seconds the picture runs on after each block")
    args = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    blocks, timeline, at = [], [], 0.0
    for name, shots in BLOCKS.items():
        audio = VOICE / f"{name}.mp3"
        if not audio.exists():
            print(f"  {name}: no narration, skipped")
            continue
        # The picture runs a beat past the words. The narration reads at close
        # to 190 words a minute and no amount of punctuation slowed it down --
        # the model paces by content, not by the line breaks in the script --
        # so the room to breathe is made here, where it is a number rather
        # than a hope. It also gives the viewer a moment to look at the thing
        # that has just been described.
        spoken_for = duration(audio)
        target = spoken_for + args.gap
        blocks.append(build_block(name, shots, target))
        timeline.append((name, at, spoken_for))
        at += target
        print(f"  {name:<5} {spoken_for:>5.1f}s speech + {args.gap:.1f}s   "
              f"{', '.join(shots)}")

    if not blocks:
        print("nothing to assemble", file=sys.stderr)
        return 1

    listing = WORK / "all.txt"
    listing.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in blocks), encoding="utf-8")
    silent = WORK / "picture.mp4"
    run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(silent)])

    # Silence of exactly the gap length after each block, so the audio track
    # stays in step with the picture blocks it was cut against. Without it the
    # two drift apart by the whole accumulated gap -- nine seconds by the end,
    # which is the difference between a sentence landing on its shot and
    # landing on the one after it.
    gap = WORK / "gap.m4a"
    run([ffmpeg(), "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{args.gap:.3f}",
         "-c:a", "aac", "-b:a", "192k", str(gap)])

    pieces = []
    for name, _start, _length in timeline:
        # Re-encoded to one common format first: concat -c copy refuses to
        # join streams that disagree about sample rate or channel count, and
        # the generated silence will not match an mp3 by accident.
        block = WORK / f"{name}.m4a"
        run([ffmpeg(), "-y", "-i", str(VOICE / f"{name}.mp3"),
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
             str(block)])
        pieces += [block, gap]

    voices = WORK / "voice.txt"
    voices.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    narration = WORK / "narration.m4a"
    run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(voices),
         "-c", "copy", str(narration)])

    filters = []
    if not args.no_captions:
        captions(timeline)
        # A bare filename, with ffmpeg run from the directory holding it.
        # An absolute Windows path cannot survive the filter parser: the drive
        # colon is a separator there, and every level of escaping that gets it
        # past one layer breaks it in the next -- ffmpeg ended up reading
        # "/Users/Acer/..." as an image size.
        filters.append(
            "subtitles=captions.srt:force_style='FontName=Segoe UI,FontSize=17,"
            "PrimaryColour=&H00F1F5F6,OutlineColour=&HC0000000,BorderStyle=3,"
            "Outline=1,Shadow=0,MarginV=42'")

    command = [ffmpeg(), "-y", "-i", str(silent), "-i", str(narration)]
    if filters:
        command += ["-vf", ",".join(filters)]
    command += ["-c:v", "libx264", "-preset", "slow", "-crf", "19",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart", str(FINAL)]
    run(command, cwd=WORK)

    length = duration(FINAL)
    print(f"\n  {FINAL}")
    print(f"  {length:.1f}s = {int(length) // 60}:{int(length) % 60:02d}   "
          f"{FINAL.stat().st_size / 1e6:.1f} MB")
    if length > 180:
        print("  OVER THREE MINUTES -- the limit is hard; cut a sentence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
