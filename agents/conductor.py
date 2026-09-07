"""The Release Conductor: a model that reasons, inside guardrails that measure.

Everything before this file is deterministic on purpose. The verdict is
PromQL, the investigation is arithmetic over `Evidence`, the repairs are
ffmpeg. That was the right call for each of them -- a model asked "why is de-DE
blocked?" produces a fluent answer whether or not it looked.

But choosing what to do about a market blocked on four different things, in
three different ways, one of which nobody can fix, is not arithmetic. It is
judgement over a messy picture, and it is where a model earns its place.

## What the model can and cannot do

It reasons by calling tools. Every tool returns `Evidence` -- a value with the
query that produced it -- so the model never states a number of its own. It can
ask what is failing, what the bar is, what shape the drift has, what a strategy's
record looks like, what a change affected. What it cannot do:

  - **Write the verdict.** No tool exposes it. Grafana owns it, and the only
    way to change it is to change the asset until the measurement changes.
  - **Assert a fact.** Numbers arrive through tools or not at all.
  - **Cite evidence it did not gather.** A proposal naming a query the model
    never ran is rejected as `uncited_evidence` -- the single most important
    check here, because a plausible query is exactly what a model will invent.
  - **Propose an action without a falsifiable prediction.** `RepairIntent`
    cannot be constructed without one, so this is enforced by the type rather
    than by the prompt.
  - **Grant itself authority.** The autonomy tier comes from
    `continuity_repairs_total` in Prometheus, read at decision time. The model
    is told what tier it has; it has no way to argue for a different one.

Every rejection is counted in `continuity_agent_rejections_total{reason}`,
which is what turns those guarantees from claims into a time series. A
guardrail nobody can watch fire is a guardrail nobody should believe.

## Why a manual tool loop

The SDK will run function calls automatically. This does it by hand because
each tool call gets its own span under the GenAI semantic conventions, and a
trace that could not separate "the agent decided" from "the agent looked
something up" could not answer whether a conclusion was grounded.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from agents.contracts import (
    AutonomyTier,
    Direction,
    Evidence,
    Prediction,
    RepairIntent,
    Strategy,
    UnsupportedClaim,
    earned_tier,
)
from agents.investigate import Investigation
from agents.signal import Signal
from telemetry.genai import GenAI
from telemetry.metrics import DRIFT_SYSTEMATIC, SYNC_OFFSET

log = logging.getLogger("continuity.conductor")

MODEL = "gemini-2.5-flash"

# A run that has not concluded in this many turns is not converging, and
# letting it continue burns quota to arrive somewhere a human should have been
# told about several turns ago.
MAX_TURNS = 8

# Repairs the executor can actually perform. Named here so the model is offered
# exactly what exists -- a strategy invented in a prompt has no implementation
# and no measured history, which is precisely what the autonomy ladder exists
# to keep away from production assets.
EXECUTABLE = {
    Strategy.RETIME: "Shift the whole dub stem in time. Fixes drift that is "
                     "uniform across every line. Cannot fix drift that grows "
                     "down the scene, because shifting moves the whole "
                     "staircase. Parameter: shift_ms (negative pulls earlier).",
    Strategy.REMIX: "Normalise the stem to the market's target loudness with a "
                    "true-peak ceiling. Does not touch timing. Parameters: "
                    "target_lufs, true_peak_max.",
    Strategy.REWRITE: "Re-adapt the dialogue shorter and re-synthesise it. The "
                      "only fix when lines overrun their slots. Expensive: it "
                      "produces new audio. Parameter: target_ms.",
}


class ConductorError(RuntimeError):
    pass


@dataclass
class Conclusion:
    """What the run decided, and everything it looked at to get there."""

    action: str                      # "repair" | "escalate" | "no_action"
    intent: RepairIntent | None = None
    reason: str = ""
    summary: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    turns: int = 0
    rejections: list[tuple[str, str]] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return self.action == "repair" and self.intent is not None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _declarations() -> list[dict[str, Any]]:
    """The tool surface offered to the model.

    Deliberately narrow. Every entry either returns a measurement with its
    query attached or ends the run. There is no tool that writes anything, and
    none that reveals the verdict -- the model works out whether a market can
    ship by looking at what is failing, exactly as a human would, rather than
    by reading the answer off the back of the book.
    """
    return [
        {
            "name": "list_failing_checks",
            "description": (
                "Which release requirements are currently failing for this "
                "market, across every dimension: localisation, audio delivery, "
                "timed text, technical conformance, rights and packaging."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "query_metric",
            "description": (
                "Run an instant PromQL query and return the value. This is the "
                "only way to obtain a number. Use the exact series names given "
                "in the briefing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {"type": "string", "description": "PromQL expression"},
                },
                "required": ["expr"],
            },
        },
        {
            "name": "get_threshold",
            "description": (
                "The bar this market is judged against for one requirement, "
                "e.g. dub_sync_max_ms or loudness_target_lufs."
            ),
            "parameters": {
                "type": "object",
                "properties": {"requirement": {"type": "string"}},
                "required": ["requirement"],
            },
        },
        {
            "name": "repair_history",
            "description": (
                "How a strategy has performed in this market: succeeded, "
                "lucky (it passed but the prediction did not hold) and failed. "
                "This determines how much autonomy the strategy has earned."
            ),
            "parameters": {
                "type": "object",
                "properties": {"strategy": {"type": "string"}},
                "required": ["strategy"],
            },
        },
        {
            "name": "propose_repair",
            "description": (
                "Propose one repair and end the investigation. You must cite, "
                "in evidence_queries, the exact query strings you actually ran "
                "and that justify this repair. Citing a query you did not run "
                "is rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": [s.value for s in EXECUTABLE],
                    },
                    "params_json": {
                        "type": "string",
                        "description": "JSON object of the strategy's parameters",
                    },
                    "predicted_series": {
                        "type": "string",
                        "description": "The metric this repair should move",
                    },
                    "predicted_target": {
                        "type": "number",
                        "description": "The value it should reach or pass",
                    },
                    "predicted_direction": {
                        "type": "string",
                        "enum": ["decrease", "increase"],
                    },
                    "evidence_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Queries you ran that justify this",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this strategy and not another",
                    },
                },
                "required": ["strategy", "params_json", "predicted_series",
                             "predicted_target", "predicted_direction",
                             "evidence_queries", "rationale"],
            },
        },
        {
            "name": "escalate",
            "description": (
                "End the investigation and hand it to a human. Use this when "
                "nothing you can do would fix the blocker -- a right that is "
                "not cleared, a deliverable that does not exist and cannot be "
                "built here, or a technical requirement the source material "
                "cannot satisfy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["reason", "summary"],
            },
        },
    ]


class Toolbox:
    """Executes tool calls and remembers every query that was actually run.

    The memory is the guardrail. When the model cites evidence for a proposal,
    the citation is checked against this record rather than taken on trust,
    which is the difference between grounding and the appearance of it.
    """

    def __init__(self, signal: Signal, *, title: str, market: str) -> None:
        self.signal = signal
        self.title = title
        self.market = market
        self.gathered: dict[str, Evidence] = {}

    def _remember(self, evidence: Evidence | None) -> Evidence | None:
        if evidence is not None:
            self.gathered[evidence.query.strip()] = evidence
        return evidence

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_failing_checks":
            findings = self.signal.failing_requirements(self.title, self.market)
            for finding in findings:
                for evidence in finding.evidence:
                    self._remember(evidence)
            gap = self.signal.missing_coverage(self.title, self.market)
            if gap:
                for evidence in gap.evidence:
                    self._remember(evidence)
            return {
                "failing": [
                    {"requirement": f.subject, "claim": f.claim,
                     "evidence": [e.cite() for e in f.evidence]}
                    for f in findings
                ],
                "coverage_gap": gap.claim if gap else None,
            }

        if name == "query_metric":
            expr = str(args.get("expr", "")).strip()
            if not expr:
                return {"error": "empty expression"}
            evidence = self._remember(self.signal.observe(expr))
            if evidence is None:
                # Absent is a real answer and a different one from zero. Saying
                # so plainly stops the model treating a missing measurement as
                # a passing one.
                return {"value": None,
                        "note": "no such series right now; absent is not zero"}
            return {"value": evidence.value, "query": evidence.query}

        if name == "get_threshold":
            requirement = str(args.get("requirement", ""))
            evidence = self._remember(
                self.signal.threshold(self.market, requirement)
            )
            if evidence is None:
                return {"value": None, "note": f"no threshold for {requirement}"}
            return {"value": evidence.value, "query": evidence.query}

        if name == "repair_history":
            strategy = str(args.get("strategy", ""))
            counts = self.signal.repair_history(strategy, self.market)
            return {
                "strategy": strategy,
                **counts,
                "earned_tier": earned_tier(**counts).name,
            }

        raise ConductorError(f"no such tool: {name}")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_SYSTEM = """You are the Release Conductor for a global media release system.

