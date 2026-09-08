"""The decision the recovery arc turns on.

`dub_sync_offset_ms` looks the same for both failures this planner has to tell
apart. A system reading only the failing number picks RETIME for both and is
surprised the second time. These tests are about that distinction and about
what the planner commits to before it acts.
"""

from __future__ import annotations

import pytest

from agents.contracts import AutonomyTier, Direction, Strategy
from agents.investigate import Investigation
from agents.plan import SAFETY_MARGIN, Unplannable, plan, plan_sync_repair, unrepairable
from agents.signal import Signal
from agents.wake import parse
from tests.test_investigate import StubClient, sample
from tests.test_wake import grafana_payload

SYNC = 'dub_sync_offset_ms{market="de-DE",title="SINTEL"}'
BAR = 'market_threshold{market="de-DE",requirement="dub_sync_max_ms"}'
SHAPE = 'dub_drift_systematic{title="SINTEL",market="de-DE"}'
LOUD_M = 'audio_loudness_lufs{market="de-DE",scene="S01",title="SINTEL"}'
LOUD_T = 'market_threshold{market="de-DE",requirement="loudness_target_lufs"}'
LOUD_TOL = 'market_threshold{market="de-DE",requirement="loudness_tolerance_lu"}'
PEAK_T = 'market_threshold{market="de-DE",requirement="true_peak_max_dbtp"}'

# A stem 5.17 LU louder than the -23 target, outside the +/- 1 band.
LOUD = {
    LOUD_M: [sample(-17.83)], LOUD_T: [sample(-23.0)],
    LOUD_TOL: [sample(1.0)], PEAK_T: [sample(-2.0)],
}


def signal_for(**overrides) -> Signal:
    answers = {
        SYNC: [sample(480)],
        BAR: [sample(120)],
        SHAPE: [sample(1)],
    }
    answers.update(overrides)
    return Signal(StubClient(answers))  # type: ignore[arg-type]


def history(strategy: str, market: str, **counts) -> dict:
    expr = (f'sum by (outcome) (continuity_repairs_total'
            f'{{market="{market}",strategy="{strategy}"}})')
    return {expr: [sample(v, outcome=k) for k, v in counts.items()]}


# ---------------------------------------------------------------------------
# The branch
# ---------------------------------------------------------------------------


def test_uniform_drift_is_repaired_by_shifting_the_stem():
    intent = plan_sync_repair(
        signal_for(**{SHAPE: [sample(1)]}),
        title="SINTEL", market="de-DE", scene="S01",
    )
    assert intent.strategy is Strategy.RETIME
    assert intent.params == {"shift_ms": -480.0}
    assert "offset rather than mistimed" in intent.rationale


def test_progressive_drift_is_not_repaired_by_shifting_the_stem():
    """The case a symptom-reader gets wrong. Shifting moves the whole
    staircase; the lines have to get shorter."""
    intent = plan_sync_repair(
        signal_for(**{SHAPE: [sample(0)]}),
        title="SINTEL", market="de-DE", scene="S01",
    )
    assert intent.strategy is Strategy.REWRITE
    assert "shift_ms" not in intent.params
    assert intent.params["reduce_by_ms"] == pytest.approx(480 - 120 * SAFETY_MARGIN)
    assert "staircase" in intent.rationale


def test_the_same_failing_number_produces_different_repairs():
    """Both start from 480 ms against a 120 ms limit. Only the drift shape
    differs, and it is the whole decision."""
    systematic = plan_sync_repair(signal_for(**{SHAPE: [sample(1)]}),
                                  title="SINTEL", market="de-DE", scene="S01")
    progressive = plan_sync_repair(signal_for(**{SHAPE: [sample(0)]}),
                                   title="SINTEL", market="de-DE", scene="S01")
    assert systematic.prediction.baseline == progressive.prediction.baseline
    assert systematic.strategy is not progressive.strategy


