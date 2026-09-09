"""Run the real pipeline from the control room, and narrate it while it runs.

    master.mp4 + dialogue.srt + a list of markets
      -> stage 1 ingest
      -> per market: dub, subtitles, audio description, storefront record
      -> release check
      -> per market: the packaged deliverable

That is the same sequence the README documents and the same one a person runs
by hand. It is run here by starting those scripts as processes, not by
reimplementing them: a second implementation of the pipeline that existed only
to make the screen move would be a demo of itself. If `scripts/stage2.py`
changes, this changes with it, and if it fails, this reports the failure it
actually got rather than a friendlier one.

## Why the output is streamed line by line

Each stage prints what it did as it does it -- the hash it ingested, the line
it adapted, the loudness it measured. Those lines are the evidence that the
work happened, and they are worth more live than summarised at the end, when
the only honest thing left to say is "it worked". A build takes minutes; an
operator should be able to watch which minute is being spent on what.

## Why only one at a time

Every stage writes to one content-addressed store and one manifest. Two builds
at once would interleave versions of the same asset, and the loser would be
discovered later as a hash that nothing explains. A second request is refused
while one is running.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
UPLOADS = ROOT / "out" / "uploads"

# The master and dialogue list the project ships with, offered so that showing
# the pipeline does not require pushing 224 MB through a browser first.
STOCK_MASTER = ROOT / "assets" / "sintel" / "master_v1.mp4"
STOCK_DIALOGUE = ROOT / "assets" / "sintel" / "sintel_en.srt"

# A stage's timeout is generous because a stage that is slow is not a stage
# that has hung: stage 2 speaks every line of dialogue through a model, and how
# long that takes depends on a service we do not control. What we refuse to do
# is wait forever with nothing on the screen.
STAGE_TIMEOUT = 900


@dataclass(frozen=True)
class Stage:
    """One step of the pipeline, and what it is for in a sentence."""

    key: str
    label: str
    detail: str
    script: str
    per_market: bool


PLAN: tuple[Stage, ...] = (
    Stage("ingest", "Ingest", "cut the master into scenes on dialogue gaps",
          "stage1.py", per_market=False),
    Stage("dub", "Dub", "adapt each line into the market's language and speak it",
          "stage2.py", per_market=True),
    Stage("subtitles", "Subtitles", "timed text from the adapted lines",
          "stage3.py", per_market=True),
    Stage("description", "Audio description",
          "narration written into the gaps between lines",
          "stage4.py", per_market=True),
    Stage("storefront", "Storefront", "localised metadata and forced narratives",
          "stage5.py", per_market=True),
    Stage("check", "Release check", "technical, rights and deliverables",
          "release_check.py", per_market=False),
    Stage("package", "Package",
          "one file carrying picture, dub, description and subtitles",
          "stage6.py", per_market=True),
)

# Lines worth pulling out of the noise. A stage prints a lot; these are the
# ones that carry a number a release is judged on, so they are marked and the
# screen can show them differently from the rest.
NOTABLE = re.compile(
    r"\d\s*(ms|s|lufs|dbtp|wpm|cps|kb|mb)\b"
    r"|\b(sync|overrun|collision|loudness|peak|wrote|packaged|dropped|"
    r"scenes?|cues?|master|dialogue|ready|blocked|dub|subtitle)\b", re.I)

# Two lines the Google SDK prints for every single model call: the URL it
# posted to, and a note about automatic function calling. Neither says
# anything about this system's work, and at four or five per line of dialogue
# they bury the lines that do. Nothing that reports a measurement, a decision
# or a failure is filtered -- only these two, by exact shape.
NOISE = re.compile(r"INFO\s+HTTP Request: POST |INFO\s+AFC is enabled with ")


def _argv(stage: Stage, market: str, master: Path, dialogue: Path,
          scene: str) -> list[str]:
    """The command line for one stage, in that stage's own vocabulary.

    Written out per script rather than assembled from flags they are assumed
    to share, because they do not share them: the release check takes no
    market at all -- a frame rate belongs to a master, not to Germany -- and
    is pointed at the cut scene instead. Guessing here would produce a stage
    that fails on an unrecognised argument, which reads on the screen as the
    pipeline being broken.
    """
    argv = [sys.executable, "-u", str(ROOT / "scripts" / stage.script)]
    if stage.script == "stage1.py":
        # Scoped to one scene here too. Ingest without it cuts every scene in
        # the master, which for a feature is many minutes of re-encoding
        # before the first thing an operator can look at appears.
        return argv + ["--master", str(master), "--dialogue", str(dialogue),
                       "--scene", scene]
    if stage.script == "release_check.py":
        return argv + ["--master", str(ROOT / "out" / "scenes" / (scene + ".mp4"))]
    return argv + ["--scene", scene, "--market", market]


class Busy(RuntimeError):
    """A build is already running."""


class Release:
    """One build at a time, streamed as it happens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running: dict[str, Any] | None = None

    # -- inputs ------------------------------------------------------------

    def sources(self) -> dict[str, Any]:
        """What can be built from right now: the stock master and any upload.

        Uploads are listed with their size so a person can tell which of two
        files with similar names is the one they just dropped.
        """
        def describe(path: Path, stock: bool) -> dict[str, Any]:
            return {"name": path.name, "path": path.relative_to(ROOT).as_posix(),
                    "bytes": path.stat().st_size, "stock": stock}

        masters, dialogues = [], []
        if STOCK_MASTER.exists():
            masters.append(describe(STOCK_MASTER, True))
        if STOCK_DIALOGUE.exists():
            dialogues.append(describe(STOCK_DIALOGUE, True))
        for path in (sorted(UPLOADS.glob("*")) if UPLOADS.exists() else []):
            if not path.is_file() or path.suffix == ".part":
                continue
            bucket = dialogues if path.suffix.lower() == ".srt" else masters
            bucket.append(describe(path, False))
        return {"masters": masters, "dialogues": dialogues,
                "busy": self.running is not None}

    def resolve(self, given: str, *, subtitle: bool) -> Path:
        """A path from the browser, checked to be one we offered.

        Compared against the resolved candidates rather than pattern-matched,
        because a build runs ffmpeg over whatever this returns and "does not
        contain .." is not the same guarantee as "is one of these files".
        """
        allowed = {
            Path(s["path"]): ROOT / s["path"]
            for s in self.sources()["dialogues" if subtitle else "masters"]
        }
        chosen = allowed.get(Path(given))
        if chosen is None or not chosen.exists():
            raise ValueError("not an available "
                             + ("dialogue list" if subtitle else "master"))
        return chosen

    # -- the build ---------------------------------------------------------

    def steps(self, markets: list[str]) -> list[dict[str, Any]]:
        """The whole plan, before any of it has run.

        Sent first so the screen shows what is coming and how far through it
        is, rather than a list that grows and gives no sense of the end.
        """
        out = []
        for stage in PLAN:
            for market in (markets if stage.per_market else [""]):
                out.append({"id": stage.key + ":" + market, "key": stage.key,
                            "label": stage.label, "detail": stage.detail,
                            "market": market})
        return out

    def build(self, master: Path, dialogue: Path, markets: list[str],
              scene: str, sink: Callable[[dict], None]) -> None:
        if not self._lock.acquire(blocking=False):
            raise Busy("a build is already running")
        started = time.time()
        try:
            self.running = {"markets": markets, "at": started}
            self._run(master, dialogue, markets, scene, sink)
        finally:
            self.running = None
            self._lock.release()
            sink({"type": "done", "seconds": round(time.time() - started, 1)})

    def _run(self, master: Path, dialogue: Path, markets: list[str],
             scene: str, sink: Callable[[dict], None]) -> None:
        sink({"type": "plan", "steps": self.steps(markets),
              "master": master.name, "dialogue": dialogue.name,
              "markets": markets})

        for stage in PLAN:
            for market in (markets if stage.per_market else [""]):
                argv = _argv(stage, market, master, dialogue, scene)
                step = stage.key + ":" + market
                sink({"type": "step", "id": step, "state": "running"})
                began = time.time()
                code, tail = self._stream(argv, step, sink)
                took = round(time.time() - began, 1)
                if code == 0:
                    sink({"type": "step", "id": step, "state": "done",
                          "seconds": took})
                    continue
                # Stop at the first failure. The stages after it read what it
                # was meant to write, so running them would produce a second,
                # less informative failure -- and possibly a package built
                # from a stage that did not finish.
                sink({"type": "step", "id": step, "state": "failed",
                      "seconds": took, "detail": tail})
                sink({"type": "halted", "step": step, "detail": tail})
                return

    def _stream(self, argv: list[str], step: str,
                sink: Callable[[dict], None]) -> tuple[int, str]:
        """Run one stage, forwarding its output line by line.

        stderr is folded into stdout because the stages print progress to one
        and warnings to the other, and an operator watching a build wants them
        interleaved in the order they happened, not sorted by stream.
        """
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
        )
        recent: list[str] = []
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                recent.append(line)
                del recent[:-12]
                if NOISE.search(line):
                    continue
                sink({"type": "log", "id": step, "line": line[:300],
                      "notable": bool(NOTABLE.search(line))})
            code = proc.wait(timeout=STAGE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            recent.append("timed out after " + str(STAGE_TIMEOUT) + "s")
            code = 124
        return code, "\n".join(recent[-6:])


def save_upload(name: str, body: Iterable[bytes]) -> dict[str, Any]:
    """Write an uploaded file, under a name of our choosing.

    The browser's filename is used only for its stem and its suffix, both
    stripped to characters that cannot mean anything to a shell or a path. A
    file arriving from the network never gets to decide where it lands.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).stem)[:60] or "upload"
    suffix = Path(name).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".srt"}:
        raise ValueError("expected a video (.mp4, .mov, .mkv) or an .srt")

    UPLOADS.mkdir(parents=True, exist_ok=True)
    target = UPLOADS / (stem + suffix)
    # Staged, then moved. A half-written master left where the picker looks
    # would be offered as buildable and fail three stages later with an ffmpeg
    # error that says nothing about the upload having been cut off.
    part = target.with_suffix(target.suffix + ".part")
    total = 0
    with part.open("wb") as handle:
        for chunk in body:
            handle.write(chunk)
            total += len(chunk)
    part.replace(target)
    return {"name": target.name, "path": target.relative_to(ROOT).as_posix(),
            "bytes": total, "subtitle": suffix == ".srt"}
