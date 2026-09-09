"""Build the one block of the film whose sound is the product, not a voice.

    python scripts/listen.py

Every other block of narration is somebody saying what a number means. This one
puts the actual files on the soundtrack: the scene as delivered, the German dub
over the same seconds of the same scene, and the described track. Three short
cues sit either side of them, and nothing is said over the audio itself --
talking across a dub is the exact failure `ad_collision_ms` exists to catch,
and doing it in the demo would be its own kind of answer.

The files are found the way the control room finds them: by asset id through
the store, not by a path typed here. If a repair changes which bytes are
current, this picks up the new ones, and if the store cannot resolve one this
fails rather than quietly cutting a shorter block.

Writes two things:

  out/voice/vo2b.mp3        the composite, one more block for the assembler
  out/voice/vo2b.cues.json  when each cue is spoken, so the burned captions
                            land on the words and not spread evenly across
                            twenty seconds of dialogue

The same timeline drives the picture: `scripts/clips.py` presses play on each
transport at these offsets, so the row that is moving is the row you can hear.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VOICE = ROOT / "out" / "voice"
WORK = ROOT / "out" / "edit" / "listen"
MARKET = "de-DE"

# Which output, which cue, and how many seconds of it. The original and the
# dub are cut from the SAME seconds of the same scene -- the comparison is
# only worth anything if the picture underneath both is identical, which is
# the claim "same picture, same gaps" is making.
# The windows are chosen off `silencedetect`, not by eye. The dub stem speaks
# at 0.98, 7.94, 12.96 and 16.02 seconds and is silent between; the first
# window tried was 4.0 to 9.0, which is four seconds of nothing and one second
# of a line, and would have played as a broken file. 12.9 onward carries two
# lines with a real gap between them, which is what a dub sounds like.
PLAN = [
    ("vo8", "Original audio", 12.9, 5.0),
    ("vo9", "Dubbed dialogue", 12.9, 5.0),
    ("vo10", "Audio description", 2.9, 6.0),
]


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def duration(path: Path) -> float:
    out = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n  ".join((result.stderr or "").strip().splitlines()[-6:])
        raise RuntimeError("ffmpeg failed:\n  " + tail)


# Playback gain for the product files, and nothing else. Measured across the
# three excerpts, they sit at -39, -30 and -24 dB mean: the scene mix is at
# broadcast level and mostly room tone, the dub stem is dialogue with silence
# between the lines, and the described track has narration in those silences.
# Played raw against a voiceover at -27, the original is inaudible and a viewer
# concludes nothing happened.
#
# This is a listening level, not a measurement. What the delivered loudness IS
# stays on the screen beside it -- `audio_loudness_lufs`, -22.96 against a -23
# target -- read off the file by ffmpeg and not off this.
LISTEN_LUFS = "loudnorm=I=-18:TP=-2:LRA=11"


def encode(source: Path, target: Path, *, start: float = 0.0,
           length: float | None = None, level: bool = False) -> Path:
    """One common format for everything, before any of it is joined.

    concat refuses to join streams that disagree about sample rate or channel
    count, and a 48 kHz mono dub stem will not match a 44.1 kHz stereo mp3 by
    accident.
    """
    args = [ffmpeg(), "-y", "-v", "error"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(source)]
    if length is not None:
        args += ["-t", f"{length:.3f}"]
    if level:
        args += ["-af", LISTEN_LUFS]
    args += ["-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "192k",
             str(target)]
    run(args)
    return target


def outputs() -> dict[str, Path]:
    """The produced files, resolved the way the control room resolves them."""
    from ui.server import State

    state = State()
    found = {}
    for entry in state.outputs(MARKET):
        got = state.media(entry["asset"])
        if got:
            found[entry["label"]] = got[0]
    return found


def main() -> int:
    available = outputs()
    missing = [label for _, label, _, _ in PLAN if label not in available]
    if missing:
        print("no media for " + ", ".join(missing) + " -- run the pipeline "
              "for " + MARKET + " first", file=sys.stderr)
        return 1

    WORK.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    cues: list[list] = []
    at = 0.0

    for cue, label, start, length in PLAN:
        voice = VOICE / f"{cue}.mp3"
        if not voice.exists():
            print(f"no {cue}.mp3 -- run scripts/voice.py", file=sys.stderr)
            return 1
        spoken = duration(voice)
        pieces.append(encode(voice, WORK / f"{cue}.m4a"))
        cues.append([round(at, 3), round(at + spoken, 3), cue])
        at += spoken

        source = available[label]
        pieces.append(encode(source, WORK / f"{label.replace(' ', '_')}.m4a",
                             start=start, length=length, level=True))
        print(f"  {label:<18} {start:>4.1f}s +{length:.1f}s  "
              f"at {at:5.1f}s   {source.name}")
        at += length

    listing = WORK / "pieces.txt"
    listing.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    composite = VOICE / "vo2b.mp3"
    # Staged and moved, like every other take: a composite half-written over a
    # good one is a block the assembler will happily cut picture against.
    staging = composite.with_suffix(composite.suffix + ".part")
    # `-f mp3` named rather than inferred: ffmpeg reads the muxer off the last
    # extension, and the last extension here is `.part` on purpose.
    run([ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c:a", "libmp3lame", "-b:a", "192k",
         "-f", "mp3", str(staging)])
    staging.replace(composite)

    (VOICE / "vo2b.cues.json").write_text(
        json.dumps(cues, indent=1), encoding="utf-8")

    print(f"\n  {composite}   {duration(composite):.1f}s")
    print(f"  cues at " + ", ".join(f"{c[0]:.1f}s" for c in cues))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
