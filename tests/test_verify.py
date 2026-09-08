"""Verification, and the distinction the whole autonomy story rests on.

`passed` and `prediction_held` are different questions and they come apart more
often than is comfortable. These tests are mostly about that gap: a repair that
worked for a reason the agent never gave must not earn the agent authority.
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
from agents.signal import Signal
from agents.verify import VerificationError, requirement_results, verify
from media.qc.types import Measurement
from tests.test_investigate import StubClient, sample

SYNC_KEY = "delivery.dub_sync_offset_ms"
PEAK_KEY = "delivery.audio_true_peak_dbtp"
LOUD_KEY = "delivery.audio_loudness_lufs"

BAR_SYNC = 'market_threshold{market="de-DE",requirement="dub_sync_max_ms"}'
BAR_PEAK = 'market_threshold{market="de-DE",requirement="true_peak_max_dbtp"}'
BAR_TARGET = 'market_threshold{market="de-DE",requirement="loudness_target_lufs"}'
BAR_TOL = 'market_threshold{market="de-DE",requirement="loudness_tolerance_lu"}'


def signal_for(**overrides) -> Signal:
    answers = {
        BAR_SYNC: [sample(120)],
        BAR_PEAK: [sample(-2.0)],
        BAR_TARGET: [sample(-23.0)],
        BAR_TOL: [sample(1.0)],
    }
    answers.update(overrides)
    return Signal(StubClient(answers))  # type: ignore[arg-type]


def measurement(key: str, value: float) -> Measurement:
    return Measurement(key=key, value=value, unit="ms", method="probe_v1")


def intent(baseline: float = 298.2, target: float = 102.0) -> RepairIntent:
    return RepairIntent(
        strategy=Strategy.RETIME,
        target_asset_id="SINTEL:S03:dub_stem:de-DE",
        params={"shift_ms": -baseline},
        prediction=Prediction("dub_sync_offset_ms", "de-DE", "S03",
                              Direction.DECREASE, target, baseline),
        justification=[Evidence(kind="metric", query="dub_sync_offset_ms",
                                value=baseline)],
        tier=AutonomyTier.AUTO_FIX_VERIFY,
    )


@pytest.fixture
def repaired(tmp_path) -> Path:
    path = tmp_path / "stem_v2.wav"
    path.write_bytes(b"RIFF....WAVE")     # verify only checks it exists
    return path


# What the failing version measured. Sync was broken; everything else was fine.
BEFORE = [
    Measurement(key=SYNC_KEY, value=298.2, unit="ms", method="probe_v1"),
    Measurement(key=PEAK_KEY, value=-2.18, unit="dBTP", method="probe_v1"),
    Measurement(key=LOUD_KEY, value=-22.5, unit="LUFS", method="probe_v1"),
]


def run(repaired: Path, measurements: list[Measurement], *, sig=None,
        it: RepairIntent | None = None, before=None):
    return verify(sig or signal_for(), it or intent(), repaired,
                  lambda _p: measurements, market="de-DE",
                  before=BEFORE if before is None else before)


# ---------------------------------------------------------------------------
# The three outcomes
# ---------------------------------------------------------------------------


def test_right_for_the_right_reason(repaired):
    result = run(repaired, [
        measurement(SYNC_KEY, 13.3), measurement(PEAK_KEY, -3.35),
        measurement(LOUD_KEY, -22.5),
    ])
    assert result.passed
    assert result.prediction_held
    assert result.outcome == "succeeded"


def test_passing_for_a_reason_the_agent_did_not_give_is_luck(repaired):
    """The market improved. The predicted number did not reach its target. An
    agent that banked this as competence would be claiming credit for weather."""
    result = run(repaired, [
        measurement(SYNC_KEY, 119.0),          # under the 120 bar, over the
        measurement(PEAK_KEY, -3.35),          # 102 it predicted
        measurement(LOUD_KEY, -22.5),
    ])
    assert result.passed
    assert not result.prediction_held
    assert result.outcome == "lucky"


def test_a_repair_that_did_not_work(repaired):
    result = run(repaired, [
        measurement(SYNC_KEY, 250.0), measurement(PEAK_KEY, -3.35),
        measurement(LOUD_KEY, -22.5),
    ])
    assert not result.passed
    assert not result.prediction_held
    assert result.outcome == "failed"


def test_a_pre_existing_fault_elsewhere_does_not_fail_the_repair(repaired):
    """The flaw the first live run exposed. RETIME fixed sync exactly as
    predicted and was recorded as `failed` because the loudness it never
    touched was still wrong -- which would punish a strategy for doing its job
    and poison the autonomy ledger against it. A pre-existing fault is another
    repair's work; the alert brings the agent back for it."""
    result = run(repaired, [
        measurement(SYNC_KEY, 13.3),
        measurement(PEAK_KEY, -3.35),
        measurement(LOUD_KEY, -17.83),        # was already failing, untouched
    ], before=[
        Measurement(key=SYNC_KEY, value=298.2, unit="ms", method="probe_v1"),
        Measurement(key=PEAK_KEY, value=-2.18, unit="dBTP", method="probe_v1"),
        Measurement(key=LOUD_KEY, value=-17.83, unit="LUFS", method="probe_v1"),
    ])
    assert result.prediction_held
    assert result.passed
    assert result.outcome == "succeeded"


