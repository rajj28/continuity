"""The contracts are only worth having if they reject things.

Every test here is a specific way an LLM pipeline degrades -- a confident
sentence with no number behind it, a repair with no stated effect, a success
counted as competence. If any of these stops raising, the corresponding
failure mode is back in the system.
"""

from __future__ import annotations

import pytest

from agents.contracts import (
    AffectedAsset,
    AutonomyTier,
    Direction,
    Evidence,
    Finding,
    ImpactSet,
    Prediction,
    RepairIntent,
    Strategy,
    UnsupportedClaim,
    VerificationResult,
    confidence_from,
    earned_tier,
)


def metric_evidence(value: float = 480.0) -> Evidence:
    return Evidence(
        kind="metric",
        query='dub_sync_offset_ms{title="SINTEL",scene="S01",market="de-DE"}',
        value=value,
        source="grafanacloud-prom",
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_evidence_without_a_query_is_rejected():
    """The whole point of Evidence is that someone else can re-run it."""
    with pytest.raises(UnsupportedClaim, match="needs a query"):
        Evidence(kind="metric", query="   ", value=480.0)


def test_unknown_evidence_kind_is_rejected():
    with pytest.raises(UnsupportedClaim, match="unknown evidence kind"):
        Evidence(kind="vibes", query="looks off to me", value=1)  # type: ignore[arg-type]


def test_evidence_is_immutable():
    """A record of what was true then. Wanting a fresher number means running
    the query again, not editing the receipt."""
    e = metric_evidence()
    with pytest.raises(Exception):
        e.value = 12.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


def test_a_finding_must_cite_something():
    with pytest.raises(UnsupportedClaim, match="no evidence"):
        Finding(claim="the German dub drifts late", evidence=[])


def test_a_finding_with_evidence_is_fine():
    f = Finding(claim="de-DE drifts 480 ms late", evidence=[metric_evidence()])
    assert f.evidence[0].cite().startswith("[metric] dub_sync_offset_ms")


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------


def affected(n: int) -> list[AffectedAsset]:
    return [
        AffectedAsset(
            asset_id=f"SINTEL:S01:dub_stem:{i}",
            sha256="0" * 64,
            kind="DUB_STEM",
            market="de-DE",
            via_span_id=f"span{i}",
            trace_id="trace0",
        )
        for i in range(n)
    ]


def test_impact_set_reports_what_it_preserved():
    """'We regenerated 4 of 27' is the claim that makes this valuable, and it
    is only credible with the denominator stated."""
    impact = ImpactSet(
        root_asset_id="SINTEL:master",
        root_sha256="a" * 64,
        affected=affected(4),
        evidence=[Evidence(kind="trace", query='{ .continuity.asset.parent_sha256 = "aaa" }', value=4)],
        considered=27,
    )
    assert impact.preserved == 23
    assert impact.markets() == ["de-DE"]


def test_impact_set_cannot_claim_fewer_considered_than_affected():
    with pytest.raises(ValueError, match="considered must include"):
        ImpactSet(
            root_asset_id="SINTEL:master",
            root_sha256="a" * 64,
            affected=affected(4),
            evidence=[Evidence(kind="trace", query="{}", value=4)],
            considered=2,
        )


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


def test_a_decrease_must_actually_decrease():
    """Guards the failure where an agent restates the current value as its
    goal and then declares the prediction held."""
    with pytest.raises(ValueError, match="is not a decrease"):
        Prediction(
            series="dub_sync_offset_ms",
            market="de-DE",
            scene="S01",
            direction=Direction.DECREASE,
            target_value=480.0,
            baseline=480.0,
        )


def test_prediction_evaluates_both_directions():
    down = Prediction("dub_sync_offset_ms", "de-DE", "S01",
                      Direction.DECREASE, 120.0, 480.0)
    assert down.holds_for(63.0)
    assert not down.holds_for(310.0)

    up = Prediction("semantic_fidelity_score", "de-DE", "S01",
                    Direction.INCREASE, 0.85, 0.71)
    assert up.holds_for(0.91)
    assert not up.holds_for(0.80)


# ---------------------------------------------------------------------------
# RepairIntent
# ---------------------------------------------------------------------------


def retime_intent(**kw) -> RepairIntent:
    defaults = dict(
        strategy=Strategy.RETIME,
        target_asset_id="SINTEL:S01:dub_stem:de-DE",
        params={"shift_ms": -480},
        prediction=Prediction("dub_sync_offset_ms", "de-DE", "S01",
                              Direction.DECREASE, 120.0, 480.0),
        justification=[metric_evidence()],
        tier=AutonomyTier.AUTO_FIX_VERIFY,
    )
    defaults.update(kw)
    return RepairIntent(**defaults)  # type: ignore[arg-type]


def test_a_repair_must_justify_itself():
    with pytest.raises(UnsupportedClaim, match="no evidence"):
        retime_intent(justification=[])


def test_autonomy_flags_follow_the_tier():
    assert retime_intent().autonomous
    assert not retime_intent().needs_human
    escalated = retime_intent(tier=AutonomyTier.REQUIRE_APPROVAL)
    assert escalated.needs_human
    assert not escalated.autonomous


# ---------------------------------------------------------------------------
# Verification -- the distinction the whole autonomy story rests on
# ---------------------------------------------------------------------------


def verified(observed: float, passed: bool) -> VerificationResult:
    return VerificationResult(
        intent=retime_intent(),
        observed=observed,
        passed=passed,
        evidence=[metric_evidence(observed)],
    )


def test_right_for_the_right_reason_is_success():
    r = verified(63.0, passed=True)
    assert r.prediction_held
    assert r.outcome == "succeeded"
    assert r.delta == pytest.approx(-417.0)


def test_passing_without_the_prediction_holding_is_luck_not_competence():
    """The market shipped, but not for the reason the agent gave. Counting
    this as competence is how an agent talks itself into autonomy it has not
    demonstrated, so it gets its own bucket."""
    r = verified(310.0, passed=True)
    assert not r.prediction_held
    assert r.outcome == "lucky"


def test_a_repair_that_did_not_work_is_a_failure():
    assert verified(455.0, passed=False).outcome == "failed"


def test_verification_needs_evidence_too():
    with pytest.raises(UnsupportedClaim, match="no evidence"):
        VerificationResult(
            intent=retime_intent(), observed=63.0, passed=True, evidence=[]
        )


# ---------------------------------------------------------------------------
# Earned autonomy
# ---------------------------------------------------------------------------


def test_a_new_strategy_is_proposed_not_trusted():
    assert earned_tier(succeeded=2, lucky=0, failed=0) is AutonomyTier.RECOMMEND


def test_a_reliable_strategy_earns_verified_autonomy():
    assert earned_tier(succeeded=9, lucky=0, failed=1) is AutonomyTier.AUTO_FIX_VERIFY


def test_luck_raises_the_denominator_and_not_the_numerator():
    """Ten passes, none of them understood. Experience without competence
    must move a strategy DOWN the ladder, not up."""
    lucky_only = earned_tier(succeeded=0, lucky=10, failed=0)
    understood = earned_tier(succeeded=10, lucky=0, failed=0)
    assert lucky_only is AutonomyTier.REQUIRE_APPROVAL
    assert understood is AutonomyTier.AUTO_FIX_VERIFY
    assert lucky_only.value > understood.value


def test_a_failing_strategy_loses_its_privileges():
    assert earned_tier(succeeded=1, lucky=0, failed=9) is AutonomyTier.REQUIRE_APPROVAL


def test_a_ceiling_caps_autonomy_no_matter_how_good_the_record():
    """A rights-sensitive market can be capped, and history cannot argue past
    it. Tiers ascend as autonomy shrinks, so the cap is a floor on the value."""
    assert earned_tier(
        succeeded=100, lucky=0, failed=0, ceiling=AutonomyTier.REQUIRE_APPROVAL
    ) is AutonomyTier.REQUIRE_APPROVAL
    assert earned_tier(
        succeeded=100, lucky=0, failed=0, ceiling=AutonomyTier.BLOCK
    ) is AutonomyTier.BLOCK


def test_a_ceiling_never_grants_autonomy_a_record_has_not_earned():
    assert earned_tier(
        succeeded=0, lucky=0, failed=10, ceiling=AutonomyTier.AUTO_FIX_VERIFY
    ) is AutonomyTier.REQUIRE_APPROVAL


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def test_confidence_is_spread_aware_rather_than_asserted():
    tight_and_far = confidence_from([480.0, 479.0, 481.0], target=120.0)
    scattered_and_near = confidence_from([130.0, 90.0, 150.0, 100.0], target=120.0)
    assert tight_and_far == 1.0
    assert scattered_and_near < 0.5


def test_one_sample_does_not_produce_certainty():
    assert confidence_from([480.0], target=120.0) == 0.5
    assert confidence_from([], target=120.0) == 0.0