def test_a_second_attempt_can_be_forced_to_a_different_hypothesis():
    """When verification refutes a RETIME, the Conductor re-plans as REWRITE.
    Without this the same evidence produces the same answer forever."""
    intent = plan_sync_repair(
        signal_for(), title="SINTEL", market="de-DE", scene="S01",
        force=Strategy.REWRITE, supersedes="intent-1",
    )
    assert intent.strategy is Strategy.REWRITE
    assert intent.supersedes == "intent-1"


# ---------------------------------------------------------------------------
# What the planner commits to
# ---------------------------------------------------------------------------


def test_the_repair_aims_inside_the_threshold_not_at_it():
    """Landing exactly on the boundary would count as a success while leaving
    the market one rounding error from failing again."""
    intent = plan_sync_repair(signal_for(), title="SINTEL", market="de-DE",
                              scene="S01")
    assert intent.prediction.target_value == pytest.approx(102.0)
    assert intent.prediction.target_value < 120.0


def test_the_prediction_is_falsifiable_before_the_repair_runs():
    intent = plan_sync_repair(signal_for(), title="SINTEL", market="de-DE",
                              scene="S01")
    p = intent.prediction
    assert p.direction is Direction.DECREASE
    assert p.baseline == 480.0
    assert p.holds_for(63.0)
    assert not p.holds_for(455.0)
    assert "480 -> <= 102" in p.describe()


def test_every_repair_cites_the_three_facts_it_rests_on():
    intent = plan_sync_repair(signal_for(), title="SINTEL", market="de-DE",
                              scene="S01")
    queried = {e.query for e in intent.justification}
    assert queried == {SYNC, BAR, SHAPE}
    assert all(e.query.strip() for e in intent.justification)


# ---------------------------------------------------------------------------
# Refusing to plan
# ---------------------------------------------------------------------------


def test_planning_on_a_partial_picture_is_refused():
    """No drift shape means no way to choose a strategy. Guessing here aims
    the repair at the wrong thing."""
    with pytest.raises(Unplannable, match="no drift shape"):
        plan_sync_repair(signal_for(**{SHAPE: []}),
                         title="SINTEL", market="de-DE", scene="S01")


def test_a_missing_threshold_stops_planning_too():
    with pytest.raises(Unplannable, match="no threshold"):
        plan_sync_repair(signal_for(**{BAR: []}),
                         title="SINTEL", market="de-DE", scene="S01")


def test_a_measurement_already_in_tolerance_is_not_repaired():
    with pytest.raises(Unplannable, match="nothing to repair"):
        plan_sync_repair(signal_for(**{SYNC: [sample(40)]}),
                         title="SINTEL", market="de-DE", scene="S01")


# ---------------------------------------------------------------------------
# Autonomy comes from the ledger, not from the planner
# ---------------------------------------------------------------------------


def test_an_unproven_strategy_is_proposed_rather_than_run():
    intent = plan_sync_repair(signal_for(), title="SINTEL", market="de-DE",
                              scene="S01")
    assert intent.tier is AutonomyTier.RECOMMEND
    assert intent.needs_human


def test_a_strategy_with_a_good_record_acts_on_its_own():
    intent = plan_sync_repair(
        signal_for(**history("RETIME", "de-DE", succeeded=9, failed=1)),
        title="SINTEL", market="de-DE", scene="S01",
    )
    assert intent.tier is AutonomyTier.AUTO_FIX_VERIFY
    assert intent.autonomous


def test_a_strategy_that_keeps_getting_lucky_loses_its_privileges():
    """Ten passes, none of them understood. Experience without competence has
    to move a strategy down the ladder."""
    intent = plan_sync_repair(
        signal_for(**history("RETIME", "de-DE", lucky=10)),
        title="SINTEL", market="de-DE", scene="S01",
    )
    assert intent.tier is AutonomyTier.REQUIRE_APPROVAL
    assert not intent.autonomous


