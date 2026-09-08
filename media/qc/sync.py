"""Dub synchronisation measurement.

Named `dub_sync_offset_ms`, not "lip-sync score", because it measures what it
actually measures: how far each dubbed utterance's boundaries drift from the
corresponding utterance in the source master. That is *isochrony* -- the thing
dubbing editors actually fit to.

True viseme-level lip-sync would require a phoneme aligner. We do not have one,
we do not claim one, and it is listed in docs/LIMITATIONS.md as future work.

The measurement is fully deterministic: same bytes in, same number out.
"""

from __future__ import annotations

import statistics
from pathlib import Path

from .ffmpeg import voiced_intervals
from .types import Interval, Measurement, ProbeError

METHOD = "ffmpeg_silencedetect_isochrony_v1"


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Stdlib-only and stable for tiny samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100.0 * len(ordered))))
    return ordered[rank - 1]


def _overlap_ms(a: Interval, b: Interval) -> float:
    return max(0.0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))


def _slot_for(span: Interval, reference: list[Interval]) -> int:
    """Which line this piece of speech belongs to.

    Three rules, in order, and the order is the whole point:

      1. the slot it overlaps most. A span sitting inside a cue belongs to
         that cue, whatever its onset happens to be nearest to.
      2. failing any overlap, the last cue that has already started. Speech
         that runs past its slot belongs to the line it came from, not to the
         one it spilled into.
      3. failing that, the first line -- speech before the first cue.

    Nearest-by-onset, the obvious rule, is wrong: the second half of "Terre de
    dragons, Sintel" sits squarely inside its own cue but starts closer to the
    NEXT cue than to its own, so nearest-onset moved it forward a line and
    reported 1192 ms of drift for audio that was in tolerance.
    """
    overlaps = [_overlap_ms(span, ref) for ref in reference]
    best = max(range(len(reference)), key=lambda i: overlaps[i])
    if overlaps[best] > 0:
        return best
    started = [i for i, ref in enumerate(reference) if ref.start_ms <= span.start_ms]
    return started[-1] if started else 0


def align(
    reference: list[Interval], measured: list[Interval]
) -> tuple[list[tuple[Interval, Interval]], str | None]:
    """Pair each reference utterance with the speech that belongs to it.

    Grouping by nearest reference rather than pairing by index, because a
    spoken line is not always one voiced span. The French stem for Sintel S03
    split "Terre de dragons, Sintel" across a 527 ms pause at the comma, and
    index pairing then shifted every later line onto the wrong slot and
    reported 6137 ms of drift for audio that was actually in tolerance.

    Grouping is robust to that: however many spans a line breaks into, they
    all land on the reference they are nearest to, and the line's onset is the
    first of them. It cannot misalign the remainder of a scene because of one
    breath.

    A reference with NO speech near it is still an anomaly and still reported.
    That is a real failure -- a line that was never spoken -- and it must not
    be smoothed away by the same mechanism that tolerates a pause.
    """
    if not reference:
        raise ProbeError("no utterances to align")

    groups: list[list[Interval]] = [[] for _ in reference]
    for span in measured:
        groups[_slot_for(span, reference)].append(span)

    pairs: list[tuple[Interval, Interval]] = []
    empty: list[int] = []
    for index, (ref, spans) in enumerate(zip(reference, groups)):
        if not spans:
            empty.append(index)
            continue
        # The line spans from its first sound to its last, pauses included --
        # which is what a viewer hears as the line.
        pairs.append((ref, Interval(spans[0].start_ms, spans[-1].end_ms)))

    anomaly: str | None = None
    if empty:
        anomaly = (
            f"no_speech_for_utterances: {empty} "
            f"(reference={len(reference)} measured={len(measured)})"
        )
    if not pairs:
        raise ProbeError("no utterances to align")
    return pairs, anomaly