def test_fixing_one_thing_and_breaking_another_is_not_passing(repaired):
    """The other direction, and the reason `passed` is not simply "the target
    is met". A shift that corrected sync and pushed true peak over the ceiling
    has not passed, however well its own prediction held."""
    result = run(repaired, [
        measurement(SYNC_KEY, 13.3),           # prediction holds ...
        measurement(PEAK_KEY, -1.2),           # ... but this now fails
        measurement(LOUD_KEY, -22.5),
    ])
    assert result.prediction_held
    assert not result.passed
    assert result.outcome == "failed"


# ---------------------------------------------------------------------------
# What verification refuses to do
# ---------------------------------------------------------------------------


def test_a_prediction_nothing_re_measures_cannot_be_held(repaired):
    """An unfalsifiable prediction must not count as held. Silence is not
    agreement."""
    with pytest.raises(VerificationError, match="cannot be falsified"):
        run(repaired, [measurement(PEAK_KEY, -3.35)])


def test_a_missing_file_is_not_a_pass(repaired, tmp_path):
    with pytest.raises(VerificationError, match="does not exist"):
        verify(signal_for(), intent(), tmp_path / "gone.wav",
               lambda _p: [measurement(SYNC_KEY, 13.3)], market="de-DE")


def test_nothing_judgeable_is_an_error_not_a_pass(repaired):
    """If no published threshold applies to anything we measured, there is
    nothing to verify against -- and 'no failures found' would be a lie."""
    with pytest.raises(VerificationError, match="nothing here to verify"):
        run(repaired, [measurement(SYNC_KEY, 13.3)],
            sig=signal_for(**{BAR_SYNC: []}))


def test_an_unjudged_measurement_is_skipped_not_assumed_to_pass():
    """A measurement with no published bar contributes nothing either way.
    The coverage gate is what notices it is missing; assuming a pass here
    would hide it."""
    results = requirement_results(
        signal_for(**{BAR_PEAK: []}),
        [measurement(SYNC_KEY, 13.3), measurement(PEAK_KEY, -1.0)],
        market="de-DE",
    )
    assert [key for key, *_ in results] == [SYNC_KEY]


# ---------------------------------------------------------------------------
# Loudness is a band, not a ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lufs,met", [
    (-22.5, True),    # inside -23 +/- 1
    (-23.5, True),
    (-21.0, False),   # too loud
    (-25.5, False),   # too quiet, and just as much a delivery failure
])
def test_loudness_is_judged_on_both_sides(lufs, met):
    results = requirement_results(
        signal_for(), [measurement(LOUD_KEY, lufs)], market="de-DE",
    )
    assert results[0][1] is met


# ---------------------------------------------------------------------------
# The evidence trail
# ---------------------------------------------------------------------------


def test_every_verification_cites_the_probe_that_produced_it(repaired):
    result = run(repaired, [
        measurement(SYNC_KEY, 13.3), measurement(PEAK_KEY, -3.35),
        measurement(LOUD_KEY, -22.5),
    ])
    assert len(result.evidence) == 3
    assert all(e.kind == "probe" for e in result.evidence)
    assert all(e.query and e.source for e in result.evidence)
    assert all("bar" in e.detail for e in result.evidence)


def test_the_ledger_records_the_outcome_not_the_intention(repaired):
    from agents.verify import record

    class Ledger:
        def __init__(self):
            self.entries = []

        def repair(self, *, strategy, outcome, market):
            self.entries.append((strategy, outcome, market))

    ledger = Ledger()
    record(ledger, run(repaired, [
        measurement(SYNC_KEY, 119.0), measurement(PEAK_KEY, -3.35),
        measurement(LOUD_KEY, -22.5),
    ]), market="de-DE")
    assert ledger.entries == [("RETIME", "lucky", "de-DE")]
