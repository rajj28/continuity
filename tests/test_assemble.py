"""Placement decides what defect the pipeline produces, so it is tested apart
from the model that produces the audio.

Every case here is arithmetic on real PCM buffers -- no network, no API key.
The segments are silence of an exact length, because what is under test is
where the audio lands, not what it sounds like.
"""

from __future__ import annotations

import pytest

from media.dub.assemble import Policy, Stem, assemble, place
from media.dub.live_translate import DubSegment
from media.qc.ffmpeg import LIVE_OUTPUT_RATE


def segment(index: int, *, spoken_ms: float, slot_ms: float, cue_ms: float,
            level: int = 0) -> DubSegment:
    """A line of a known length, at a known cue. `level` fills the buffer with
    a constant sample so overlap is visible in the output bytes."""
    samples = int(round(spoken_ms / 1000 * LIVE_OUTPUT_RATE))
    pcm = level.to_bytes(2, "little", signed=True) * samples
    return DubSegment(
        index=index, pcm=pcm, reference_ms=slot_ms, reference_start_ms=cue_ms
    )


def read(stem: Stem, ms: float) -> int:
    i = int(round(ms / 1000 * LIVE_OUTPUT_RATE)) * 2
    return int.from_bytes(stem.pcm[i:i + 2], "little", signed=True)


# ---------------------------------------------------------------------------
# The measurement each line carries
# ---------------------------------------------------------------------------


def test_a_line_that_fits_has_no_overrun():
    s = segment(0, spoken_ms=1500, slot_ms=1800, cue_ms=500)
    assert s.duration_ms == pytest.approx(1500, abs=1)
    assert s.overrun_ms == pytest.approx(-300, abs=1)
    assert s.expansion == pytest.approx(1500 / 1800, abs=0.01)


def test_german_style_expansion_shows_up_as_overrun():
    """The classic localisation problem: German against English lands near
    1.2x, and a slot written for English does not have room for it."""
    s = segment(0, spoken_ms=2200, slot_ms=1800, cue_ms=500)
    assert s.overrun_ms == pytest.approx(400, abs=1)
    assert s.expansion == pytest.approx(1.22, abs=0.01)


# ---------------------------------------------------------------------------
# CUE: onsets are right, the damage is overlap
# ---------------------------------------------------------------------------


def test_cue_policy_keeps_every_onset_exact():
    """And therefore hides the defect from an onset-drift probe -- which is
    precisely why it is not the default."""
    segments = [
        segment(0, spoken_ms=2200, slot_ms=1800, cue_ms=500),
        segment(1, spoken_ms=1200, slot_ms=1500, cue_ms=3000),
    ]
    placements = place(segments, policy=Policy.CUE)
    assert [p.onset_drift_ms for p in placements] == [0.0, 0.0]
    assert [round(p.overrun_ms) for p in placements] == [400, -300]


def test_cue_policy_overlap_is_summed_not_averaged():
    """A console sums. Averaging would duck both lines and make a collision
    sound like a mix decision rather than a fault."""
    segments = [
        segment(0, spoken_ms=1000, slot_ms=800, cue_ms=0, level=1000),
        segment(1, spoken_ms=1000, slot_ms=800, cue_ms=500, level=1000),
    ]
    stem = assemble(segments, policy=Policy.CUE)
    assert read(stem, 250) == 1000      # first line alone
    assert read(stem, 700) == 2000      # both lines, summed
    assert read(stem, 1200) == 1000     # second line alone


def test_summing_saturates_rather_than_wrapping():
    """Wraparound would turn a loud overlap into a crack, and the probe would
    then be measuring an integer bug instead of a dubbing fault."""
    segments = [
        segment(0, spoken_ms=500, slot_ms=500, cue_ms=0, level=30000),
        segment(1, spoken_ms=500, slot_ms=500, cue_ms=0, level=30000),
    ]
    stem = assemble(segments, policy=Policy.CUE)
    assert read(stem, 250) == 32767