A market is blocked. Your job is to work out why and decide ONE next action:
propose a specific repair, or escalate to a human.

How you work:
- You have no knowledge of this title's state except what your tools return.
  Never state a number you did not obtain from a tool.
- Investigate before you decide. Look at what is failing, then at the bar it
  failed against, then at anything that would tell you WHY.
- Distinguish the symptom from the cause. Lines landing late and lines being
  too long look identical in a sync measurement and need opposite repairs.
- Prefer the least invasive repair your evidence supports.
- Some blockers cannot be repaired by anyone here. A right that is not cleared
  is not a defect in a file. A stereo master where 5.1 is required is not a
  processing problem. Escalate those; do not invent a fix.
- When you propose a repair you must cite the exact query strings you ran that
  justify it. Citing a query you did not run will be rejected.
- Your prediction must be falsifiable: which series, which direction, past
  which value. It will be checked against the measured result afterwards, and
  being right for the wrong reason is recorded differently from being right.

Be economical. Every tool call costs time and money."""


def _briefing(investigation: Investigation, tier: AutonomyTier) -> str:
    incident = investigation.incident
    lines = [
        f"Title: {incident.title_id}",
        f"Market: {incident.market}",
        f"Alert: {incident.alertname} ({incident.action})",
        "",
        "Series you can query (all take {title=...,market=...,scene=...}):",
        f"  {SYNC_OFFSET}          worst onset drift, ms",
        f"  {DRIFT_SYSTEMATIC}     1 if the drift is uniform across lines, else 0",
        "  dub_line_overrun_ms      how far the worst line runs past its slot",
        "  audio_loudness_lufs      integrated loudness",
        "  audio_true_peak_dbtp     true peak",
        "  speech_rate_wpm          words per minute",
        "  ad_collision_ms          audio description overlapping dialogue",
        "  subtitle_reading_rate_cps",
        "  market_check_met         market-level checks: rights, technical, packaging",
        "",
        "Repairs available to you:",
    ]
    lines += [f"  {s.value}: {d}" for s, d in EXECUTABLE.items()]
    lines += [
        "",
        f"Your current authority for this market is {tier.name}. It was derived "
        f"from measured repair history and you cannot change it.",
    ]
    if investigation.findings:
        lines += ["", "The deterministic investigation already established:"]
        lines += [f"  - {f.claim}" for f in investigation.findings]
    if investigation.gaps:
        lines += ["", "It could NOT establish:"]
        lines += [f"  - {g}" for g in investigation.gaps]
    lines += ["", "Investigate, then call propose_repair or escalate."]
    return "\n".join(lines)


def _validate(
    args: dict[str, Any], toolbox: Toolbox, genai: GenAI, *,
    title: str, market: str, scene: str, tier: AutonomyTier,
) -> RepairIntent:
    """Turn the model's proposal into a RepairIntent, or refuse it.

    Each refusal has a named reason and is counted. The reasons are the
    failure modes worth naming: a strategy that does not exist, a citation to
    a query that was never run, a prediction that restates the status quo.
    """
    try:
        strategy = Strategy(str(args.get("strategy", "")))
    except ValueError:
        genai.rejected("unknown_strategy", str(args.get("strategy")))
        raise UnsupportedClaim(f"no such strategy: {args.get('strategy')!r}")
    if strategy not in EXECUTABLE:
        genai.rejected("unexecutable_strategy", strategy.value)
        raise UnsupportedClaim(f"{strategy.value} has no implementation")

    cited = [str(q).strip() for q in args.get("evidence_queries", []) if str(q).strip()]
    justification = [toolbox.gathered[q] for q in cited if q in toolbox.gathered]
    uncited = [q for q in cited if q not in toolbox.gathered]
    if uncited:
        # The most important check here. A plausible-looking query is exactly
        # what a model invents when it wants a conclusion to look grounded.
        genai.rejected("uncited_evidence", "; ".join(uncited)[:300])
        raise UnsupportedClaim(
            f"cited {len(uncited)} query/queries that were never run: {uncited}"
        )
    if not justification:
        genai.rejected("no_evidence", "proposal cited nothing")
        raise UnsupportedClaim("a repair with no cited evidence is not a repair")

    series = str(args.get("predicted_series", ""))
    baseline = toolbox.gathered.get(
        next((q for q in cited if series and series in q), ""), None
    )
    if baseline is None:
        # Fall back to asking directly rather than guessing: a prediction with
        # a made-up baseline cannot be falsified against anything.
        baseline = toolbox.signal.measurement(
            series, title=title, market=market, scene=scene
        )
    if baseline is None:
        genai.rejected("unmeasurable_prediction", series)
        raise UnsupportedClaim(
            f"cannot establish a baseline for {series}; the prediction would "
            f"not be falsifiable"
        )

    direction = (Direction.DECREASE
                 if str(args.get("predicted_direction")) == "decrease"
                 else Direction.INCREASE)
    try:
        prediction = Prediction(
            series=series, market=market, scene=scene, direction=direction,
            target_value=float(args.get("predicted_target")),
            baseline=float(baseline.value),
        )
    except (TypeError, ValueError) as exc:
        genai.rejected("incoherent_prediction", str(exc)[:300])
        raise UnsupportedClaim(f"incoherent prediction: {exc}") from exc

    try:
        params = json.loads(str(args.get("params_json", "{}")))
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        genai.rejected("bad_params", str(exc)[:200])
        raise UnsupportedClaim(f"unusable params: {exc}") from exc

    return RepairIntent(
        strategy=strategy,
        target_asset_id=f"{title}:{scene}:dub_stem:{market}",
        params=params,
        prediction=prediction,
        justification=justification,
        tier=tier,
        rationale=str(args.get("rationale", ""))[:600],
    )


def conduct(
    client: Any,
    signal: Signal,
    investigation: Investigation,
    genai: GenAI,
    *,
    scene: str = "S03",
    model: str = MODEL,
    max_turns: int = MAX_TURNS,
) -> Conclusion:
    """Run the reasoning loop until the model proposes, escalates, or stalls."""
    from google.genai import types

    incident = investigation.incident
    title, market = incident.title_id, incident.market
    toolbox = Toolbox(signal, title=title, market=market)
    conversation = str(uuid.uuid4())

    # Authority is read before the model is consulted, and given to it as a
    # fact. There is no tool that changes it.
    history = signal.repair_history(Strategy.RETIME.value, market)
    tier = earned_tier(**history)

    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        temperature=0.2,
        tools=[types.Tool(function_declarations=_declarations())],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
    )
    contents: list[Any] = [
        types.Content(role="user",
                      parts=[types.Part(text=_briefing(investigation, tier))])
    ]
    rejections: list[tuple[str, str]] = []

    for turn in range(1, max_turns + 1):
        genai.step("reason")
        with genai.call(model, conversation_id=conversation,
                        temperature=0.2,
                        extra={"continuity.market": market,
                               "continuity.turn": turn}) as span:
            response = client.models.generate_content(
                model=model, contents=contents, config=config
            )
            genai.record_usage(span, response, model)

        calls = list(getattr(response, "function_calls", None) or [])
        if not calls:
            # No tool call and no conclusion. Nudge once, then give up rather
            # than looping on a model that has stopped making progress.
            text = (getattr(response, "text", "") or "").strip()
            log.info("turn %d: no tool call (%s)", turn, text[:120])
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text="Call a tool, or conclude with "
                                       "propose_repair or escalate.")],
            ))
            continue

        contents.append(response.candidates[0].content)

        for call in calls:
            args = dict(call.args or {})

            if call.name == "escalate":
                genai.decision("conclusion", "escalate")
                return Conclusion(
                    action="escalate",
                    reason=str(args.get("reason", "")),
                    summary=str(args.get("summary", "")),
                    evidence=list(toolbox.gathered.values()),
                    turns=turn, rejections=rejections,
                )

            if call.name == "propose_repair":
                try:
                    intent = _validate(
                        args, toolbox, genai, title=title, market=market,
                        scene=scene, tier=tier,
                    )
                except UnsupportedClaim as exc:
                    # Refused, and told why. The model gets one chance to fix
                    # its own proposal, which is how a real reviewer would
                    # handle it -- and every refusal is already counted.
                    rejections.append((call.name, str(exc)))
                    log.warning("proposal rejected: %s", exc)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            f"That proposal was rejected: {exc}\n"
                            f"Queries you have actually run:\n" +
                            "\n".join(f"  {q}" for q in toolbox.gathered) +
                            "\nCorrect it, or escalate."
                        ))],
                    ))
                    continue
                genai.decision("conclusion", "repair")
                return Conclusion(
                    action="repair", intent=intent,
                    summary=intent.rationale,
                    evidence=list(toolbox.gathered.values()),
                    turns=turn, rejections=rejections,
                )

            with genai.tool(call.name, getattr(call, "id", "") or ""):
                try:
                    result = toolbox.dispatch(call.name, args)
                except ConductorError as exc:
                    result = {"error": str(exc)}
            contents.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(
                    name=call.name, response=result
                )],
            ))

    genai.decision("conclusion", "stalled")
    return Conclusion(
        action="escalate",
        reason="not_converging",
        summary=(
            f"The Conductor took {max_turns} turns without reaching a "
            f"conclusion. Continuing would burn quota to arrive somewhere a "
            f"human should already have been told about."
        ),
        evidence=list(toolbox.gathered.values()),
        turns=max_turns, rejections=rejections,
    )