def sync_offset(
    reference: list[Interval], measured: list[Interval]
) -> Measurement:
    """Worst-case absolute onset drift across the scene, in milliseconds.

    We report the max because a release blocks on its worst moment, not its
    average one. p95 and the per-utterance detail ride along so the agent can
    tell "one bad line" from "the whole scene has slipped" -- which selects a
    completely different repair strategy.

    ## The magnitude is the measurement; the sign is a separate fact

    The published value is deliberately unsigned. It is compared against a
    tolerance, and a signed value under a `<= 120` threshold would let a dub
    running 400 ms EARLY pass as comfortably within spec.

    But direction cannot simply be discarded, which is what this function used
    to do on its first line. RETIME shifts the stem, and a shift needs to know
    which way. Blind to that, the loop oscillated on real audio:

        273 ms late  -> shift -153  -> 120 ms   (right direction, by luck)
        187 ms EARLY -> shift -188  -> 375 ms   (further early; twice as wrong)

    Every repair reported itself FAILED and the verification caught each one,
    so nothing shipped -- but a repair strategy that cannot tell early from
    late is a coin flip, and three of those in a row is not a control loop.

    So: magnitude for the threshold, signed mean for the repair. Positive means
    the dub comes in LATE and needs pulling earlier.
    """
    pairs, anomaly = align(reference, measured)

    signed = [m.start_ms - r.start_ms for r, m in pairs]
    onsets = [abs(x) for x in signed]
    overhangs = [m.end_ms - r.end_ms for r, m in pairs]

    worst_idx = onsets.index(max(onsets))

    return Measurement(
        key="delivery.dub_sync_offset_ms",
        value=round(max(onsets), 1),
        unit="ms",
        method=METHOD,
        detail={
            "utterances": len(pairs),
            "p95_onset_ms": round(_percentile(onsets, 95), 1),
            "mean_onset_ms": round(statistics.fmean(onsets), 1),
            # Signed, and the only thing here that says which way to shift.
            "mean_signed_onset_ms": round(statistics.fmean(signed), 1),
            "worst_signed_onset_ms": round(signed[worst_idx], 1),
            "max_end_overhang_ms": round(max(overhangs, key=abs), 1),
            "worst_utterance_index": worst_idx,
            "worst_utterance_ref": repr(pairs[worst_idx][0]),
            "worst_utterance_dub": repr(pairs[worst_idx][1]),
            "drift_is_systematic": _is_systematic(signed),
            "anomaly": anomaly,
        },
    )


def _is_systematic(signed: list[float]) -> bool:
    """True when every utterance drifts by a similar amount.

    Systematic drift means the whole stem is offset -- a muxing or padding fault,
    fixable by shifting the track. Scattered drift means individual lines do not
    fit their slots, which is a script-length problem no amount of shifting will
    solve. The agent branches on exactly this.
    """
    onsets = [abs(x) for x in signed]
    if len(signed) < 3:
        return False
    # Spread of the SIGNED drifts against the mean MAGNITUDE: lines slipping
    # the same way by a similar amount is systematic; lines slipping opposite
    # ways is not, however equal their magnitudes.
    spread = statistics.pstdev(signed)
    mean = statistics.fmean(onsets)
    return spread < 0.25 * mean if mean else False


def speech_rate_wpm(measured: list[Interval], word_count: int) -> Measurement:
    """Words per minute over voiced time only.

    Silence is excluded deliberately: a line delivered fast with long pauses
    around it is still delivered fast, and that is what makes it sound rushed.
    This is the signal that reveals why naive retiming fails on German.
    """
    voiced_ms = sum(i.duration_ms for i in measured)
    if voiced_ms <= 0:
        raise ProbeError("no voiced audio; cannot compute speech rate")

    wpm = word_count / (voiced_ms / 60_000.0)
    return Measurement(
        key="quality.speech_rate_wpm",
        value=round(wpm, 1),
        unit="wpm",
        method=METHOD,
        detail={
            "word_count": word_count,
            "voiced_ms": round(voiced_ms, 1),
            "utterances": len(measured),
        },
    )


def line_overrun(
    reference: list[Interval], measured: list[Interval]
) -> Measurement:
    """How far the worst line runs PAST the end of its slot, in milliseconds.

    A separate failure from onset drift, and the reason this probe exists:
    running the real pipeline on Sintel S03 produced a stem whose onsets were
    all exactly right -- the gaps between cues were long enough to absorb the
    overrun -- while two of four German lines were still being spoken after
    the picture had moved on. `sync_offset` reported 0 ms and was correct;
    it simply does not measure this.

    Onset drift says the stem is MISTIMED, which a shift can fix. Overrun says
    the lines are TOO LONG, which only a shorter line can fix. Measuring both
    is what lets the agent tell the symptom from the cause instead of
    inferring one from the shape of the other.

    Negative means every line finishes inside its slot. Reported rather than
    clamped, because "how much room was left" is useful to an adaptor.
    """
    pairs, anomaly = align(reference, measured)
    overhangs = [m.end_ms - r.end_ms for r, m in pairs]
    worst = max(overhangs)
    worst_idx = overhangs.index(worst)

    return Measurement(
        key="delivery.line_overrun_ms",
        value=round(worst, 1),
        unit="ms",
        method=METHOD,
        detail={
            "utterances": len(pairs),
            "overrunning_lines": sum(1 for o in overhangs if o > 0),
            "worst_line_index": worst_idx,
            "worst_line_ref": repr(pairs[worst_idx][0]),
            "worst_line_dub": repr(pairs[worst_idx][1]),
            "per_line_overrun_ms": [round(o, 1) for o in overhangs],
            "anomaly": anomaly,
        },
    )


def measure_dub(
    dub_path: Path,
    reference: list[Interval],
    word_count: int,
    noise_db: float = -35.0,
) -> list[Measurement]:
    """Full sync probe for one dubbed stem. Returns every measurement it made."""
    measured = voiced_intervals(dub_path, noise_db=noise_db)
    return [
        sync_offset(reference, measured),
        line_overrun(reference, measured),
        speech_rate_wpm(measured, word_count),
    ]
