"""The Conductor as an ADK agent, deployable to Agent Engine.

Same tools, same guardrails, same contracts. What changes is who runs the loop:
the Agent Development Kit owns the turn-taking, the session state and the tool
dispatch, and this module supplies the tools and the rules about what the model
is allowed to conclude.

That division is the point, and it is worth being explicit because "we used
ADK" is easy to say and easy to mean nothing by. ADK is the runtime. The
authority is still `agents/contracts.py`:

  - a tool returns `Evidence` or nothing, so the model cannot state a number
  - `propose_repair` is validated before it becomes a `RepairIntent`, and a
    proposal citing a query the model never ran is rejected as
    `uncited_evidence`
  - the autonomy tier is read from Prometheus before the agent is consulted

An ADK agent with those tools is safe in the same way the hand-rolled loop was.
An ADK agent without them would be a chatbot with a nicer runtime.

## Why both loops exist

`agents/conductor.py` runs the tool loop by hand. It is not dead code and it is
not a rehearsal: it was written first, it is what the 17 guardrail tests drive
with a model scripted turn by turn, and it is the reference implementation the
contracts were designed against. This module is the production path -- ADK
gives session management, streaming, callbacks and a deployment target that a
hand-rolled loop would have to grow itself.

They share the Toolbox, so there is one implementation of every question the
agent can ask, and no way for the two to disagree about what the answer was.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents.conductor import EXECUTABLE, Toolbox, _validate
from agents.contracts import AutonomyTier, RepairIntent, Strategy, UnsupportedClaim, earned_tier
from agents.investigate import Investigation
from agents.signal import Signal
from telemetry.genai import GenAI
from telemetry.metrics import DRIFT_SYSTEMATIC, SYNC_OFFSET

log = logging.getLogger("continuity.adk")

INSTRUCTION = """You are the Release Conductor for a global media release system.

A market is blocked, often by several things at once. Work out why and decide
ONE next action: propose a specific repair, or escalate to a human.

How you work:
- You have no knowledge of this title's state except what your tools return.
  Never state a number you did not obtain from a tool.
- Investigate before you decide. Look at what is failing, then at the bar it
  failed against, then at anything that would tell you WHY.
- Distinguish the symptom from the cause. Lines landing late and lines being
  too long look identical in a sync measurement and need opposite repairs.
- Repair the most consequential blocker you CAN fix, even when others remain.
  The loop runs again after every repair. Escalating because one blocker is
  unrepairable abandons the ones that were not.
- Some blockers cannot be repaired by anyone here. A right that is not cleared
  is not a defect in a file. A stereo master where 5.1 is required is not a
  processing problem. Never invent a fix for those.
- When you propose a repair you must cite the exact query strings you ran that
  justify it. Citing a query you did not run will be rejected.
- Your prediction must be falsifiable: which series, which direction, past
  which value. It is checked against the measured result afterwards.

