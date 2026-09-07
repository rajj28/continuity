"""Investigation, against a Signal that returns whatever the test says.

The stub is a dict of PromQL -> samples, so each test states exactly what
Grafana would answer and nothing else. That matters more here than usual: the
whole claim of this file is that a finding cannot exist without a query that
produced it, and the way to prove it is to withhold the query and check that
the finding disappears.
"""

from __future__ import annotations

from agents.investigate import investigate
from agents.signal import Signal
from agents.wake import parse
from tests.test_wake import grafana_payload


class StubClient:
    """Answers query_prometheus from a canned table, and records what was asked."""

    def __init__(self, answers: dict[str, list[dict]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def call(self, name: str, arguments: dict):
        assert name == "query_prometheus", name
        expr = arguments["expr"]
        self.asked.append(expr)
        return {"data": self.answers.get(expr, [])}


def sample(value: float, **labels) -> dict:
    return {"metric": labels, "value": [1788790000.0, str(value)]}


def signal_for(answers: dict) -> Signal:
    return Signal(StubClient(answers))  # type: ignore[arg-type]


VERDICT = 'market_release_ready{market="de-DE",title="SINTEL"}'
FAILING = 'market_requirement_met{market="de-DE",title="SINTEL"} == 0'
STALE_SUM = 'sum(asset_stale{title="SINTEL",market="de-DE"})'
PRESENT = 'market_checks_present{market="de-DE",title="SINTEL"}'
OWED = 'market_required_checks{market="de-DE"}'
SYNC_BAR = 'market_threshold{market="de-DE",requirement="dub_sync_max_ms"}'


def incident():
    return parse(grafana_payload())[0]


# ---------------------------------------------------------------------------
# Is it still true?
# ---------------------------------------------------------------------------


def test_a_market_that_recovered_is_not_repaired():
    """With `for: 2m` on the rule and a repair possibly already in flight, the
    market being green by the time we look is a normal outcome -- and acting on
    it is how an autonomous system does damage."""
    result = investigate(signal_for({VERDICT: [sample(1)]}), incident())
    assert result.resolved_before_we_arrived
    assert result.findings == []
    assert "recovered" in result.summary()


def test_no_verdict_at_all_is_treated_as_still_failing():
    """Absent is not recovery. A market we cannot judge is strictly worse than
    one we judged as failing, so it must not be mistaken for a green light."""
    result = investigate(signal_for({}), incident())
    assert result.still_failing
    assert not result.resolved_before_we_arrived
    assert any("cannot currently be computed" in g for g in result.gaps)


# ---------------------------------------------------------------------------
# What is failing?
# ---------------------------------------------------------------------------


def test_a_failing_check_is_reported_with_the_bar_it_failed_against():
    """'480' is not a diagnosis. '480 against a 120 limit' is."""
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [sample(0, requirement="dub_sync", market="de-DE")],
        SYNC_BAR: [sample(120)],
        PRESENT: [sample(6)],
        OWED: [sample(6)],
        STALE_SUM: [sample(0)],
    }), incident())

    assert result.subjects == ["dub_sync"]
    finding = result.findings[0]
    assert len(finding.evidence) == 2
    assert any(e.value == 120.0 for e in finding.evidence)
    # every number carries the query that produced it
    assert all(e.query for e in finding.evidence)


def test_several_failing_checks_are_all_reported():
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [
            sample(0, requirement="dub_sync", market="de-DE"),
            sample(0, requirement="loudness", market="de-DE"),
        ],
        PRESENT: [sample(6)], OWED: [sample(6)], STALE_SUM: [sample(0)],
    }), incident())
    assert result.subjects == ["dub_sync", "loudness"]


# ---------------------------------------------------------------------------
# What was never measured?
# ---------------------------------------------------------------------------


def test_a_market_can_be_blocked_with_nothing_failing():
    """The case that reads as healthy on every dashboard unless something
    asks: one check present, six owed, and the one present passes."""
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [],
        PRESENT: [sample(1)],
        OWED: [sample(6)],
        STALE_SUM: [sample(0)],
    }), incident())

    assert result.subjects == ["coverage"]
    claim = result.findings[0].claim
    assert "1 of 6" in claim
    assert "never measured" in claim


def test_full_coverage_produces_no_coverage_finding():
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [sample(0, requirement="dub_sync", market="de-DE")],
        PRESENT: [sample(6)], OWED: [sample(6)], STALE_SUM: [sample(0)],
    }), incident())
    assert "coverage" not in result.subjects


# ---------------------------------------------------------------------------
# What moved underneath it?
# ---------------------------------------------------------------------------


def test_staleness_is_reported_as_its_own_finding():
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [],
        PRESENT: [sample(6)], OWED: [sample(6)],
        STALE_SUM: [sample(3)],
        'asset_stale{title="SINTEL",market="de-DE"} == 1': [
            sample(1, scene="S01", market="de-DE")
        ],
    }), incident())
    assert "staleness" in result.subjects
    assert "3 asset(s)" in result.findings[0].claim


def test_a_healthy_lineage_is_not_searched():
    """A blast radius query on a lineage with nothing stale returns everything
    and explains nothing, so it is not asked for."""
    stub = StubClient({
        VERDICT: [sample(0)], FAILING: [], PRESENT: [sample(6)],
        OWED: [sample(6)], STALE_SUM: [sample(0)],
    })
    investigate(Signal(stub), incident())  # type: ignore[arg-type]
    assert not any("== 1" in q for q in stub.asked)


def test_missing_staleness_data_is_a_gap_not_a_clean_bill():
    """An investigation that silently skipped a step looks identical to one
    where the step came back clean. The Conductor has to tell those apart."""
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [sample(0, requirement="dub_sync", market="de-DE")],
        PRESENT: [sample(6)], OWED: [sample(6)],
        # no STALE_SUM answer at all
    }), incident())
    assert any("staleness unknown" in g for g in result.gaps)


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------


def test_every_finding_is_backed_by_a_re_runnable_query():
    """The claim this whole file rests on: withhold the query and the finding
    cannot exist, because Evidence refuses to be constructed without one."""
    result = investigate(signal_for({
        VERDICT: [sample(0)],
        FAILING: [sample(0, requirement="dub_sync", market="de-DE")],
        SYNC_BAR: [sample(120)],
        PRESENT: [sample(6)], OWED: [sample(6)], STALE_SUM: [sample(2)],
        'asset_stale{title="SINTEL",market="de-DE"} == 1': [sample(1)],
    }), incident())

    trail = result.evidence()
    assert len(trail) >= 3
    assert all(e.query.strip() for e in trail)
    assert all(e.source == "grafanacloud-prom" for e in trail)
