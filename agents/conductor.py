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
from pathlib import Path
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
from agents.specialists import ALL_TOOLS, Specialist
from agents.signal import Signal
from media.dub.quota import DailyQuotaExhausted
from media.dub.quota import call as quota_call
from telemetry.genai import GenAI
from telemetry.metrics import DRIFT_SYSTEMATIC, SYNC_OFFSET, SYNC_SIGNED

log = logging.getLogger("continuity.conductor")

# Where the asset index lives. The blast-radius tool reads lineage from
# disk rather than from traces, because an empty trace search means "not
# observed lately" and would read as "nothing depends on this".
_STORE_ROOT = Path(__file__).resolve().parents[1] / "out" / "store"

# Resolved per backend by media/model.py. On Vertex this is gemini-2.5-flash,
# billed to the project and free of the consumer tier's per-day cap that used
# to stop a repair being planned because the dub had spent the day.
def _default_model() -> str:
    from media.model import model_for
    return model_for("text")

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


def _declarations(specialist: Specialist | None = None) -> list[dict[str, Any]]:
    """The tool surface offered to the model.

    Deliberately narrow. Every entry either returns a measurement with its
    query attached or ends the run. There is no tool that writes anything, and
    none that reveals the verdict -- the model works out whether a market can
    ship by looking at what is failing, exactly as a human would, rather than
    by reading the answer off the back of the book.

    A `specialist` narrows it further, and that narrowing is an authority
    boundary rather than a convenience. The compliance specialist is not given
    `propose_repair`, so the function is absent from the protocol it speaks:
    it cannot invent a fix for an uncleared right because there is no call it
    could make that would express one. Prompts can be argued with. A missing
    function cannot.

    The strategy enum is narrowed the same way, so an agent that may only
    REMIX cannot name RETIME even in a malformed call.
    """
    allowed = set(specialist.tools) if specialist else set(ALL_TOOLS)
    strategies = (sorted(s.value for s in specialist.strategies)
                  if specialist else [s.value for s in EXECUTABLE])
    return [
        declaration for declaration in _all_declarations(strategies)
        if declaration["name"] in allowed
    ]


