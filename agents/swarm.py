"""Run the specialists a market's failures actually call for, concurrently.

A blocked market is usually blocked several ways at once, and those ways are
independent: whether the dub lands on the picture has nothing to do with
whether a music cue was licensed for the territory. Investigating them one
after another is not caution, it is just slower.

    swarm(...)  ->  Swarm(conclusions=[...], repairs=[...], escalations=[...])

## What concurrency is safe here

Investigation is read-only, so any number of specialists can investigate at
once. There is no shared state to corrupt: each gets its own `Toolbox` and
therefore its own record of which queries it ran, which is what keeps evidence
citations attributable to the agent that gathered them.

ACTING is the part that is not safe, and this module does not act. It returns
proposals. Two agents rewriting the same dub stem concurrently would race on
the file, so the caller applies repairs in sequence -- and `order_repairs`
sequences them by lineage, so a repair whose output another repair depends on
runs first.

## Why not one agent per market as well

Because markets are already separate incidents. Grafana fires an alert per
market and each one wakes its own run; parallelism across markets falls out of
that for free and does not belong here.

## Cost

Only the specialists whose dimension is failing are woken -- `dispatch_for`
sees to that -- so a market blocked on loudness alone costs one agent run, not
five. A market blocked on everything costs five, which is the case where five
is the right number.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from agents.conductor import Conclusion, conduct
from agents.investigate import Investigation
from agents.signal import Signal
from agents.specialists import Specialist, dispatch_for
from telemetry.genai import GenAI

log = logging.getLogger("continuity.swarm")


@dataclass
class Verdict:
    """One specialist's conclusion, and whose it was."""

    specialist: Specialist
    checks: list[str]
    conclusion: Conclusion | None = None
    error: str = ""

    @property
    def name(self) -> str:
        return self.specialist.name

    @property
    def proposed(self) -> bool:
        return self.conclusion is not None and self.conclusion.acted


@dataclass
class Swarm:
    """What the whole roster concluded about one market."""

    market: str
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def repairs(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.proposed]

    @property
    def escalations(self) -> list[Verdict]:
        return [v for v in self.verdicts
                if v.conclusion is not None
                and v.conclusion.action == "escalate"]

    @property
    def failures(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.error]

    def describe(self) -> str:
        parts = []
        for verdict in self.verdicts:
            if verdict.error:
                outcome = f"error: {verdict.error[:60]}"
            elif verdict.conclusion is None:
                outcome = "no conclusion"
            elif verdict.conclusion.acted:
                outcome = f"proposes {verdict.conclusion.intent.strategy.value}"
            else:
                outcome = f"{verdict.conclusion.action}"
            parts.append(f"{verdict.name}({','.join(verdict.checks)}) -> {outcome}")
        return "; ".join(parts) or "nothing to investigate"


def order_repairs(proposals: list[Verdict], dependencies: dict[str, list[str]],
                  ) -> list[Verdict]:
    """Sequence proposals so a repair runs before anything built from it.

    The ordering is not invented. It is already written down in the asset
    lineage: if the package records the dub stem as a parent, then repairing
    the stem invalidates the package, and rebuilding the package first would
    be work thrown away.

    `dependencies` maps an asset id to the assets built from it -- exactly what
    the `blast_radius` tool returns. A proposal whose target appears as a
    PARENT of another proposal's target goes first.

    Stable for anything unrelated, so two proposals that do not touch each
    other keep roster order and a run stays reproducible.
    """
    def depth(verdict: Verdict) -> int:
        """How many of the other proposals' targets depend on this one."""
        intent = verdict.conclusion.intent if verdict.conclusion else None
        target = getattr(intent, "target_asset_id", "") if intent else ""
        if not target:
            return 0
        downstream = set(dependencies.get(target, ()))
        others = {
            getattr(v.conclusion.intent, "target_asset_id", "")
            for v in proposals if v is not verdict and v.conclusion
            and v.conclusion.intent
        }
        return -len(downstream & others)      # more dependants -> earlier

    return sorted(proposals, key=depth)


async def _one(specialist: Specialist, checks: list[str], *, client: Any,
               signal: Signal, investigation: Investigation, genai: GenAI,
               scene: str, model: str) -> Verdict:
    verdict = Verdict(specialist=specialist, checks=checks)
    try:
        # `conduct` is synchronous and spends its time waiting on HTTP, so it
        # goes to a thread. Threads rather than an async client because the
        # guardrails, the toolbox and the tracing are all shared with the
        # single-agent path, and forking that into an async twin would give
        # two implementations of the rules to keep honest.
        verdict.conclusion = await asyncio.to_thread(
            conduct, client, signal, investigation, genai,
            scene=scene, model=model, specialist=specialist, focus=checks,
        )
    except Exception as exc:                                  # noqa: BLE001
        # One specialist failing must not take the others with it. A market
        # where compliance errored and localisation proposed a good repair is
        # better served by the repair plus a visible error than by nothing.
        log.warning("%s specialist failed: %s", specialist.name, exc)
        verdict.error = str(exc)
    return verdict


async def swarm(
    client: Any,
    signal: Signal,
    investigation: Investigation,
    genai: GenAI,
    *,
    failing: list[str],
    scene: str = "S03",
    model: str = "",
    limit: int = 4,
) -> Swarm:
    """Investigate every failing dimension of one market at once.

    `limit` caps how many specialists reason simultaneously. Not for safety --
    they only read -- but because they share a model quota and a Grafana
    datasource, and four in flight is enough to hide the latency without
    turning one incident into a thundering herd.
    """
    market = investigation.incident.market
    work = dispatch_for(failing)
    if not work:
        return Swarm(market=market)

    log.info("waking %d specialist(s) for %s: %s", len(work), market,
             ", ".join(s.name for s, _ in work))

    gate = asyncio.Semaphore(max(1, limit))

    async def guarded(specialist: Specialist, checks: list[str]) -> Verdict:
        async with gate:
            return await _one(specialist, checks, client=client, signal=signal,
                              investigation=investigation, genai=genai,
                              scene=scene, model=model)

    verdicts = await asyncio.gather(
        *(guarded(specialist, checks) for specialist, checks in work)
    )
    result = Swarm(market=market, verdicts=list(verdicts))
    log.info("swarm for %s: %s", market, result.describe())
    return result
