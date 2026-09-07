"""Acting: the strategies that actually change an asset.

Every repair here is deterministic signal processing. No model touches the
audio, and that is the point -- a repair has to be reproducible from its
parameters, or a verification that passes proves nothing about whether the
repair is what caused it.

Three strategies, each matched to a fault the probes can distinguish:

  RETIME   the voice enters late by a uniform amount. Shift the stem earlier.
           Fixes systematic drift; provably cannot fix progressive drift,
           because shifting moves the whole staircase.

  REMIX    the stem is at the wrong level. Normalise to the market's target
           loudness with a true-peak ceiling. Touches no timing at all.

  REWRITE  the lines are too long. This is the only strategy that needs a
           model, and it is not implemented here -- it lives in media/dub,
           re-synthesises, and produces a new asset rather than transforming
           an existing one. Named here so the Conductor's dispatch is total.

Every repair writes a NEW asset. Nothing is edited in place, ever. The version
that failed keeps its hash and its measurements, so "before" is a fact that
still exists on disk rather than a number someone remembers -- which is what
makes a verification honest and a rollback a lookup instead of a rebuild.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.contracts import RepairIntent, Strategy
from media.qc.types import ProbeError

log = logging.getLogger("continuity.repair")


class RepairError(ProbeError):
    pass


@dataclass
class RepairOutcome:
    """What a strategy produced. Not whether it worked -- that is verification's
    job, and a repair that graded its own homework would be worthless."""

    path: Path
    strategy: Strategy
    params: dict[str, Any]
    detail: dict[str, Any]


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise RepairError("ffmpeg not found on PATH")
    return found


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RepairError(f"ffmpeg failed: {detail[-400:]}")


def retime(source: Path, dest: Path, *, shift_ms: float,
           total_ms: float | None = None) -> RepairOutcome:
    """Shift the whole stem in time. Negative pulls it earlier.

    Implemented with `atrim`+`adelay` rather than `-ss`, because a negative
    shift means dropping samples from the head and a positive one means
    inserting silence, and one filter graph that does both keeps the two
    directions from diverging into separately-buggy code paths.

    The stem is held to its original length. A repair that changed the
    duration would break the mux it is supposed to fix, and the QC probe
    would then be measuring a different fault than the one we set out to
    repair.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if shift_ms < 0:
        # Pull earlier: discard the leading silence.
        graph = f"atrim=start={abs(shift_ms) / 1000:.6f},asetpts=PTS-STARTPTS"
    elif shift_ms > 0:
        graph = f"adelay={int(round(shift_ms))}:all=1"
    else:
        raise RepairError("a retime of 0 ms is not a repair")

    if total_ms is not None:
        # apad then atrim: pad first so a pulled-earlier stem regains its tail,
        # then cut to exactly the scene length.
        graph += f",apad,atrim=end={total_ms / 1000:.6f}"

    _run([_ffmpeg(), "-y", "-v", "error", "-i", str(source),
          "-af", graph, str(dest)])
    return RepairOutcome(
        path=dest, strategy=Strategy.RETIME,
        params={"shift_ms": shift_ms, "total_ms": total_ms},
        detail={"filter": graph, "tool": "ffmpeg"},
    )


def remix(source: Path, dest: Path, *, target_lufs: float,
          true_peak_max: float, lra: float = 11.0) -> RepairOutcome:
    """Normalise loudness to the market's target, with a true-peak ceiling.

    Single-pass `loudnorm`. Two-pass is more accurate, but the second pass
    consumes the first pass's measured values, and we already measure the
    result independently with the QC probe -- so a two-pass repair would be
    trusting ffmpeg's own analysis of its own output, which is exactly the kind
    of self-grading this system avoids elsewhere.

    Timing is untouched. That matters: loudness and sync are independent
    faults, and a repair that quietly changed both would make it impossible to
    say which one the verification confirmed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    graph = (f"loudnorm=I={target_lufs}:TP={true_peak_max}:LRA={lra}")
    _run([_ffmpeg(), "-y", "-v", "error", "-i", str(source),
          "-af", graph, "-ar", "24000", "-ac", "1", str(dest)])
    return RepairOutcome(
        path=dest, strategy=Strategy.REMIX,
        params={"target_lufs": target_lufs, "true_peak_max": true_peak_max},
        detail={"filter": graph, "tool": "ffmpeg", "passes": 1},
    )


def apply(intent: RepairIntent, source: Path, dest: Path,
          **context: Any) -> RepairOutcome:
    """Dispatch an intent to its strategy.

    Deliberately total over the strategies it claims to implement and loud
    about the ones it does not. An unimplemented strategy reaching here is a
    planner bug, and a silent no-op would present as a repair that ran and
    changed nothing -- which verification would report as `failed` and the
    autonomy ledger would count against the strategy rather than against the
    dispatch.
    """
    if intent.strategy is Strategy.RETIME:
        return retime(source, dest, shift_ms=float(intent.params["shift_ms"]),
                      total_ms=context.get("total_ms"))
    if intent.strategy is Strategy.REMIX:
        return remix(source, dest,
                     target_lufs=float(intent.params["target_lufs"]),
                     true_peak_max=float(intent.params["true_peak_max"]))
    raise RepairError(
        f"{intent.strategy.value} is not executable here. REWRITE re-synthesises "
        f"through media/dub and produces a new asset rather than transforming "
        f"one; anything else is a planner bug."
    )