# ---------------------------------------------------------------------------
# The whole plan
# ---------------------------------------------------------------------------


def investigation_for(*subjects: str) -> Investigation:
    from agents.contracts import Evidence, Finding
    return Investigation(
        incident=parse(grafana_payload())[0],
        still_failing=True,
        findings=[
            Finding(claim=f"{s} fails", subject=s,
                    evidence=[Evidence(kind="metric", query=f"{s}_q", value=1)])
            for s in subjects
        ],
    )


def test_a_recovered_market_gets_no_plan():
    empty = Investigation(incident=parse(grafana_payload())[0],
                          still_failing=False)
    assert plan(signal_for(), empty) == []


def test_only_failures_we_have_a_strategy_for_are_planned():
    """A strategy invented for an unfamiliar failure is a strategy with no
    measured history -- exactly what the autonomy ladder exists to keep away
    from production assets."""
    investigation = investigation_for("dub_sync", "semantic_fidelity")
    intents = plan(signal_for(), investigation)
    assert [i.strategy for i in intents] == [Strategy.RETIME]
    assert unrepairable(investigation) == ["semantic_fidelity"]


def test_an_investigation_we_cannot_act_on_yields_an_empty_plan():
    """Which the Conductor reads as 'escalate', not as 'nothing wrong'."""
    investigation = investigation_for("rights_cleared", "coverage")
    assert plan(signal_for(), investigation) == []
    assert unrepairable(investigation) == ["coverage", "rights_cleared"]


def test_a_loudness_failure_is_planned_as_a_remix():
    investigation = investigation_for("loudness")
    intent, = plan(signal_for(**LOUD), investigation)
    assert intent.strategy is Strategy.REMIX
    assert intent.params == {"target_lufs": -23.0, "true_peak_max": -2.0}


# ---------------------------------------------------------------------------
# Loudness is a band, and that changes what a prediction may say
# ---------------------------------------------------------------------------


def test_a_band_repair_predicts_the_near_edge_not_the_target():
    """Aiming at -23 would be wrong. Normalising lands NEAR the target, not on
    it, so a repair arriving at -22.5 inside a +/- 1 band would be recorded as
    having failed its own prediction -- punishing a repair that worked."""
    from agents.plan import plan_loudness_repair

    intent = plan_loudness_repair(signal_for(**LOUD), title="SINTEL",
                                  market="de-DE", scene="S01")
    assert intent.prediction.target_value == pytest.approx(-22.0)
    assert intent.prediction.direction is Direction.DECREASE
    assert intent.prediction.holds_for(-22.5)      # the real measured result
    assert not intent.prediction.holds_for(-21.0)  # still too loud


def test_a_stem_that_is_too_quiet_is_pushed_the_other_way():
    answers = dict(LOUD)
    answers[LOUD_M] = [sample(-27.0)]
    from agents.plan import plan_loudness_repair

    intent = plan_loudness_repair(signal_for(**answers), title="SINTEL",
                                  market="de-DE", scene="S01")
    assert intent.prediction.direction is Direction.INCREASE
    assert intent.prediction.target_value == pytest.approx(-24.0)
    assert "quieter" in intent.rationale


def test_a_stem_already_inside_the_band_is_not_repaired():
    answers = dict(LOUD)
    answers[LOUD_M] = [sample(-22.6)]
    from agents.plan import plan_loudness_repair

    with pytest.raises(Unplannable, match="inside the -23"):
        plan_loudness_repair(signal_for(**answers), title="SINTEL",
                             market="de-DE", scene="S01")


def test_a_remix_says_it_will_not_disturb_the_timing():
    """The independence that makes two sequential repairs attributable."""
    from agents.plan import plan_loudness_repair

    intent = plan_loudness_repair(signal_for(**LOUD), title="SINTEL",
                                  market="de-DE", scene="S01")
    assert "cannot disturb the sync" in intent.rationale
