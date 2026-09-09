"""Capture the one shot that cannot be done headlessly: a real terminal.

    python scripts/terminal_clip.py

Everything else in the video is a browser and comes out of `record.py` without
touching the screen. This is Grafana's alert arriving at the Cloud Run
receiver, in a console, and there is no honest way to show that without
filming an actual console.

## Only this window, ever

Captured by window handle, not by filming the screen. Desktop capture is far
more reliable and it was tried -- and it recorded the editor that happened to
be in front of the console, including the conversation on screen in it. For a
video that gets published, "whatever was visually on top" is not an acceptable
definition of the subject.

So gdigrab attaches to the window by title. It cannot pick up anything else,
whatever is stacked over it.

That reliability had to be earned back. Window capture dies with "Failed to
capture image (error 8)" the moment the console resizes itself, so the window
is polled for until it exists and then left alone until `mode con` has
finished with it. And the console is launched through `conhost` explicitly:
Windows 11 routes `cmd` into Windows Terminal, which gdigrab cannot read at
all, and `wt` rejected the command line outright.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TITLE = "ContinuityDemo"
SCRIPT = ROOT / "scripts" / "terminal_shot.cmd"
OUT = ROOT / "out" / "clips" / "terminal.mp4"

# Win32, because a window's position is not something PowerShell exposes and
# the crop has to be exact or the shot has a strip of desktop down one side.
_RECT = """
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L, T, Rt, B; }
}
'@
$p = Get-Process | Where-Object { $_.MainWindowTitle -eq '%s' } | Select-Object -First 1
if ($p) { $r = New-Object W+R; [void][W]::GetWindowRect($p.MainWindowHandle, [ref]$r);
          Write-Output "$($r.L) $($r.T) $($r.Rt) $($r.B)" }
"""


_FOREGROUND = """
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class F {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}
'@
$p = Get-Process | Where-Object { $_.MainWindowTitle -eq '%s' } | Select-Object -First 1
if ($p) { [void][F]::ShowWindow($p.MainWindowHandle, 9);
          [void][F]::SetForegroundWindow($p.MainWindowHandle) }
"""


def _rect(title: str):
    """The window's position on screen, or None if it is not there."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _RECT % title],
        capture_output=True, text=True)
    parts = result.stdout.split()
    if len(parts) != 4:
        return None
    left, top, right, bottom = (int(v) for v in parts)
    return left, top, right - left, bottom - top


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print("launching the terminal...")
    # conhost, explicitly. `wt` refused the command line and opened an error
    # tab; the legacy host takes it and, more usefully, honours the `title`
    # the script sets -- which is how this is found on screen. Its renderer
    # no longer matters now that the desktop is what gets captured.
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Start-Process conhost -ArgumentList 'cmd','/c','{SCRIPT}'"],
        check=True, capture_output=True)

    rect = None
    deadline = time.time() + 25
    while time.time() < deadline:
        rect = _rect(TITLE)
        if rect:
            break
        time.sleep(0.4)
    if not rect:
        print(f"no window titled {TITLE} appeared", file=sys.stderr)
        return 1

    # Bring it to the front. gdigrab copies from the window's device context
    # and an occluded window gives it nothing -- "Failed to capture image
    # (error 8)", which reads like a resize problem and is really a visibility
    # one. Only the console is raised; nothing else on the desktop is touched,
    # and nothing else can end up in the frame because the capture is bound to
    # this window.
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", _FOREGROUND % TITLE],
        capture_output=True, text=True)
    # Let it finish sizing itself before the rectangle is trusted.
    time.sleep(2.5)
    rect = _rect(TITLE) or rect
    x, y, w, h = rect
    # Even dimensions: yuv420p cannot represent an odd-sized frame.
    w, h = w - (w % 2), h - (h % 2)
    print(f"  window at {x},{y}  {w}x{h}")
    print("capturing...")
    result = subprocess.run([
        "ffmpeg", "-y", "-f", "gdigrab", "-framerate", "12",
        "-i", f"title={TITLE}", "-t", "16",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        # Fitted inside the project's frame and padded rather than stretched:
        # a console is not 16:9, and distorting the text to make it fit would
        # look worse than letterboxing it.
        "-vf", ("scale=1920:1080:force_original_aspect_ratio=decrease:"
                "flags=lanczos,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#0b0b0a"),
        str(OUT),
    ], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip().splitlines()[-3:], file=sys.stderr)
        return 1

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(OUT)],
        capture_output=True, text=True).stdout.strip()
    print(f"  {OUT}  {float(probe or 0):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
