"""Repairs, measured on real audio rather than asserted.

Every case here runs ffmpeg and then runs the QC probes over the result. A test
that checked the filter string would prove the repair was *requested*; only
measuring the output proves it happened.

The fixtures are the generated tones from scripts/make_fixtures.sh, whose
utterance boundaries are known to the millisecond, so a shift of 40 ms is
checkable as a shift of 40 ms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.contracts import (
    AutonomyTier,
    Direction,
    Evidence,
    Prediction,
    RepairIntent,
    Strategy,
)
from agents.repair import RepairError, apply, remix, retime
from media.qc.ffmpeg import duration_ms
from media.qc.loudness import measure_loudness
from media.qc.sync import measure_dub
from media.qc.types import Interval

FIXTURES = Path(__file__).resolve().parents[1] / "media" / "fixtures"
REF = FIXTURES / "ref_scene14.wav"
LATE = FIXTURES / "dub_fr_ok.wav"       # uniformly ~40 ms late

# The reference utterances make_fixtures.sh generates, in ms.
REFERENCE = [Interval(500, 2000), Interval(3000, 5000), Interval(6500, 9000)]
WORDS = 12


def sync_of(path: Path) -> float:
    return next(
        m.value for m in measure_dub(path, REFERENCE, WORDS)
        if m.key == "delivery.dub_sync_offset_ms"
    )


def loudness_of(path: Path) -> tuple[float, float]:
    ms = {m.key: m.value for m in measure_loudness(path)}
    return ms["delivery.audio_loudness_lufs"], ms["delivery.audio_true_peak_dbtp"]


# ---------------------------------------------------------------------------
# RETIME
# ---------------------------------------------------------------------------


def test_pulling_a_late_stem_earlier_reduces_measured_drift(tmp_path):
    """The whole RETIME hypothesis, checked against a probe rather than a
    filter string."""
    before = sync_of(LATE)
    assert before > 30, "fixture should start out late"

    out = retime(LATE, tmp_path / "fixed.wav", shift_ms=-before)
    after = sync_of(out.path)

    assert after < before
    assert after < 15


def test_a_shift_does_not_change_loudness(tmp_path):
    """Independence is what makes verification attributable. If RETIME moved
    the level too, a market that recovered after both repairs could not be
    attributed to either."""
    lufs_before, tp_before = loudness_of(LATE)
    out = retime(LATE, tmp_path / "shifted.wav", shift_ms=-40)
    lufs_after, tp_after = loudness_of(out.path)

    assert lufs_after == pytest.approx(lufs_before, abs=0.15)
    assert tp_after == pytest.approx(tp_before, abs=0.15)


def test_the_stem_keeps_its_length(tmp_path):
    """A repair that changed the duration would break the mux it exists to fix,
    and the probe would then be measuring a different fault."""
    original = duration_ms(LATE)
    out = retime(LATE, tmp_path / "held.wav", shift_ms=-300, total_ms=original)
    assert duration_ms(out.path) == pytest.approx(original, abs=30)


def test_both_shift_directions_work(tmp_path):
    early = retime(LATE, tmp_path / "early.wav", shift_ms=-200)
    later = retime(LATE, tmp_path / "later.wav", shift_ms=+200)
    assert duration_ms(later.path) > duration_ms(early.path)


def test_a_zero_shift_is_refused(tmp_path):
    """It would produce a new asset identical to the old one, verify as no
    change, and be counted against the strategy as a failure."""
    with pytest.raises(RepairError, match="0 ms is not a repair"):
        retime(LATE, tmp_path / "nothing.wav", shift_ms=0)


# ---------------------------------------------------------------------------
# REMIX
# ---------------------------------------------------------------------------


def test_normalising_moves_loudness_towards_the_target(tmp_path):
    before, _ = loudness_of(LATE)
    out = remix(LATE, tmp_path / "level.wav", target_lufs=-23.0, true_peak_max=-2.0)
    after, peak = loudness_of(out.path)

    assert abs(after - (-23.0)) < abs(before - (-23.0))
    assert after == pytest.approx(-23.0, abs=1.5)
    assert peak <= -2.0 + 0.5


def test_normalising_does_not_move_timing(tmp_path):
    """The mirror of the RETIME independence test, and it matters for the same
    reason."""
    before = sync_of(LATE)
    out = remix(LATE, tmp_path / "level.wav", target_lufs=-23.0, true_peak_max=-2.0)
    assert sync_of(out.path) == pytest.approx(before, abs=15)


# ---------------------------------------------------------------------------
# Nothing is edited in place
# ---------------------------------------------------------------------------


def test_the_failing_version_survives_its_own_repair(tmp_path):
    """'Before' has to be a fact that still exists on disk, not a number
    someone remembers. It is what makes verification honest and rollback a
    lookup rather than a rebuild."""
    original = LATE.read_bytes()
    retime(LATE, tmp_path / "new.wav", shift_ms=-40)
    remix(LATE, tmp_path / "new2.wav", target_lufs=-23.0, true_peak_max=-2.0)
    assert LATE.read_bytes() == original


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def intent(strategy: Strategy, params: dict) -> RepairIntent:
    return RepairIntent(
        strategy=strategy,
        target_asset_id="SINTEL:S03:dub_stem:de-DE",
        params=params,
        prediction=Prediction("dub_sync_offset_ms", "de-DE", "S03",
                              Direction.DECREASE, 102.0, 298.2),
        justification=[Evidence(kind="metric", query="dub_sync_offset_ms", value=298.2)],
        tier=AutonomyTier.AUTO_FIX_VERIFY,
    )


def test_dispatch_runs_the_named_strategy(tmp_path):
    out = apply(intent(Strategy.RETIME, {"shift_ms": -40}), LATE,
                tmp_path / "a.wav")
    assert out.strategy is Strategy.RETIME
    assert out.path.exists()

    out = apply(intent(Strategy.REMIX,
                       {"target_lufs": -23.0, "true_peak_max": -2.0}),
                LATE, tmp_path / "b.wav")
    assert out.strategy is Strategy.REMIX


def test_an_unexecutable_strategy_is_loud_rather_than_a_no_op(tmp_path):
    """A silent no-op would present as a repair that ran and changed nothing,
    which verification reports as `failed` and the autonomy ledger counts
    against the strategy rather than against the dispatch."""
    with pytest.raises(RepairError, match="REWRITE re-synthesises"):
        apply(intent(Strategy.REWRITE, {"target_ms": 1650}), LATE,
              tmp_path / "c.wav")
