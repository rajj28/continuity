"""Specialists investigate concurrently; repairs are applied in lineage order.

Investigating is read-only, so any number of agents can do it at once. Acting
is not: two agents rewriting the same dub stem would race on the file. So the
swarm returns proposals and the caller applies them one at a time, in an order
taken from the asset lineage rather than invented.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace

from agents.conductor import Conclusion
from agents.contracts import (
    AutonomyTier,
    Direction,
    Evidence,
    Prediction,
    RepairIntent,
    Strategy,
)
from agents.investigate import Investigation
from agents.signal import Signal
from agents.specialists import AUDIO, COMPLIANCE, LOCALISATION, PACKAGING
from agents.swarm import Verdict, order_repairs, swarm
from agents.wake import parse
from telemetry.genai import GenAI
from telemetry.metrics import Instruments
from tests.test_investigate import StubClient
from tests.test_wake import grafana_payload


@pytest.fixture
def world():
    from opentelemetry.metrics import NoOpMeter
    signal = Signal(StubClient({}))                # type: ignore[arg-type]
    genai = GenAI(trace.get_tracer("test"), Instruments(NoOpMeter("t")),
                  "conductor")
    investigation = Investigation(incident=parse(grafana_payload())[0],
                                  still_failing=True)
    return signal, genai, investigation


def _intent(strategy: Strategy, asset_id: str) -> RepairIntent:
    return RepairIntent(
        strategy=strategy, target_asset_id=asset_id, params={},
        prediction=Prediction(series="dub_sync_offset_ms", market="de-DE",
                              scene="S03", direction=Direction.DECREASE,
                              target_value=100.0, baseline=300.0),
        justification=[Evidence(kind="metric", query="q", value=1.0,
                                source="s")],
        tier=AutonomyTier.RECOMMEND, rationale="because",
    )


def _proposal(specialist, asset_id: str) -> Verdict:
    return Verdict(specialist=specialist, checks=[],
                   conclusion=Conclusion(action="repair",
                                         intent=_intent(Strategy.RETIME,
                                                        asset_id)))


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_only_the_failing_dimensions_are_woken(world, monkeypatch):
    signal, genai, investigation = world
    woken: list[str] = []

    def fake_conduct(_client, _signal, _inv, _genai, *, specialist, focus,
                     **_kw):
        woken.append(specialist.name)
        return Conclusion(action="escalate", reason="x")

    monkeypatch.setattr("agents.swarm.conduct", fake_conduct)
    result = asyncio.run(swarm(object(), signal, investigation, genai,
                               failing=["loudness", "rights_cleared"]))
    assert sorted(woken) == ["audio", "compliance"]
    assert len(result.verdicts) == 2


def test_each_specialist_is_asked_only_about_its_own_checks(world, monkeypatch):
    signal, genai, investigation = world
    asked: dict[str, list[str]] = {}

    def fake_conduct(_c, _s, _i, _g, *, specialist, focus, **_kw):
        asked[specialist.name] = focus
        return Conclusion(action="escalate")

    monkeypatch.setattr("agents.swarm.conduct", fake_conduct)
    asyncio.run(swarm(object(), signal, investigation, genai,
                      failing=["dub_sync", "line_overrun", "certified"]))
    assert asked["localisation"] == ["dub_sync", "line_overrun"]
    assert asked["compliance"] == ["certified"]


def test_specialists_really_do_overlap(world, monkeypatch):
    """Not merely called in a loop -- actually in flight together.

    Each agent spends its time waiting on HTTP, so running them one after
    another turns a 4-second incident into a 20-second one for no reason.
    """
    signal, genai, investigation = world
    live = 0
    peak = 0

    def fake_conduct(_c, _s, _i, _g, **_kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        import time
        time.sleep(0.05)
        live -= 1
        return Conclusion(action="escalate")

    monkeypatch.setattr("agents.swarm.conduct", fake_conduct)
    asyncio.run(swarm(object(), signal, investigation, genai,
                      failing=["dub_sync", "loudness", "certified",
                               "ad_collision"]))
    assert peak > 1, "the specialists ran one at a time"


def test_concurrency_is_capped(world, monkeypatch):
    """They share a model quota and one Grafana datasource."""
    signal, genai, investigation = world
    live = 0
    peak = 0

    def fake_conduct(_c, _s, _i, _g, **_kw):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        import time
        time.sleep(0.05)
        live -= 1
        return Conclusion(action="escalate")

    monkeypatch.setattr("agents.swarm.conduct", fake_conduct)
    asyncio.run(swarm(object(), signal, investigation, genai, limit=2,
                      failing=["dub_sync", "loudness", "certified",
                               "ad_collision", "deliverables_complete"]))
    assert peak <= 2


def test_one_specialist_failing_does_not_take_the_others_with_it(world,
                                                                 monkeypatch):
    """A market where compliance errored and localisation found a real repair
    is better served by the repair plus a visible error than by nothing."""
    signal, genai, investigation = world

    def fake_conduct(_c, _s, _i, _g, *, specialist, **_kw):
        if specialist.name == "compliance":
            raise RuntimeError("vertex quota exhausted")
        return Conclusion(action="escalate", reason="fine")

    monkeypatch.setattr("agents.swarm.conduct", fake_conduct)
    result = asyncio.run(swarm(object(), signal, investigation, genai,
                               failing=["dub_sync", "rights_cleared"]))
    assert len(result.failures) == 1
    assert result.failures[0].name == "compliance"
    assert "quota" in result.failures[0].error
    assert len(result.verdicts) == 2, "the healthy one still concluded"


def test_nothing_failing_wakes_nobody(world, monkeypatch):
    signal, genai, investigation = world
    monkeypatch.setattr("agents.swarm.conduct",
                        lambda *a, **k: pytest.fail("should not be called"))
    result = asyncio.run(swarm(object(), signal, investigation, genai,
                               failing=[]))
    assert result.verdicts == []


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_a_repair_runs_before_what_is_built_from_it():
    """Rebuilding the package first would be work thrown away.

    The order is not invented: the package records the dub stem as a parent,
    so repairing the stem invalidates the package. Lineage IS the schedule.
    """
    stem = _proposal(LOCALISATION, "SINTEL:S03:dub_stem:de-DE")
    package = _proposal(PACKAGING, "SINTEL:S03:package:de-DE")
    dependencies = {
        "SINTEL:S03:dub_stem:de-DE": ["SINTEL:S03:package:de-DE"],
    }
    ordered = order_repairs([package, stem], dependencies)
    assert [v.name for v in ordered] == ["localisation", "packaging"]


def test_unrelated_repairs_keep_their_order():
    """Two proposals that do not touch each other must stay reproducible."""
    audio = _proposal(AUDIO, "SINTEL:S03:dub_stem:de-DE")
    other = _proposal(PACKAGING, "SINTEL:S03:subtitle:de-DE")
    assert [v.name for v in order_repairs([audio, other], {})] == [
        "audio", "packaging"]
    assert [v.name for v in order_repairs([other, audio], {})] == [
        "packaging", "audio"]


def test_ordering_ignores_dependants_that_nobody_proposed():
    """Only proposals in this batch can be ordered against each other.

    The stem has a dependant, but nothing proposed repairing it, so there is
    nothing to sequence and the batch should not be reshuffled on the strength
    of an asset no agent mentioned.
    """
    stem = _proposal(LOCALISATION, "SINTEL:S03:dub_stem:de-DE")
    audio = _proposal(AUDIO, "SINTEL:S03:subtitle:de-DE")
    dependencies = {"SINTEL:S03:dub_stem:de-DE": ["SINTEL:S03:package:de-DE"]}
    assert [v.name for v in order_repairs([audio, stem], dependencies)] == [
        "audio", "localisation"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_the_swarm_separates_repairs_from_escalations(world, monkeypatch):
    signal, genai, investigation = world

    def fake_conduct(_c, _s, _i, _g, *, specialist, **_kw):
        if specialist is COMPLIANCE:
            return Conclusion(action="escalate", reason="rights_not_cleared")
        return Conclusion(action="repair",
                          intent=_intent(Strategy.RETIME, "asset"))

    monkeypatch.setattr("agents.swarm.conduct", fake_conduct)
    result = asyncio.run(swarm(object(), signal, investigation, genai,
                               failing=["dub_sync", "rights_cleared"]))
    assert [v.name for v in result.repairs] == ["localisation"]
    assert [v.name for v in result.escalations] == ["compliance"]
    assert "localisation" in result.describe()
