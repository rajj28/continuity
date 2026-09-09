"""Drive the control room headlessly and capture it as video.

    python scripts/record.py --clip board --out out/clips

Chrome runs headless, so this never touches the screen and nothing has to be
left undisturbed while it works. It also means a clip can be re-shot as many
times as it takes, which is the real reason to do it this way: a hand-recorded
take that goes wrong costs a retake of everything after it.

## Animation is driven, not observed

Scrolling is done a few pixels at a time with a frame captured after each step,
rather than triggering a smooth-scroll and hoping the capture loop samples it
evenly. The motion is therefore exactly as smooth as the frame rate, every
time, and identical between takes.

Time-based sequences -- an agent reasoning, where the interesting thing is that
it takes a while -- are captured on a wall clock instead, because there the
duration IS the evidence. Speeding those up would be a lie about how fast the
system is.

## Frames, not a screencast

`Page.startScreencast` pushes frames as the compositor produces them, which
means irregular intervals and a resampling step. Capturing on demand costs a
little more per frame and produces an exact, evenly spaced sequence that ffmpeg
can turn into video without guessing.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
PROFILE = ROOT / "out" / "chrome-profile"


class Take:
    """One clip: a headless browser, a frame directory, and a frame counter."""

    def __init__(self, name: str, out: Path, *, fps: int = 12,
                 width: int = 1920, height: int = 1080, port: int = 9444,
                 profile: Path | None = None) -> None:
        self.name = name
        self.fps = fps
        self.width, self.height = width, height
        self.port = port
        self.profile = profile
        self.frames = out / name
        self.out = out
        self.n = 0
        self._chrome: subprocess.Popen | None = None
        self._scratch: Path | None = None
        self._ws = None
        self._id = 0

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "Take":
        import websockets

        if self.frames.exists():
            shutil.rmtree(self.frames)
        self.frames.mkdir(parents=True)

        args = [
            str(CHROME), "--headless=new", "--disable-gpu",
            "--hide-scrollbars", "--mute-audio",
            f"--remote-debugging-port={self.port}",
            f"--window-size={self.width},{self.height}",
            # Deterministic text: without this, font hinting can differ
            # between runs and two takes of the same clip will not match.
            "--force-device-scale-factor=1",
            "--font-render-hinting=none",
            "about:blank",
        ]
        # A profile carries the cookies for anything that needs a real login,
        # so the browser arrives already signed in rather than filming a login
        # form. Clips that need no session get a FRESH throwaway directory
        # each time: Chrome locks a profile while it runs and exits with code
        # 21 if it finds one already held, so a shared scratch profile turns
        # any stray browser -- including one left over from a previous take --
        # into a launch failure that looks like a debugging-port problem.
        self._scratch: Path | None = None
        if self.profile is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="continuity-take-"))
        args.insert(-1, f"--user-data-dir={self.profile or self._scratch}")

        self._chrome = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        target = await self._wait_for_target()
        self._ws = await websockets.connect(target, max_size=64 * 1024 * 1024)
        await self.cdp("Page.enable")
        await self.cdp("Runtime.enable")
        await self.cdp("Emulation.setDeviceMetricsOverride", width=self.width,
                       height=self.height, deviceScaleFactor=1, mobile=False)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._chrome is not None:
            self._chrome.terminate()
            try:
                self._chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._chrome.kill()
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)

    async def _wait_for_target(self, timeout: float = 40.0) -> str:
        import urllib.request

        # A no-proxy opener, deliberately. urllib honours HTTP_PROXY from the
        # environment, so a machine configured with one sends a request for
        # 127.0.0.1 out to the proxy, which cannot answer it -- and the whole
        # launch then fails with "chrome did not expose a debugging target"
        # while chrome is sitting there perfectly happy. curl was reaching it
        # the entire time.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last = "no response"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._chrome is not None and self._chrome.poll() is not None:
                raise RuntimeError(
                    f"chrome exited immediately with code "
                    f"{self._chrome.returncode}")
            try:
                with opener.open(
                        f"http://127.0.0.1:{self.port}/json", timeout=3) as fh:
                    targets = json.load(fh)
                for page in targets:
                    if page.get("type") == "page":
                        return page["webSocketDebuggerUrl"]
                last = f"{len(targets)} target(s), none of type page"
            except Exception as exc:                           # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.4)
        raise RuntimeError(
            f"chrome did not expose a debugging target on port {self.port} "
            f"after {timeout:.0f}s -- last attempt: {last}")

    # -- protocol ----------------------------------------------------------

    async def cdp(self, method: str, **params):
        self._id += 1
        await self._ws.send(json.dumps(
            {"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def js(self, expression: str):
        """Evaluate in the page and return the value."""
        result = await self.cdp("Runtime.evaluate", expression=expression,
                                awaitPromise=True, returnByValue=True)
        return (result.get("result") or {}).get("value")

    async def goto(self, url: str, settle: float = 3.0) -> None:
        await self.cdp("Page.navigate", url=url)
        await asyncio.sleep(settle)

    async def wait_for(self, expression: str, timeout: float = 30.0) -> bool:
        """Poll until a page expression is truthy. Better than sleeping."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self.js(expression):
                return True
            await asyncio.sleep(0.25)
        return False

    # -- capture -----------------------------------------------------------

    async def _frame(self) -> None:
        shot = await self.cdp("Page.captureScreenshot", format="png",
                              captureBeyondViewport=False)
        (self.frames / f"{self.n:05d}.png").write_bytes(
            base64.b64decode(shot["data"]))
        self.n += 1

    async def hold(self, seconds: float) -> None:
        """Sit still and let the viewer read. Also what covers a narration
        line that has more words than the screen has movement."""
        for _ in range(max(1, round(seconds * self.fps))):
            await self._frame()

    async def watch(self, seconds: float) -> float:
        """Capture on a wall clock, for things that happen at their own pace.

        Used for the agent stream, where the elapsed time IS the evidence:
        speeding it up would misrepresent how long the system takes.

        And it silently did. A screenshot over the debugging protocol costs
        more than one frame period at 12fps, so the naive loop produced fewer
        frames than the duration called for -- and ffmpeg, told only a frame
        rate, played them back as a shorter clip. Sixteen seconds of an agent
        reasoning became eleven, with nothing in the output to say so.

        The last frame is therefore repeated until the frame count matches the
        elapsed wall time. Playback is then real-time whatever the capture
        manages, and a slow machine costs smoothness rather than honesty.
        """
        # Relative to where this watch STARTED, not to the clip. `self.n` is
        # cumulative, so measuring the target against it padded short by
        # exactly the number of frames already shot -- thirteen seconds of a
        # forty-six second take, silently.
        start = time.time()
        base = self.n
        while time.time() - start < seconds:
            await self._frame()
            wanted = base + round((time.time() - start) * self.fps)
            while self.n < wanted:
                shutil.copyfile(self.frames / f"{self.n - 1:05d}.png",
                                self.frames / f"{self.n:05d}.png")
                self.n += 1
        captured = time.time() - start
        return captured

    async def glide(self, to_y: int, seconds: float,
                    ease: bool = True) -> None:
        """Scroll to a position over a duration, one frame per step.

        Eased, because a scroll that starts and stops at full speed reads as a
        jump cut. The easing is done here rather than by CSS smooth-scroll so
        that the position is known at every captured frame.
        """
        start = await self.js("window.scrollY") or 0
        steps = max(1, round(seconds * self.fps))
        for i in range(1, steps + 1):
            t = i / steps
            if ease:                       # cosine ease-in-out
                import math
                t = (1 - math.cos(math.pi * t)) / 2
            await self.js(f"window.scrollTo(0, {start + (to_y - start) * t})")
            await self._frame()

    # -- output ------------------------------------------------------------

    def assemble(self) -> Path:
        dest = self.out / f"{self.name}.mp4"
        subprocess.run([
            shutil.which("ffmpeg") or "ffmpeg", "-y",
            "-framerate", str(self.fps), "-i", str(self.frames / "%05d.png"),
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            # yuv420p or half the world's players show a black rectangle.
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(dest),
        ], check=True, capture_output=True)
        return dest


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

