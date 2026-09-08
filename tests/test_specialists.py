"""Authority is the tool list, not the prompt.

The compliance specialist cannot propose a repair for an uncleared music cue.
Not because it is instructed not to -- because `propose_repair` is absent from
the tool surface it is given, so there is no call it could make that would
express one.

That distinction is the whole reason these tests exist. A prompt saying "never
invent a fix for a rights failure" is a request. A missing function is a fact
about the protocol, and it is the same guarantee as `mcp-grafana
--disable-write` applied one level up: the capability is not refused at
runtime, it is absent.

So what is asserted here is not behaviour under a particular model. It is the
shape of what the model is handed, which holds whatever the model does.
"""

from __future__ import annotations

import pytest

from agents.conductor import EXECUTABLE, _briefing, _declarations, _system_for
from agents.contracts import AutonomyTier, Strategy
from agents.investigate import Investigation
from agents.wake import parse
from tests.test_wake import grafana_payload
from agents.specialists import (
    ACCESSIBILITY,
    AUDIO,
    COMPLIANCE,
    LOCALISATION,
    PACKAGING,
    ROSTER,
    dispatch_for,
    for_check,
)


def _tool_names(specialist=None) -> list[str]:
    return [d["name"] for d in _declarations(specialist)]


@pytest.fixture
def sample_investigation() -> Investigation:
    return Investigation(incident=parse(grafana_payload())[0],
                         still_failing=True)


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_compliance_is_never_handed_a_repair_tool():
    """The headline guarantee. A right is not a defect in a file."""
    assert "propose_repair" not in _tool_names(COMPLIANCE)
    assert "escalate" in _tool_names(COMPLIANCE)
    assert COMPLIANCE.may_repair is False


@pytest.mark.parametrize(
    "specialist", [COMPLIANCE, ACCESSIBILITY, PACKAGING],
    ids=lambda s: s.name)
def test_specialists_with_no_executable_strategy_cannot_propose(specialist):
    """Not being able to fix it and not being offered the tool must agree.

    An agent with an empty strategy set but a `propose_repair` tool would be
    invited to name a strategy and then refused for naming any of them, which
    wastes a turn and teaches it nothing.
    """
    assert specialist.strategies == frozenset()
    assert "propose_repair" not in _tool_names(specialist)


def test_a_specialist_is_offered_only_its_own_strategies():
    """An audio agent that decides the problem is timing must hand it back."""
    proposal = [d for d in _declarations(AUDIO) if d["name"] == "propose_repair"]
    assert proposal, "audio can repair, so it must have the tool"
    offered = proposal[0]["parameters"]["properties"]["strategy"]["enum"]
    assert offered == ["REMIX"]
    assert "RETIME" not in offered


def test_the_unrestricted_conductor_keeps_the_full_surface():
    """Specialisation must not quietly narrow the general agent too."""
    assert _tool_names() == [
        "list_failing_checks", "query_metric", "get_threshold",
        "repair_history", "metric_history", "blast_radius",
        "compare_markets", "propose_repair", "escalate",
    ]
    offered = [d for d in _declarations()
               if d["name"] == "propose_repair"][0]
    enum = offered["parameters"]["properties"]["strategy"]["enum"]
    assert set(enum) == {s.value for s in EXECUTABLE}


def test_no_specialist_offers_a_strategy_the_executor_cannot_run():
    """REWRITE validates and then dies at the moment of acting.

    `agents.repair.apply` refuses it -- it re-synthesises through the dub
    pipeline rather than transforming an asset. Offering it would produce a
    proposal that passes every contract, gets approved by a human, and fails
    when the work starts, which is the worst possible place to discover that a
    capability does not exist.
    """
    for specialist in ROSTER:
        assert Strategy.REWRITE not in specialist.strategies, specialist.name


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_every_check_lands_with_exactly_one_specialist():
    """Two owners means two agents repairing the same asset concurrently."""
    seen: dict[str, str] = {}
    for specialist in ROSTER:
        for check in specialist.owns:
            assert check not in seen, (
                f"{check} is owned by both {seen.get(check)} and "
                f"{specialist.name}"
            )
            seen[check] = specialist.name


def test_only_the_specialists_whose_dimension_failed_are_woken():
    """The saving that makes a roster affordable, and just correct anyway."""
    work = dispatch_for(["dub_sync", "loudness"])
    assert [s.name for s, _ in work] == ["localisation", "audio"]
    assert "compliance" not in [s.name for s, _ in work]


def test_each_specialist_is_told_only_its_own_failures():
    work = dict((s.name, checks) for s, checks in dispatch_for(
        ["dub_sync", "line_overrun", "loudness", "rights_cleared"]))
    assert work["localisation"] == ["dub_sync", "line_overrun"]
    assert work["audio"] == ["loudness"]
    assert work["compliance"] == ["rights_cleared"]


def test_a_check_nobody_owns_is_surfaced_rather_than_dropped():
    """A hole in the roster must look like one.

    Silently skipping an unowned check would report a market as fully
    investigated while nobody had looked at one of its failures -- the same
    class of lie as a coverage gate that ignores absent measurements.
    """
    work = dispatch_for(["dub_sync", "a_check_from_the_future"])
    names = {s.name: checks for s, checks in work}
    assert names["unassigned"] == ["a_check_from_the_future"]


def test_dispatch_order_is_stable():
    """A run has to be reproducible to be comparable."""
    failing = ["rights_cleared", "loudness", "dub_sync"]
    assert ([s.name for s, _ in dispatch_for(failing)]
            == [s.name for s, _ in dispatch_for(list(reversed(failing)))])


def test_for_check_knows_the_dimensions():
    assert for_check("dub_sync") is LOCALISATION
    assert for_check("true_peak") is AUDIO
    assert for_check("certified") is COMPLIANCE
    assert for_check("nothing_owns_this") is None


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------


def test_an_agent_with_no_repair_is_told_so_plainly(sample_investigation):
    """It should not spend a turn looking for a tool it does not have."""
    brief = _briefing(sample_investigation, AutonomyTier.RECOMMEND,
                      specialist=COMPLIANCE, focus=["rights_cleared"])
    assert "NONE" in brief
    assert "Investigate, then escalate." in brief
    assert "propose_repair" not in brief.split("Investigate, then")[1]


def test_a_specialist_is_told_which_failures_are_its_own(sample_investigation):
    brief = _briefing(sample_investigation, AutonomyTier.RECOMMEND,
                      specialist=AUDIO, focus=["loudness", "true_peak"])
    assert "loudness" in brief and "true_peak" in brief
    assert "Do not re-triage the whole market." in brief


def test_a_specialist_brief_replaces_the_triage_instructions():
    """"Repair the most consequential blocker" is wrong advice for a
    single-dimension agent, and is exactly the sentence that would push a
    compliance agent to reach for a repair it does not have."""
    general = _system_for(None)
    specialised = _system_for(LOCALISATION)
    assert "most consequential blocker" in general
    assert "most consequential blocker" not in specialised
    # The invariants every agent keeps, specialised or not.
    for instruction in (general, specialised):
        assert "Never state a number you did not obtain from a tool." in instruction
        assert "Citing a query you did not run will be rejected." in instruction
