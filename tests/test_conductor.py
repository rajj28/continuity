"""The Conductor, driven by a model whose every move the test chooses.

The point of these tests is not that the agent works when the model behaves.
It is that the guardrails hold when it does not -- when it cites a query it
never ran, proposes a strategy that does not exist, predicts a change to the
value already measured, or simply talks forever.

A guardrail that has only been tested against a cooperative model has not been
tested.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.conductor import Conclusion, Toolbox, conduct
from agents.contracts import AutonomyTier, Strategy
from agents.investigate import Investigation
from agents.signal import Signal
from agents.wake import parse
from telemetry.genai import GenAI
from tests.test_investigate import StubClient, sample
from tests.test_wake import grafana_payload

SYNC = 'dub_sync_offset_ms{market="de-DE",scene="S03",title="SINTEL"}'
SHAPE = 'dub_drift_systematic{market="de-DE",title="SINTEL"}'
BAR = 'market_threshold{market="de-DE",requirement="dub_sync_max_ms"}'
FAILING = 'market_requirement_met{market="de-DE",title="SINTEL"} == 0'


# ---------------------------------------------------------------------------
# A model we script turn by turn
# ---------------------------------------------------------------------------


class Call(SimpleNamespace):
    """One function call the scripted model makes."""


def turn(*calls: Call):
    """One model response containing the given calls."""
    return SimpleNamespace(
        function_calls=list(calls),
        candidates=[SimpleNamespace(
            content=SimpleNamespace(role="model", parts=[]),
            finish_reason="STOP",
        )],
        text="",
        usage_metadata=SimpleNamespace(
            prompt_token_count=120, candidates_token_count=40
        ),
    )


class ScriptedModel:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.sent = []

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.sent.append(contents)
        if self.script:
            return self.script.pop(0)
        return turn()   # nothing more to say


class FakeClient:
    def __init__(self, script):
        self.models = ScriptedModel(script)


class RecordingInstruments:
    def __init__(self):
        self.counters: dict[tuple, float] = {}
        self.gauges: dict[tuple, float] = {}

    def _key(self, name, attrs):
        return (name, tuple(sorted((attrs or {}).items())))

    def counter(self, name, description=""):
        parent = self

        class _C:
            def add(self, value, attributes=None):
                k = parent._key(name, attributes)
                parent.counters[k] = parent.counters.get(k, 0) + value
        return _C()

    def gauge(self, name, description=""):
        parent = self

        class _G:
            def set(self, value, attributes=None):
                parent.gauges[parent._key(name, attributes)] = value
        return _G()

    def rejections(self) -> dict[str, float]:
        return {
            dict(attrs)["reason"]: v
            for (name, attrs), v in self.counters.items()
            if name == "continuity_agent_rejections_total"
        }

    def tool_calls(self) -> dict[str, float]:
        return {
            dict(attrs)["tool"]: v
            for (name, attrs), v in self.counters.items()
            if name == "continuity_agent_tool_calls_total"
        }


@pytest.fixture
def world():
    from opentelemetry import trace

    answers = {
        SYNC: [sample(298.2)],
        SHAPE: [sample(1)],
        BAR: [sample(120)],
        FAILING: [sample(0, requirement="dub_sync", market="de-DE")],
    }
    signal = Signal(StubClient(answers))            # type: ignore[arg-type]
    instruments = RecordingInstruments()
    genai = GenAI(trace.get_tracer("test"), instruments, "conductor")
    investigation = Investigation(
        incident=parse(grafana_payload())[0], still_failing=True
    )
    return signal, genai, instruments, investigation


def run(script, world, **kw) -> tuple[Conclusion, RecordingInstruments]:
    signal, genai, instruments, investigation = world
    conclusion = conduct(
        FakeClient(script), signal, investigation, genai, scene="S03", **kw
    )
    return conclusion, instruments


def good_proposal(**overrides) -> Call:
    args = {
        "strategy": "RETIME",
        "params_json": '{"shift_ms": -298.2}',
        "predicted_series": "dub_sync_offset_ms",
        "predicted_target": 102.0,
        "predicted_direction": "decrease",
        "evidence_queries": [SYNC, SHAPE, BAR],
        "rationale": "drift is uniform, so the stem is offset",
    }
    args.update(overrides)
    return Call(name="propose_repair", args=args, id="c1")


def gather() -> Call:
    return Call(name="query_metric", args={"expr": SYNC}, id="g1")


# ---------------------------------------------------------------------------
# The happy path, so the failures below mean something
# ---------------------------------------------------------------------------


def test_a_grounded_proposal_becomes_a_repair_intent(world):
    conclusion, instruments = run([
        turn(Call(name="list_failing_checks", args={}, id="a")),
        turn(gather(),
             Call(name="query_metric", args={"expr": SHAPE}, id="g2"),
             Call(name="get_threshold", args={"requirement": "dub_sync_max_ms"}, id="g3")),
        turn(good_proposal()),
    ], world)

    assert conclusion.acted
    assert conclusion.intent.strategy is Strategy.RETIME
    assert conclusion.intent.prediction.baseline == 298.2
    assert conclusion.intent.prediction.target_value == 102.0
    # every cited query is one the toolbox actually ran
    assert len(conclusion.intent.justification) == 3
    assert instruments.rejections() == {}
    assert instruments.tool_calls()["query_metric"] == 2


def test_tool_calls_and_tokens_are_measured(world):
    _, instruments = run([
        turn(Call(name="list_failing_checks", args={}, id="a")),
        turn(good_proposal(evidence_queries=[SYNC])),
    ], world)
    # the proposal cited SYNC, which list_failing_checks did not run ...
    assert "uncited_evidence" in instruments.rejections()
    assert instruments.tool_calls()["list_failing_checks"] == 1
    # token usage is a counter, not a gauge: tokens are additive, and a gauge
    # from a one-shot run goes stale five minutes later leaving the panel empty
    assert any(name == "gen_ai_client_token_usage"
               for name, _ in instruments.counters)


# ---------------------------------------------------------------------------
# The guardrails, against a model that misbehaves
# ---------------------------------------------------------------------------


def test_a_query_the_model_never_ran_is_refused(world):
    """The single most important check. A plausible-looking query is exactly
    what a model invents when it wants a conclusion to look grounded."""
    conclusion, instruments = run([
        turn(gather()),
        turn(good_proposal(evidence_queries=[
            'dub_sync_offset_ms{market="de-DE"}',      # never run: different text
        ])),
        turn(Call(name="escalate",
                  args={"reason": "gave up", "summary": "s"}, id="e")),
    ], world)

    assert instruments.rejections()["uncited_evidence"] == 1
    assert conclusion.action == "escalate"
    assert conclusion.rejections


def test_a_proposal_citing_nothing_is_refused(world):
    _, instruments = run([
        turn(gather()),
        turn(good_proposal(evidence_queries=[])),
        turn(Call(name="escalate", args={"reason": "r", "summary": "s"}, id="e")),
    ], world)
    assert instruments.rejections()["no_evidence"] == 1


def test_a_strategy_with_no_implementation_is_refused(world):
    """A strategy invented in a prompt has no code behind it and no measured
    history, which is exactly what the autonomy ladder exists to keep away
    from production assets."""
    _, instruments = run([
        turn(gather()),
        turn(good_proposal(strategy="RECONFORM_SUBS")),
        turn(Call(name="escalate", args={"reason": "r", "summary": "s"}, id="e")),
    ], world)
    assert instruments.rejections()["unexecutable_strategy"] == 1


def test_an_invented_strategy_is_refused(world):
    _, instruments = run([
        turn(gather()),
        turn(good_proposal(strategy="ENHANCE")),
        turn(Call(name="escalate", args={"reason": "r", "summary": "s"}, id="e")),
    ], world)
    assert instruments.rejections()["unknown_strategy"] == 1


def test_predicting_the_value_already_measured_is_refused(world):
    """How an agent restates the status quo as its goal and then declares the
    prediction held.

    Cites only the query this script actually ran, because the citation check
    runs first and would otherwise mask the invariant under test.
    """
    _, instruments = run([
        turn(gather()),
        turn(good_proposal(predicted_target=298.2, evidence_queries=[SYNC])),
        turn(Call(name="escalate", args={"reason": "r", "summary": "s"}, id="e")),
    ], world)
    assert instruments.rejections()["incoherent_prediction"] == 1


def test_unusable_parameters_are_refused(world):
    _, instruments = run([
        turn(gather()),
        turn(good_proposal(params_json="shift by a bit", evidence_queries=[SYNC])),
        turn(Call(name="escalate", args={"reason": "r", "summary": "s"}, id="e")),
    ], world)
    assert instruments.rejections()["bad_params"] == 1


def test_a_rejected_model_is_told_what_it_actually_ran(world):
    """Refusal without the correction is just a wall. The model gets one
    honest chance to fix its own proposal."""
    signal, genai, instruments, investigation = world
    client = FakeClient([
        turn(gather()),
        turn(good_proposal(evidence_queries=["made_up_query"])),
        turn(good_proposal(evidence_queries=[SYNC])),
    ])
    conclusion = conduct(client, signal, investigation, genai, scene="S03")

    assert conclusion.acted, "the corrected proposal should be accepted"
    nudge = str(client.models.sent[-1])
    assert "rejected" in nudge
    assert SYNC in nudge


# ---------------------------------------------------------------------------
# Knowing when to stop
# ---------------------------------------------------------------------------


def test_escalation_is_a_first_class_outcome(world):
    """Some blockers cannot be repaired by anyone here, and inventing a fix
    for them is worse than saying so."""
    conclusion, _ = run([
        turn(Call(name="list_failing_checks", args={}, id="a")),
        turn(Call(name="escalate", args={
            "reason": "rights_not_cleared",
            "summary": "No Japanese music-cue grant. No repair can create one.",
        }, id="e")),
    ], world)

    assert conclusion.action == "escalate"
    assert conclusion.reason == "rights_not_cleared"
    assert not conclusion.acted


def test_a_model_that_never_concludes_is_stopped(world):
    """Letting it run burns quota to arrive somewhere a human should already
    have been told about."""
    conclusion, _ = run([turn(gather()) for _ in range(20)], world,
                        max_turns=4)
    assert conclusion.action == "escalate"
    assert conclusion.reason == "not_converging"
    assert conclusion.turns == 4


def test_a_silent_model_is_nudged_not_looped_forever(world):
    conclusion, _ = run([turn(), turn(), turn()], world, max_turns=3)
    assert conclusion.reason == "not_converging"


# ---------------------------------------------------------------------------
# What the model is never given
# ---------------------------------------------------------------------------


def test_no_tool_exposes_the_verdict(world):
    """Grafana owns the answer. An agent that could read it might learn to
    optimise for it rather than for the content."""
    from agents.conductor import _declarations

    names = {d["name"] for d in _declarations()}
    assert "market_release_ready" not in " ".join(names)
    assert not any("verdict" in n or "ready" in n for n in names)


def test_no_tool_writes_anything(world):
    from agents.conductor import _declarations

    for declaration in _declarations():
        name = declaration["name"]
        assert not any(
            verb in name for verb in ("set_", "write", "update", "delete",
                                      "create", "publish")
        ), f"{name} looks like a write"


def test_authority_comes_from_the_ledger_not_the_model(world):
    """The tier is read from measured history before the model is consulted,
    and there is no tool that changes it."""
    signal, genai, instruments, investigation = world
    conclusion = conduct(
        FakeClient([turn(gather()),
                    turn(good_proposal(evidence_queries=[SYNC]))]),
        signal, investigation, genai, scene="S03",
    )
    # no repair history in the stub -> not enough evidence to trust it alone
    assert conclusion.intent.tier is AutonomyTier.RECOMMEND
    assert conclusion.intent.needs_human


def test_the_toolbox_remembers_only_queries_it_ran(world):
    signal, _, _, _ = world
    box = Toolbox(signal, title="SINTEL", market="de-DE")
    box.dispatch("query_metric", {"expr": SYNC})
    assert SYNC in box.gathered
    assert 'dub_sync_offset_ms{market="de-DE"}' not in box.gathered


def test_an_absent_series_is_reported_as_absent_not_zero(world):
    signal, _, _, _ = world
    box = Toolbox(signal, title="SINTEL", market="de-DE")
    result = box.dispatch("query_metric", {"expr": "nonexistent_metric"})
    assert result["value"] is None
    assert "absent is not zero" in result["note"]


def test_a_predicted_series_given_as_a_selector_is_normalised(world):
    """The model answers with what it just queried -- a full selector -- where
    a bare metric name is wanted. Verification maps the NAME to the probe that
    re-measures it, so a selector would leave the repair unverifiable. The
    metric name inside a selector is unambiguous, so it is normalised rather
    than refused; rejecting would burn a turn on a proposal that was right."""
    conclusion, instruments = run([
        turn(gather()),
        turn(good_proposal(predicted_series=SYNC, evidence_queries=[SYNC])),
    ], world)

    assert conclusion.acted
    assert conclusion.intent.prediction.series == "dub_sync_offset_ms"
    assert instruments.rejections() == {}
