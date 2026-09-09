"""Generate the narration from docs/SCRIPT.md, one file per block.

    python scripts/voice.py            # generate anything missing
    python scripts/voice.py --force    # regenerate everything

Blocks rather than one long take, for the same reason the video is shot in
clips: a bad read costs one block instead of the whole narration, and short
files are far easier to lay against picture.

The script is the source of truth. Parsing it here rather than keeping a second
copy in code means the words that get spoken are the words a reader reviewed,
and there is no way for the two to drift apart.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "SCRIPT.md"
OUT = ROOT / "out" / "voice"

# River -- relaxed, neutral, informative. Chosen for a technical demo: the
# "storyteller" voices put emphasis on words that do not want it, and a
# narration that sounds excited about a loudness measurement is distracting.
VOICE = "SAz9YHcvj6GT2YYXdXww"
MODEL = "eleven_multilingual_v2"
SETTINGS = {"stability": 0.55, "similarity_boost": 0.75, "style": 0.0}


def blocks() -> list[tuple[str, str]]:
    """(name, text) for each `## VO n` section, in order.

    Only the quoted lines are spoken. The headings say what the block is over,
    and the prose around them is direction for whoever assembles it -- reading
    either aloud would be nonsense.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    out = []
    for match in re.finditer(r"^## (VO \d+[a-z]?)[^\n]*\n(.*?)(?=^## |\Z)",
                             text, re.S | re.M):
        name = match.group(1).lower().replace(" ", "")
        spoken = [
            re.sub(r"^>\s?", "", line).strip()
            for line in match.group(2).splitlines()
            if line.strip().startswith(">")
        ]
        # Blank quoted lines are paragraph breaks; they become the pauses.
        paragraphs, current = [], []
        for line in spoken:
            if line:
                current.append(line)
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        if paragraphs:
            out.append((name, "\n\n".join(paragraphs)))
    return out


def synthesise(text: str, key: str) -> bytes:
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}",
        data=json.dumps({"text": text, "model_id": MODEL,
                         "voice_settings": SETTINGS}).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"elevenlabs {exc.code}: {detail}") from exc


def duration(path: Path) -> float:
    import subprocess, shutil
    result = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="regenerate blocks that already exist")
    args = ap.parse_args()

    from telemetry.otel import load_env
    key = load_env().get("ELEVENLABS_API_KEY", "")
    if not key:
        print("no ELEVENLABS_API_KEY in .env.local", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    total_chars = total_seconds = 0
    for name, text in blocks():
        path = OUT / f"{name}.mp3"
        if path.exists() and not args.force:
            print(f"  {name:<5} {duration(path):>5.1f}s  (kept)")
            total_seconds += duration(path)
            continue
        # Written beside the target and moved into place only once the whole
        # request has come back. The previous version wrote directly, and a
        # regeneration that ran out of quota part-way through destroyed three
        # perfectly good takes it could no longer replace. A file that exists
        # is worth more than a file that is about to be better.
        staging = path.with_suffix(".part")
        staging.write_bytes(synthesise(text, key))
        staging.replace(path)
        words = len(text.split())
        seconds = duration(path)
        total_chars += len(text)
        total_seconds += seconds
        print(f"  {name:<5} {seconds:>5.1f}s  {words:>3} words  "
              f"{words / (seconds / 60):>3.0f} wpm")

    print(f"\n  total {total_seconds:.0f}s = "
          f"{int(total_seconds) // 60}:{int(total_seconds) % 60:02d}"
          f"   ({total_chars} characters generated)")
    if total_seconds > 175:
        print("  OVER BUDGET -- three minutes is a hard limit, and the video "
              "needs a little air at each end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
