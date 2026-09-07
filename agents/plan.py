"""Planning: choosing a repair, and predicting what it will do.

The interesting decision in this system is not "is de-DE broken" -- Grafana
answered that -- but *which repair could possibly work*, and that turns on one
distinction the probes already make:

    systematic drift   every line is late by about the same amount. The stem
                       is offset. Shifting it fixes everything. RETIME.

    progressive drift  lines are late by increasing amounts, because each one
                       overruns its slot and pushes the next. Shifting the
                       stem moves the whole staircase and fixes nothing. The
                       lines are too long, so they have to get shorter.
                       REWRITE.

Both look identical in `dub_sync_offset_ms`. A system that reads only the
failing number picks RETIME for both, and for the second case it will produce a
stem that is differently wrong and be surprised. Distinguishing them is the
difference between reacting to a symptom and diagnosing a cause.

The planner is deliberately allowed to be wrong. It states a falsifiable
prediction, the repair runs, and verification either confirms it or refutes it
-- and a refuted RETIME is what escalates the incident to REWRITE. That second
attempt is not a retry; it is a different hypothesis, formed because the first
one was disproved by measurement.

No model is involved here either. Choosing between two strategies on the basis
of a published boolean is arithmetic. Gemini's judgement is needed inside
REWRITE -- saying the same thing in fewer syllables -- not in deciding that a
rewrite is what the evidence calls for.
"""

from __future__ import annotations

from agents.contracts import (
    AutonomyTier,
    Direction,
    Evidence,
    Prediction,
    RepairIntent,
    Strategy,
    earned_tier,
)
from agents.investigate import Investigation
from agents.signal import Signal
from telemetry.metrics import DRIFT_SYSTEMATIC, SYNC_OFFSET

# How close to the threshold a repair should aim. Predicting exactly the
# threshold would make a repair that lands on the boundary count as a success
# while leaving the market one rounding error from failing again.
SAFETY_MARGIN = 0.85

# Requirements this planner knows how to repair. Anything else is escalated
# rather than attempted: a strategy invented for an unfamiliar failure is a
# strategy with no measured history, which is exactly what the autonomy ladder
# exists to keep away from production assets.
REPAIRABLE = ("dub_sync",)


class Unplannable(RuntimeError):
    """No repair can be proposed from this evidence. Not an error condition --
    it is the correct outcome when the system does not understand the failure,
    and it routes to a human instead of to a guess."""


def _sync_evidence(
    signal: Signal, title: str, market: str
) -> tuple[Evidence, Evidence, Evidence]:
    """The three facts a sync repair has to be built on."""
    observed = signal.measurement(SYNC_OFFSET, title=title, market=market)
    bar = signal.threshold(market, "dub_sync_max_ms")
    systematic = signal.observe(
        f'{DRIFT_SYSTEMATIC}{{title="{title}",market="{market}"}}'
    )
    missing = [
        name for name, value in
        (("measurement", observed), ("threshold", bar),
         ("drift shape", systematic))
        if value is None
    ]
    if missing:
        raise Unplannable(
            f"cannot plan a sync repair for {market}: no {', no '.join(missing)}. "
            f"Planning on a partial picture is how a repair gets aimed at the "
            f"wrong thing."
        )
    return observed, bar, systematic  # type: ignore[return-value]


def plan_sync_repair(
    signal: Signal,
    *,
    title: str,
    market: str,
    scene: str,
    supersedes: str | None = None,
    force: Strategy | None = None,
) -> RepairIntent:
    """Propose one repair for a sync failure, with its predicted effect.

    `force` exists for the second attempt: when verification refutes a RETIME,
    the Conductor re-plans with REWRITE rather than letting the same evidence
    produce the same answer forever.
    """
    observed, bar, systematic = _sync_evidence(signal, title, market)
    current = float(observed.value)
    limit = float(bar.value)
    target = round(limit * SAFETY_MARGIN, 1)

    if current <= target:
        raise Unplannable(
            f"{market} sync is {current:g} ms against a {limit:g} ms limit; "
            f"there is nothing to repair"
        )

    if force is not None:
        strategy = force
    elif float(systematic.value) == 1.0:
        strategy = Strategy.RETIME
    else:
        strategy = Strategy.REWRITE

    if strategy is Strategy.RETIME:
        # A uniform offset is undone by shifting the stem by that offset. The
        # parameter is the measurement, which is why this is checkable: if the
        # stem was not uniformly offset, the shift lands somewhere provably
        # wrong and verification says so.
        params = {"shift_ms": -round(current, 1)}
        rationale = (
            f"every line drifts by a similar amount ({current:g} ms), so the "
            f"stem is offset rather than mistimed. Shifting it by "
            f"{-round(current, 1):g} ms should bring every onset back."
        )
    else:
        # Shortening the text is the only thing that shortens a line. How much
        # shorter is a judgement, and that is where the model is used.
        params = {
            "target_ms": target,
            "reduce_by_ms": round(current - target, 1),
        }
        rationale = (
            f"drift grows down the scene, so lines are overrunning their slots "
            f"and pushing the next one late. Shifting the stem would move the "
            f"whole staircase. The lines have to get shorter."
        )

    history = signal.repair_history(strategy.value, market)
    tier = earned_tier(**history)

    return RepairIntent(
        strategy=strategy,
        target_asset_id=f"{title}:{scene}:dub_stem:{market}",
        params=params,
        prediction=Prediction(
            series=SYNC_OFFSET, market=market, scene=scene,
            direction=Direction.DECREASE, target_value=target,
            baseline=current,
        ),
        justification=[observed, bar, systematic],
        tier=tier,
        supersedes=supersedes,
        rationale=rationale,
    )


def plan(
    signal: Signal, investigation: Investigation, *, scene: str = "S01"
) -> list[RepairIntent]:
    """Propose repairs for everything in an investigation we know how to fix.

    Returns an empty list rather than raising when nothing is repairable. The
    Conductor then escalates, which is the correct behaviour: an autonomous
    system that cannot fix something must say so, not improvise.
    """
    if investigation.resolved_before_we_arrived:
        return []

    title, market = investigation.incident.title_id, investigation.incident.market
    intents: list[RepairIntent] = []
    for subject in investigation.subjects:
        if subject not in REPAIRABLE:
            continue
        try:
            intents.append(
                plan_sync_repair(signal, title=title, market=market, scene=scene)
            )
        except Unplannable:
            # Kept out of the plan, not silenced: the Conductor sees an empty
            # plan against a non-empty investigation and escalates.
            continue
    return intents


def unrepairable(investigation: Investigation) -> list[str]:
    """Subjects this planner has no strategy for. The escalation payload."""
    return [s for s in investigation.subjects if s not in REPAIRABLE]