You may call several tools in one turn, and you should. Gather what you need in
two or three turns, then conclude."""


class Session:
    """One incident's worth of agent state.

    Holds the Toolbox -- and therefore the record of which queries were
    actually run -- so the ADK tool functions can close over it. ADK tools are
    plain callables, so the alternative would be module-level state, which
    would make two concurrent incidents share an evidence record and let one
    market's proposal cite the other's queries.
    """

    def __init__(self, signal: Signal, investigation: Investigation,
                 genai: GenAI, *, scene: str) -> None:
        self.signal = signal
        self.investigation = investigation
        self.genai = genai
        self.scene = scene
        self.title = investigation.incident.title_id
        self.market = investigation.incident.market
        self.toolbox = Toolbox(signal, title=self.title, market=self.market)
        self.tier = earned_tier(
            **signal.repair_history(Strategy.RETIME.value, self.market)
        )
        self.intent: RepairIntent | None = None
        self.escalation: dict[str, str] | None = None
        self.rejections: list[str] = []

    # -- the tools, as plain functions ADK can introspect ------------------

    def list_failing_checks(self) -> dict:
        """List every release requirement currently failing for this market,
        across localisation, audio delivery, timed text, technical
        conformance, rights, certification and packaging."""
        with self.genai.tool("list_failing_checks"):
            return self.toolbox.dispatch("list_failing_checks", {})

    def query_metric(self, expr: str) -> dict:
        """Run an instant PromQL query and return its value. This is the only
        way to obtain a number.

        Args:
            expr: the PromQL expression to evaluate.
        """
        with self.genai.tool("query_metric"):
            return self.toolbox.dispatch("query_metric", {"expr": expr})

    def get_threshold(self, requirement: str) -> dict:
        """The bar this market is judged against for one requirement.

        Args:
            requirement: e.g. dub_sync_max_ms or loudness_target_lufs.
        """
        with self.genai.tool("get_threshold"):
            return self.toolbox.dispatch("get_threshold",
                                         {"requirement": requirement})

    def repair_history(self, strategy: str) -> dict:
        """How a strategy has performed in this market: succeeded, lucky (it
        passed but the prediction did not hold) and failed. This determines
        how much autonomy the strategy has earned.

        Args:
            strategy: RETIME, REMIX or REWRITE.
        """
        with self.genai.tool("repair_history"):
            return self.toolbox.dispatch("repair_history", {"strategy": strategy})

    def propose_repair(
        self, strategy: str, params_json: str, predicted_series: str,
        predicted_target: float, predicted_direction: str,
        evidence_queries: list[str], rationale: str,
    ) -> dict:
        """Propose one repair and end the investigation.

        Args:
            strategy: RETIME, REMIX or REWRITE.
            params_json: JSON object of the strategy's parameters.
            predicted_series: the metric this repair should move.
            predicted_target: the value it should reach or pass.
            predicted_direction: decrease or increase.
            evidence_queries: the exact query strings you ran that justify
                this. Citing one you did not run is rejected.
            rationale: why this strategy and not another.
        """
        args = {
            "strategy": strategy, "params_json": params_json,
            "predicted_series": predicted_series,
            "predicted_target": predicted_target,
            "predicted_direction": predicted_direction,
            "evidence_queries": list(evidence_queries), "rationale": rationale,
        }
        try:
            self.intent = _validate(
                args, self.toolbox, self.genai, title=self.title,
                market=self.market, scene=self.scene, tier=self.tier,
            )
        except UnsupportedClaim as exc:
            # Refused, and told why, with the queries it actually ran. A wall
            # without the correction just produces the same proposal again.
            self.rejections.append(str(exc))
            log.warning("proposal rejected: %s", exc)
            return {
                "accepted": False, "reason": str(exc),
                "queries_you_actually_ran": sorted(self.toolbox.gathered),
                "next": "correct the proposal, or escalate",
            }
        self.genai.decision("conclusion", "repair")
        return {
            "accepted": True,
            "strategy": self.intent.strategy.value,
            "authority": self.intent.tier.name,
            "prediction": self.intent.prediction.describe(),
        }

    def escalate(self, reason: str, summary: str) -> dict:
        """End the investigation and hand it to a human. Use this when nothing
        you can do would fix the blocker.

        Args:
            reason: a short machine-readable reason, e.g. rights_not_cleared.
            summary: what a human needs to decide.
        """
        self.escalation = {"reason": reason, "summary": summary}
        self.genai.decision("conclusion", "escalate")
        return {"accepted": True, "escalated": reason}

    # -- the agent ---------------------------------------------------------

    def briefing(self) -> str:
        incident = self.investigation.incident
        lines = [
            f"Title: {incident.title_id}",
            f"Market: {incident.market}",
            f"Alert: {incident.alertname} ({incident.action})",
            "",
            "Series you can query (all take {title=...,market=...,scene=...}):",
            f"  {SYNC_OFFSET}          worst onset drift, ms",
            f"  {DRIFT_SYSTEMATIC}     1 if drift is uniform across lines, else 0",
            "  dub_line_overrun_ms      how far the worst line runs past its slot",
            "  audio_loudness_lufs      integrated loudness",
            "  audio_true_peak_dbtp     true peak",
            "  speech_rate_wpm          words per minute",
            "  ad_collision_ms          audio description overlapping dialogue",
            "  subtitle_reading_rate_cps",
            "  market_check_met         rights, certification, technical, packaging",
            "",
            "Repairs available to you:",
        ]
        lines += [f"  {s.value}: {d}" for s, d in EXECUTABLE.items()]
        lines += [
            "",
            f"Your authority for this market is {self.tier.name}, derived from "
            f"measured repair history. You cannot change it.",
        ]
        if self.investigation.findings:
            lines += ["", "The deterministic investigation already established:"]
            lines += [f"  - {f.claim}" for f in self.investigation.findings]
        if self.investigation.gaps:
            lines += ["", "It could NOT establish:"]
            lines += [f"  - {g}" for g in self.investigation.gaps]
        lines += ["", "Investigate, then call propose_repair or escalate."]
        return "\n".join(lines)

    def agent(self, model: str = "") -> Any:
        """The ADK agent, with these tools bound to this incident."""
        from google.adk.agents import LlmAgent

        from media.model import configure_environment, model_for

        # ADK builds its own genai client from the process environment, so the
        # backend choice has to be exported before the agent is constructed.
        configure_environment()
        return LlmAgent(
            name="release_conductor",
            model=model or model_for("text"),
            description=(
                "Diagnoses why a market cannot ship and proposes one repair, "
                "grounded in Grafana measurements."
            ),
            instruction=INSTRUCTION,
            tools=[
                self.list_failing_checks, self.query_metric,
                self.get_threshold, self.repair_history,
                self.propose_repair, self.escalate,
            ],
        )


async def conduct_adk(
    signal: Signal, investigation: Investigation, genai: GenAI,
    *, scene: str = "S03", model: str = "", max_turns: int = 8,
) -> Session:
    """Run the ADK agent over one incident and return its session.

    Returns the Session rather than a Conclusion because the session carries
    everything the caller needs -- the validated intent, the escalation, the
    rejections, and the evidence the toolbox actually gathered.
    """
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    session = Session(signal, investigation, genai, scene=scene)
    runner = InMemoryRunner(agent=session.agent(model), app_name="continuity")
    adk_session = await runner.session_service.create_session(
        app_name="continuity", user_id="conductor"
    )

    genai.step("reason")
    events = runner.run_async(
        user_id="conductor",
        session_id=adk_session.id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=session.briefing())]
        ),
    )
    seen = 0
    try:
        async for _event in events:
            seen += 1
            if session.intent is not None or session.escalation is not None:
                break
            if seen > max_turns * 4:   # events, not turns; a generous ceiling
                log.warning("ADK run did not converge after %d events", seen)
                session.escalation = {
                    "reason": "not_converging",
                    "summary": (
                        "The agent produced no conclusion within its event "
                        "budget. Continuing would burn quota to arrive "
                        "somewhere a human should already have been told "
                        "about."
                    ),
                }
                genai.decision("conclusion", "stalled")
                break
    finally:
        # Leaving an async generator to be garbage-collected mid-iteration
        # raises GeneratorExit through ADK's own frames and prints a traceback
        # over a run that actually succeeded. Closing it is not optional
        # tidiness; an operator reading that output would think it failed.
        await events.aclose()
    return session
