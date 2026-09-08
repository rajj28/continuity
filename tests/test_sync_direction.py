"""A repair that cannot tell early from late is a coin flip.

`delivery.dub_sync_offset_ms` is deliberately a MAGNITUDE: it is judged against
a tolerance, and a signed value under a `<= 120 ms` check would let a dub
running 400 ms early sail through. But the sign was discarded on the first line
of the measurement, so nothing downstream could recover it -- and RETIME, whose
whole job is to shift the stem one way or the other, had nothing to steer by.

On real Sintel audio, three consecutive runs:

    273 ms late   -> shift -153  -> 120 ms    right direction, by luck
    187 ms early  -> shift -188  -> 375 ms    further early, twice as wrong
    375 ms early  ->  ...

Every one of those was caught by verification and recorded `failed`, so nothing
shipped on a bad repair. That is the safety net working. It is not a substitute
for the loop converging.

The second failure below is subtler and was latent the whole time: four lines
drifting by ±200 ms have identical magnitudes, so `_is_systematic` called it a
uniform slip and the planner chose RETIME -- a strategy that shifts the entire
stem, for a fault where half the lines need moving the other way.
"""

from __future__ import annotations

from media.qc.sync import sync_offset
from media.qc.types import Interval

REFERENCE = [
    Interval(1000, 2000), Interval(3000, 4000),
    Interval(5000, 6000), Interval(7000, 8000),
]


def _shifted(by: float) -> list[Interval]:
    return [Interval(i.start_ms + by, i.end_ms + by) for i in REFERENCE]


def test_the_published_value_stays_an_unsigned_magnitude():
    """Both directions are equally far out of spec, and must measure the same.

    This is the property that makes a `<= tolerance` threshold meaningful, and
    the reason the fix could not simply be "make the measurement signed".
    """
    assert sync_offset(REFERENCE, _shifted(200)).value == 200
    assert sync_offset(REFERENCE, _shifted(-200)).value == 200


def test_a_late_dub_reports_a_positive_signed_drift():
    measurement = sync_offset(REFERENCE, _shifted(200))
    assert measurement.detail["mean_signed_onset_ms"] == 200.0
    assert measurement.detail["worst_signed_onset_ms"] == 200.0


def test_an_early_dub_reports_a_negative_signed_drift():
    """The case that broke it. Same magnitude, opposite repair."""
    measurement = sync_offset(REFERENCE, _shifted(-200))
    assert measurement.detail["mean_signed_onset_ms"] == -200.0
    assert measurement.detail["worst_signed_onset_ms"] == -200.0


def test_the_shift_that_undoes_the_drift_is_the_negated_signed_value():
    """What the planner actually computes, asserted end to end.

    Shifting by `-mean_signed_onset_ms` must land the dub back on the
    reference, in both directions. Using the magnitude instead doubles the
    error on an early stem, which is precisely what happened.
    """
    for offset in (200, -200, 37.5, -412.9):
        measured = sync_offset(REFERENCE, _shifted(offset))
        shift = -measured.detail["mean_signed_onset_ms"]
        corrected = sync_offset(REFERENCE, _shifted(offset + shift))
        assert corrected.value < 0.05, (
            f"a {offset} ms drift corrected by {shift} ms left "
            f"{corrected.value} ms"
        )


def test_lines_drifting_opposite_ways_are_not_systematic():
    """Equal magnitudes in opposite directions are not a uniform slip.

    `_is_systematic` compared the spread of ABSOLUTE drifts, so +200, -200,
    +200, -200 looked perfectly uniform and selected RETIME -- one shift for
    the whole stem, applied to a fault where half the lines need moving the
    other way. No amount of shifting fixes it; the lines have to be re-timed
    individually, which is REWRITE.
    """
    alternating = [
        Interval(1200, 2200), Interval(2800, 3800),
        Interval(5200, 6200), Interval(6800, 7800),
    ]
    measurement = sync_offset(REFERENCE, alternating)
    assert measurement.value == 200, "each line is still 200 ms out"
    assert measurement.detail["drift_is_systematic"] is False


def test_a_genuinely_uniform_slip_is_still_systematic():
    """The fix must not make RETIME unreachable for the case it is for."""
    assert sync_offset(REFERENCE, _shifted(200)).detail["drift_is_systematic"]
    assert sync_offset(REFERENCE, _shifted(-200)).detail["drift_is_systematic"]