# ---------------------------------------------------------------------------
# SEQUENTIAL: overrun becomes accumulating lateness
# ---------------------------------------------------------------------------


def test_an_overrun_pushes_every_later_line_late():
    segments = [
        segment(0, spoken_ms=2200, slot_ms=1800, cue_ms=500),   # 400 over
        segment(1, spoken_ms=1200, slot_ms=1500, cue_ms=2500),  # fits
    ]
    placements = place(segments, policy=Policy.SEQUENTIAL)
    assert placements[0].onset_drift_ms == pytest.approx(0, abs=1)
    # first line runs to 2700, so the second cannot start at its 2500 cue
    assert placements[1].start_ms == pytest.approx(2700, abs=1)
    assert placements[1].onset_drift_ms == pytest.approx(200, abs=1)


def test_lateness_accumulates_down_the_scene():
    """Progressive, not uniform. That distinction is what tells the agent a
    stem shift cannot fix this."""
    segments = [
        segment(i, spoken_ms=1400, slot_ms=1000, cue_ms=i * 1000.0)
        for i in range(4)
    ]
    drifts = [p.onset_drift_ms for p in place(segments, policy=Policy.SEQUENTIAL)]
    assert drifts[0] == pytest.approx(0, abs=1)
    assert drifts == sorted(drifts), "drift must be monotonically increasing"
    assert drifts[-1] == pytest.approx(1200, abs=2)


def test_lines_that_fit_never_drift():
    """The negative control: sequential placement must not invent drift where
    the translation actually fits."""
    segments = [
        segment(i, spoken_ms=800, slot_ms=1000, cue_ms=i * 2000.0)
        for i in range(4)
    ]
    stem = assemble(segments, policy=Policy.SEQUENTIAL)
    assert stem.worst_drift_ms == pytest.approx(0, abs=1)
    assert stem.overrunning == []


def test_a_gap_is_respected_between_lines():
    segments = [
        segment(0, spoken_ms=1500, slot_ms=1000, cue_ms=0),
        segment(1, spoken_ms=500, slot_ms=1000, cue_ms=1000),
    ]
    placements = place(segments, policy=Policy.SEQUENTIAL, min_gap_ms=120)
    assert placements[1].start_ms == pytest.approx(1620, abs=1)


# ---------------------------------------------------------------------------
# The stem
# ---------------------------------------------------------------------------


def test_the_stem_names_the_lines_that_do_not_fit():
    """The repair targets. The agent acts on these, not on the whole stem."""
    segments = [
        segment(0, spoken_ms=900, slot_ms=1000, cue_ms=0),
        segment(1, spoken_ms=1600, slot_ms=1000, cue_ms=2000),
        segment(2, spoken_ms=1400, slot_ms=1000, cue_ms=4000),
    ]
    stem = assemble(segments, policy=Policy.SEQUENTIAL)
    assert [p.index for p in stem.overrunning] == [1, 2]
    assert stem.worst_overrun_ms == pytest.approx(600, abs=1)


def test_the_stem_is_padded_to_the_scene_length():
    """A dub stem shorter than its picture is a muxing fault waiting to
    happen, so the timeline is held open to the scene duration."""
    segments = [segment(0, spoken_ms=500, slot_ms=1000, cue_ms=0)]
    stem = assemble(segments, policy=Policy.SEQUENTIAL, total_ms=8000)
    assert stem.duration_ms == pytest.approx(8000, abs=1)


def test_placement_snaps_to_whole_samples():
    """An odd byte offset would shift the sign bit into the low byte and turn
    the stem into noise -- a spectacular QC failure with nothing to do with
    dubbing."""
    segments = [segment(0, spoken_ms=500, slot_ms=500, cue_ms=0.03125, level=999)]
    stem = assemble(segments, policy=Policy.CUE)
    assert len(stem.pcm) % 2 == 0
    assert read(stem, 200) == 999