CONTROL = "https://continuity-control-z6txmgck2a-el.a.run.app"


async def clip_test(out: Path, token: str) -> Path:
    """The proof-of-quality clip: the board, the verdict, a live investigation."""
    async with Take("test", out, fps=12) as take:
        await take.goto(CONTROL, settle=2)
        # The board arrives over HTTP; wait for it rather than guessing.
        await take.wait_for("document.querySelectorAll('#rows .row').length > 0")
        # The token has to be in place before Investigate is pressed. Set here
        # rather than typed on camera -- filming a credential being entered is
        # both slow and a bad habit to record.
        await take.js(f"sessionStorage.setItem('op', {token!r})")
        await asyncio.sleep(1.5)

        await take.hold(2.0)                       # the board, at rest
        await take.glide(560, 3.5)                 # down through the matrix
        await take.hold(1.5)
        await take.glide(0, 2.0)                   # back to the top

        # Open a blocked market. Clicked through the DOM rather than by
        # coordinates so it cannot drift when the layout changes.
        await take.js(
            "document.querySelector('#rows .row[data-m=\"ja-JP\"]').click()")
        await take.wait_for("!!document.getElementById('investigate')")
        await take.hold(2.5)                       # the verdict panel

        await take.js("document.getElementById('investigate').click()")
        # Long enough for the specialists to reach a conclusion, which is the
        # whole point of the shot. They take about forty seconds.
        await take.watch(46.0)
        await take.hold(1.5)
        return take.assemble()


CLIPS = {"test": clip_test}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", default="test", choices=sorted(CLIPS))
    ap.add_argument("--out", default="out/clips")
    args = ap.parse_args()

    from telemetry.otel import load_env
    token = load_env().get("DEMO_TOKEN", "")
    if not token:
        print("no DEMO_TOKEN in .env.local", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = asyncio.run(CLIPS[args.clip](out, token))
    size = path.stat().st_size / 1e6
    duration = subprocess.run([
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
    ], capture_output=True, text=True).stdout.strip()
    print(f"\n{path}  {size:.1f} MB  {float(duration or 0):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