def _all_declarations(strategies: list[str]) -> list[dict[str, Any]]:
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
            "name": "metric_history",
            "description": (
                "The same PromQL query over a window, summarised: first, last, "
                "min, max and whether it changed. Answers 'since when' and "
                "'was it ever good', which an instant read cannot. Use it to "
                "tell a fresh regression from a standing condition, and to "
                "check whether a previous repair actually held."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {"type": "string",
                             "description": "PromQL expression"},
                    "hours": {"type": "integer",
                              "description": "window, 1-24, default 6"},
                },
                "required": ["expr"],
            },
        },
        {
            "name": "blast_radius",
            "description": (
                "What repairing each asset in this market would invalidate "
                "downstream. Read this BEFORE proposing: a repair gives the "
                "asset a new hash, and anything built from it goes stale and "
                "must be rebuilt before the market can ship. A proposal that "
                "ignores its own consequences is half a plan."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "compare_markets",
            "description": (
                "The same series across every market, so you can see whether "
                "this failure is peculiar to this market or common to all of "
                "them. A fault every market shares usually means the source "
                "or the spec, not the localisation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "series": {"type": "string",
                               "description": "metric name, e.g. dub_sync_offset_ms"},
                },
                "required": ["series"],
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
                        "enum": strategies,
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
                # Say what WOULD have worked. A bare "not found" costs a whole
                # turn to a near-miss like `ad_collision_ms` for
                # `ad_collision_max_ms`, and turns are the budget.
                available = sorted(
                    e.detail.get("requirement", "")
                    for e in self.signal.observe_all(
                        f'market_threshold{{market="{self.market}"}}'
                    )
                )
                return {
                    "value": None,
                    "note": f"no threshold named {requirement!r}",
                    "available": [a for a in available if a],
                }
            return {"value": evidence.value, "query": evidence.query}

        if name == "repair_history":
            strategy = str(args.get("strategy", ""))
            counts = self.signal.repair_history(strategy, self.market)
            return {
                "strategy": strategy,
                **counts,
                "earned_tier": earned_tier(**counts).name,
            }

        if name == "metric_history":
            # "Since when" is a different question from "is it broken", and
            # usually the more useful one. A market red for four minutes is an
            # incident; one red all day is a plan. Nothing in an instant query
            # separates them, which is why an agent working only from `now`
            # cannot tell a regression from a standing condition.
            expr = str(args.get("expr", "")).strip()
            if not expr:
                return {"error": "empty expression"}
            hours = max(1, min(24, int(args.get("hours", 6) or 6)))
            series = self.signal.history(expr, since=f"now-{hours}h",
                                         step_s=max(60, hours * 3600 // 60))
            if not series:
                return {"points": [], "note": "no samples in this window"}
            if len(series) > 1:
                # More than one series matched, and summarising an arbitrary
                # one is how an agent ends up reasoning from a number that is
                # not the number it asked for. Caught in exactly that way: an
                # instant read said 17.4 ms while this reported 273 flat,
                # because two exporter instances had both published over the
                # window and `series[0]` was the stale one.
                #
                # So the ambiguity is returned rather than resolved. The agent
                # can narrow the query; this tool cannot know which of them it
                # meant.
                return {
                    "query": expr,
                    "ambiguous": True,
                    "matched": [
                        {"labels": {k: v for k, v in s["labels"].items()
                                    if k != "__name__"},
                         "last": s["points"][-1][1]}
                        for s in series
                    ],
                    "note": (
                        "this expression matches several series. They usually "
                        "differ only by `instance`, because every exporter "
                        "restart publishes under a new one -- that is not "
                        "several measurements, it is one measurement reported "
                        "by successive processes. Aggregate away the "
                        "difference: "
                        "max by (title, market, scene) (<your expression>)"
                    ),
                }
            points = series[0]["points"]
            values = [v for _t, v in points]
            # Remembered as evidence: a history is as citable as an instant
            # read, and a proposal reasoning from a trend should be able to
            # say so.
            self._remember(Evidence(
                kind="metric", query=expr, value=values[-1],
                source=self.signal.uid, detail={"window_hours": hours},
            ))
            return {
                "query": expr, "window_hours": hours, "samples": len(values),
                "first": values[0], "last": values[-1],
                "min": min(values), "max": max(values),
                "changed": values[0] != values[-1],
            }

        if name == "blast_radius":
            # What a repair here would invalidate. An agent proposing to
            # rewrite the dub without knowing the package is built from it is
            # proposing half a plan, and the half it left out is the one that
            # puts the market back to blocked.
            return self._blast_radius()

        if name == "compare_markets":
            # How other markets fare on the same requirement, and what fixed
            # it where it is fixed. This is how a human specialist actually
            # works -- "we saw this in France last week" -- and it is nearly
            # free, because the series are already published per market.
            requirement = str(args.get("series", "")).strip()
            if not requirement:
                return {"error": "name the series to compare"}
            others = self.signal.observe_all(
                f'{requirement}{{title="{self.title}"}}')
            if not others:
                return {"markets": [], "note": "no market publishes this"}
            for evidence in others:
                self._remember(evidence)
            rows = sorted(
                ({"market": e.detail.get("market", ""), "value": e.value}
                 for e in others),
                key=lambda r: r["market"],
            )
            return {"series": requirement, "markets": rows,
                    "this_market": self.market}

        raise ConductorError(f"no such tool: {name}")

    def _blast_radius(self) -> dict[str, Any]:
        """Downstream dependants of this market's assets, from the index.

        Read from the store rather than from traces. `Signal.blast_radius`
        answers the same question with a TraceQL search, and for corroboration
        that is the right tool -- but traces are sampled and expire, so an
        empty answer there means "not observed lately" and would read here as
        "nothing depends on this". That is the most dangerous wrong answer to
        the question "what will my repair break".
        """
        from media.store import Store

        try:
            store = Store(_STORE_ROOT)
            assets = store.all_assets()
        except OSError:
            return {"error": "no local asset index available"}

        children: dict[str, list[str]] = {}
        for asset in assets:
            for parent in asset.parents:
                if parent.asset_id == asset.id:
                    continue          # a repair records its own predecessor
                children.setdefault(parent.asset_id, []).append(asset.id)

        out = []
        for asset in assets:
            if asset.market != self.market:
                continue
            downstream = sorted(set(children.get(asset.id, [])))
            if downstream:
                out.append({"asset": asset.id, "kind": asset.kind,
                            "would_invalidate": downstream})
        return {
            "market": self.market,
            "dependencies": sorted(out, key=lambda r: -len(r["would_invalidate"])),
            "note": ("Repairing an asset gives it a new hash, so everything "
                     "listed under it becomes stale until rebuilt."),
        }


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_SYSTEM = """You are the Release Conductor for a global media release system.

A market is blocked, often by several things at once. Your job is to work out
why and decide ONE next action: propose a specific repair, or escalate.

Repair the most consequential blocker you CAN fix, even when others remain.
The loop runs again after every repair, so fixing three of four blockers is
progress and the alert will bring you back for the fourth. Escalating because
one blocker is unrepairable abandons the three that were not -- that is the
most common way this goes wrong, so check yourself against it before you
escalate. Escalate only when NOTHING you could do would move the market
forward.

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
  processing problem. Never invent a fix for those -- but if something else in
  the same market IS repairable, repair that and let the alert bring you back.
- When you propose a repair you must cite the exact query strings you ran that
  justify it. Citing a query you did not run will be rejected.
- Your prediction must be falsifiable: which series, which direction, past
  which value. It will be checked against the measured result afterwards, and
  being right for the wrong reason is recorded differently from being right.

Be economical, and be quick. You may request SEVERAL tools in one turn and
you should -- asking one question per turn wastes your budget on round trips
rather than on thinking. Gather what you need in two or three turns, then
conclude."""


def _system_for(specialist: Specialist | None) -> str:
    """The system instruction, general or specialised.

    A specialist gets its own brief in place of the Conductor's triage
    instructions, because "repair the most consequential blocker you CAN fix"
    is advice for an agent looking at the whole market and actively misleading
    for one looking at a single dimension. What carries over is the part that
    is true of every agent here: numbers come from tools, citations are
    checked, predictions are falsifiable.
    """
    if specialist is None:
        return _SYSTEM
    return specialist.brief + "\n\n" + _COMMON


_COMMON = """How you work, whatever your dimension:
- You have no knowledge of this title's state except what your tools return.
  Never state a number you did not obtain from a tool.
- Investigate before you decide. Look at what is failing, then at the bar it
  failed against, then at anything that would tell you WHY.
- If you propose a repair you must cite the exact query strings you ran that
  justify it. Citing a query you did not run will be rejected.
- Your prediction must be falsifiable: which series, which direction, past
  which value. It will be checked against the measured result afterwards, and
  being right for the wrong reason is recorded differently from being right.
- Stay inside your dimension. If the real problem belongs to another
  specialist, say so and escalate rather than reaching across -- somebody else
  is already looking at it.

You may request SEVERAL tools in one turn and you should. Gather what you need
in two or three turns, then conclude."""


def _briefing(investigation: Investigation, tier: AutonomyTier, *,
              specialist: Specialist | None = None,
              focus: list[str] | None = None) -> str:
    incident = investigation.incident
    lines = [
        f"Title: {incident.title_id}",
        f"Market: {incident.market}",
        f"Alert: {incident.alertname} ({incident.action})",
        "",
        "Series you can query (all take {title=...,market=...,scene=...}):",
        f"  {SYNC_OFFSET}          worst onset drift, ms",
        f"  {DRIFT_SYSTEMATIC}     1 if the drift is uniform across lines, else 0",
        f"  {SYNC_SIGNED}        SIGNED mean drift, ms. Positive means the dub arrives LATE.",
        "                           dub_sync_offset_ms is a magnitude and does NOT say which",
        "                           way. A RETIME shift must be the NEGATIVE of this value.",
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
    available = ({s: d for s, d in EXECUTABLE.items()
                  if s in specialist.strategies} if specialist else EXECUTABLE)
    if available:
        lines += [f"  {s.value}: {d}" for s, d in available.items()]
    else:
        lines += [
            "  NONE. You have no repair tool, deliberately: nothing in your",
            "  dimension is fixed by processing a file. Escalate with the",
            "  facts a human needs in order to act.",
        ]
    lines += [
        "",
        f"Your current authority for this market is {tier.name}. It was derived "
        f"from measured repair history and you cannot change it.",
    ]
    if focus:
        lines += [
            "",
            "You have been asked about these failing checks specifically:",
            *[f"  - {check}" for check in focus],
            "Other dimensions of this market are being investigated by other "
            "specialists at the same time. Do not re-triage the whole market.",
        ]
    if investigation.findings:
        lines += ["", "The deterministic investigation already established:"]
        lines += [f"  - {f.claim}" for f in investigation.findings]
    if investigation.gaps:
        lines += ["", "It could NOT establish:"]
        lines += [f"  - {g}" for g in investigation.gaps]
    lines += ["", "Investigate, then call propose_repair or escalate."
              if (specialist is None or specialist.may_repair)
              else "Investigate, then escalate."]
    return "\n".join(lines)


def _validate(
    args: dict[str, Any], toolbox: Toolbox, genai: GenAI, *,
    title: str, market: str, scene: str, tier: AutonomyTier,
    specialist: Specialist | None = None,
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
    if specialist is not None and strategy not in specialist.strategies:
        # Defence in depth. The tool declaration already narrows the enum, so a
        # well-formed call cannot name this strategy -- but the enum is a hint
        # to the model and this is the contract. An audio specialist deciding
        # the loudness problem is really a timing problem has to hand it back,
        # not reach across.
        genai.rejected("outside_remit",
                       f"{specialist.name} may not {strategy.value}",
                       title=title, market=market)
        raise UnsupportedClaim(
            f"the {specialist.name} specialist may not propose "
            f"{strategy.value}; it may propose "
            f"{sorted(s.value for s in specialist.strategies) or 'nothing'}. "
            f"If this is the right repair, escalate and say so."
        )

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

    # The model routinely answers with a full selector --
    # `dub_sync_offset_ms{title="SINTEL",market="de-DE"}` -- where a bare
    # metric name is wanted, because that is what it just queried. Verification
    # maps the name through MEASUREMENT_SERIES to find the probe that
    # re-measures it, and a selector maps to nothing, so the repair would run
    # and then be unverifiable. Normalised rather than rejected: the metric
    # name inside a selector is unambiguous, and refusing here would burn a
    # turn on a proposal that was substantively right.
    series = str(args.get("predicted_series", "")).split("{", 1)[0].strip()
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
    model: str = "",
    max_turns: int = MAX_TURNS,
    specialist: Specialist | None = None,
    focus: list[str] | None = None,
) -> Conclusion:
    """Run the reasoning loop until the model proposes, escalates, or stalls.

    With a `specialist`, this is that specialist's investigation: a narrowed
    tool surface, a narrowed strategy set, and a brief about one dimension
    rather than all eight. `focus` names the failing checks it is being asked
    about, so it works its own dimension instead of re-triaging the market.
    """
    from google.genai import types

    model = model or _default_model()
    incident = investigation.incident
    title, market = incident.title_id, incident.market
    toolbox = Toolbox(signal, title=title, market=market)
    conversation = str(uuid.uuid4())

    # Authority is read before the model is consulted, and given to it as a
    # fact. There is no tool that changes it.
    history = signal.repair_history(Strategy.RETIME.value, market)
    tier = earned_tier(**history)

    config = types.GenerateContentConfig(
        system_instruction=_system_for(specialist),
        temperature=0.2,
        tools=[types.Tool(function_declarations=_declarations(specialist))],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
    )
    contents: list[Any] = [
        types.Content(role="user", parts=[types.Part(
            text=_briefing(investigation, tier, specialist=specialist,
                           focus=focus))])
    ]
    rejections: list[tuple[str, str]] = []

    for turn in range(1, max_turns + 1):
        genai.step("reason")
        try:
          with genai.call(model, conversation_id=conversation,
                        temperature=0.2,
                        extra={"continuity.market": market,
                               "continuity.turn": turn}) as span:
            # Through the shared budget like every other model call: the
            # Conductor competes for the same free-tier quota as dubbing and
            # description, and a reasoning loop that ignored that would starve
            # the pipeline it is trying to repair.
            response = quota_call(
                model,
                lambda: client.models.generate_content(
                    model=model, contents=contents, config=config
                ),
                label=f"conduct({market})",
            )
            genai.record_usage(span, response, model)
        except DailyQuotaExhausted as exc:
            # Running out of budget mid-thought is a real operating condition,
            # not a crash. Everything gathered so far is real evidence and a
            # human can act on it; throwing it away to raise would be worse
            # than handing over what we have and saying why.
            genai.decision("conclusion", "budget_exhausted")
            return Conclusion(
                action="escalate", reason="budget_exhausted",
                summary=(
                    f"Reasoning stopped after {turn - 1} turn(s): the model "
                    f"budget for today is spent. {len(toolbox.gathered)} "
                    f"measurement(s) were gathered and are attached. {exc}"
                )[:600],
                evidence=list(toolbox.gathered.values()),
                turns=turn - 1, rejections=rejections,
            )

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

        # One user turn carrying one response per call, assembled here and
        # appended once below. Gemini pairs responses to calls per TURN, so
        # emitting them as separate user contents invites the same
        # count-mismatch that the rejection path caused outright.
        answers: list[Any] = []

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
                        scene=scene, tier=tier, specialist=specialist,
                    )
                except UnsupportedClaim as exc:
                    # Refused, and told why. The model gets one chance to fix
                    # its own proposal, which is how a real reviewer would
                    # handle it -- and every refusal is already counted.
                    #
                    # The refusal goes back as this CALL'S function response,
                    # not as a user message. The model is encouraged to call
                    # several tools in one turn and does; Gemini then requires
                    # exactly one function response per function call, and
                    # answering a rejected proposal with prose left the turn
                    # one response short. The next request failed with
                    #
                    #   400 INVALID_ARGUMENT: Please ensure that the number of
                    #   function response parts is equal to the number of
                    #   function call parts of the function call turn
                    #
                    # which reads like a client bug and is really "you did not
                    # answer one of my questions". Guardrails have to reply in
                    # the protocol they refused in.
                    rejections.append((call.name, str(exc)))
                    log.warning("proposal rejected: %s", exc)
                    answers.append(types.Part.from_function_response(
                        name=call.name,
                        response={
                            "accepted": False,
                            "rejected_because": str(exc),
                            "queries_you_actually_ran": sorted(toolbox.gathered),
                            "next": "correct the proposal, or escalate",
                        },
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
            # Logged at INFO because the sequence of tool calls IS the
            # reasoning. A run that concluded wrongly and a run that never
            # looked are indistinguishable without it.
            log.info("turn %d  %s(%s) -> %s", turn, call.name,
                     ", ".join(f"{k}={v}" for k, v in args.items())[:90],
                     str(result)[:110])
            answers.append(types.Part.from_function_response(
                name=call.name, response=result
            ))

        if answers:
            contents.append(types.Content(role="user", parts=answers))

        # Budget pressure, appended AFTER this turn's exchange so it lands in
        # conversation order. An agent that does not know how much rope it has
        # left will investigate until it runs out, which is exactly what
        # happened before this existed: eight turns of good questions and no
        # answer.
        remaining = max_turns - turn
        if 0 < remaining <= 2:
            contents.append(types.Content(role="user", parts=[types.Part(
                text=(f"You have {remaining} turn(s) left. Conclude now: call "
                      f"propose_repair with the evidence you already have, or "
                      f"escalate and say what a human needs to decide.")
            )]))

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
