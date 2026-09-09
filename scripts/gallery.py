"""Capture the control room, panel by panel, for the submission gallery.

    python scripts/gallery.py

Frames pulled out of the demo video are 1080p and soft; these are taken from
the running interface at 2x and clipped to the panel being shown, which is
what a reader zooming into a Devpost gallery actually wants.

Every image lands in `docs/gallery/` at 3:2 -- Devpost's stated ratio --
letterboxed onto the page's own background rather than cropped, because
cutting a control room to fit an aspect ratio means cutting a column off the
readiness matrix.

Needs the control room running locally with media present:

    python ui/server.py --port 8090
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.record import Take  # noqa: E402

OUT = ROOT / "docs" / "gallery"
LOCAL = "http://127.0.0.1:8090"
BG = (11, 11, 10)

# name, url, selector to frame (None = the whole viewport), and how long to
# let the page settle first. `wait` is the expression that has to become true
# before anything is measured -- the detail pane is one round trip per figure
# to Grafana Cloud, and measuring a panel below it while it is still a
# skeleton frames whatever was there a second ago.
DETAIL_READY = "!!document.getElementById('measured')"
SHOTS: list[tuple[str, str, str | None, str | None]] = [
    ("01-control-room",   "/#de-DE",   None,               DETAIL_READY),
    ("02-new-release",    "/#de-DE",   "#build",           None),
    ("03-readiness",      "/#de-DE",   "table.matrix",     None),
    ("04-verdict",        "/#de-DE",   "#detail .pad:nth-of-type(1)", DETAIL_READY),
    ("05-outputs",        "/#de-DE",   "#detail .outputs", DETAIL_READY),
    ("06-compliance",     "/#ja-JP",   "#detail .comp",    DETAIL_READY),
    ("07-lineage",        "/#de-DE",   "#detail .asset",   DETAIL_READY),
    ("08-blast-radius",   "/#de-DE",   "#detail .blast",   DETAIL_READY),
    ("09-proposal",       "/#pt-BR",   "#detail .prop",    DETAIL_READY),
    ("10-ledger",         "/#de-DE",   "#activity",        DETAIL_READY),
    ("11-autonomy",       "/#de-DE",   "#autonomy",        DETAIL_READY),
    # These two are their own page, centred, with nothing around them: a clip
    # grown to 3:2 reaches into blank margin, so they are taken whole.
    ("12-architecture",   "/diagram",  None,               None),
    ("13-alert-received", "/wakelog",  None,               None),
]

PAD = 26.0


async def region(take, selector: str) -> dict:
    """The page-coordinate box of every element matching `selector`.

    The union, not the first match: `.asset` is nine rows and framing one of
    them and padding outwards lands wherever the padding happens to reach.
    Converted to document coordinates because that is what `clip` wants, and
    a viewport rect is not one.
    """
    box = await take.js(
        "(() => { const els = [...document.querySelectorAll(%r)];"
        " if (!els.length) return null;"
        " const r = els.map(e => e.getBoundingClientRect());"
        " const x = Math.min(...r.map(b => b.left)) + scrollX;"
        " const y = Math.min(...r.map(b => b.top)) + scrollY;"
        " return [x, y, Math.max(...r.map(b => b.right)) + scrollX - x,"
        "         Math.max(...r.map(b => b.bottom)) + scrollY - y]; })()"
        % selector)
    if not box:
        raise RuntimeError(f"nothing matches {selector!r}")
    x, y, w, h = box
    x, y, w, h = max(0.0, x - PAD), max(0.0, y - PAD), w + PAD * 2, h + PAD * 2

    # Grow the box to 3:2 with real page content rather than padding it with
    # black afterwards. The New Release panel is nearly five times wider than
    # it is tall; letterboxed, it became a thin strip in an empty frame, which
    # is a worse picture of a good panel.
    page = await take.js(
        "[document.documentElement.scrollWidth,"
        " document.documentElement.scrollHeight]")
    pw, ph = float(page[0]), float(page[1])
    if w / h > 1.5:
        want = min(w / 1.5, ph)
        y = max(0.0, min(y - (want - h) / 2, ph - want))
        h = want
    else:
        want = min(h * 1.5, pw)
        x = max(0.0, min(x - (want - w) / 2, pw - want))
        w = want
    return {"x": x, "y": y, "width": w, "height": h, "scale": 2}


def three_by_two(path: Path, width: int = 1800) -> None:
    from PIL import Image

    im = Image.open(path).convert("RGB")
    height = round(width * 2 / 3)
    scaled = im.copy()
    scaled.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), BG)
    canvas.paste(scaled, ((width - scaled.width) // 2,
                          (height - scaled.height) // 2))
    canvas.save(path, quality=92, optimize=True)


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    async with Take("gallery", OUT, fps=1) as take:
        for name, path, selector, wait in SHOTS:
            await take.goto(LOCAL + path, settle=3)
            if wait:
                if not await take.wait_for(wait, timeout=180):
                    print(f"  {name:20} SKIPPED — the page never finished")
                    continue
            # Make the video elements paint. `preload="metadata"` loads the
            # duration and not a frame, so the packaged deliverable rendered
            # as a black rectangle -- which is exactly what the panel exists
            # to disprove.
            await take.js(
                "(() => { for (const v of document.querySelectorAll('video'))"
                "  { try { v.currentTime = 3; } catch (e) {} } })()")
            await asyncio.sleep(1.6)
            params = {"format": "png"}
            if selector:
                try:
                    params["clip"] = await region(take, selector)
                except RuntimeError as exc:
                    print(f"  {name:20} SKIPPED — {exc}")
                    continue
                params["captureBeyondViewport"] = True
            data = await take.cdp("Page.captureScreenshot", **params)
            target = OUT / f"{name}.png"
            target.write_bytes(base64.b64decode(data["data"]))
            three_by_two(target)
            print(f"  {name:20} {target.stat().st_size // 1024:>4} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
