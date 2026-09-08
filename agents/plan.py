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
from telemetry.metrics import DRIFT_SYSTEMATIC, LOUDNESS, SYNC_OFFSET, SYNC_SIGNED

# How close to the threshold a repair should aim. Predicting exactly the
# threshold would make a repair that lands on the boundary count as a success
# while leaving the market one rounding error from failing again.
SAFETY_MARGIN = 0.85

# Requirements this planner knows how to repair. Anything else is escalated
# rather than attempted: a strategy invented for an unfamiliar failure is a
# strategy with no measured history, which is exactly what the autonomy ladder
# exists to keep away from production assets.
REPAIRABLE = ("dub_sync", "loudness")


class Unplannable(RuntimeError):
    """No repair can be proposed from this evidence. Not an error condition --
    it is the correct outcome when the system does not understand the failure,
    and it routes to a human instead of to a guess."""


def _sync_evidence(
    signal: Signal, title: str, market: str
) -> tuple[Evidence, Evidence, Evidence, Evidence]:
    """The four facts a sync repair has to be built on.

    `direction` is the fourth, and it was learned the expensive way. The
    measurement is a magnitude, because it is judged against a tolerance and a
    signed value would let a badly EARLY dub pass a `<= 120 ms` check. So on
    its own it says how far wrong the stem is and not which way, and RETIME --
    whose entire job is to shift the stem one way or the other -- was choosing
    from a coin flip. On real audio it went 187 ms early, shifted a further
    188 ms earlier, and landed at 375 ms.

    Refusing without it rather than assuming "late" is the same rule this
    module already applies to a missing threshold: planning on a partial
    picture is how a repair gets aimed at the wrong thing.
    """
    observed = signal.measurement(SYNC_OFFSET, title=title, market=market)
    bar = signal.threshold(market, "dub_sync_max_ms")
    systematic = signal.observe(
        f'{DRIFT_SYSTEMATIC}{{title="{title}",market="{market}"}}'
    )
    direction = signal.observe(
        f'{SYNC_SIGNED}{{title="{title}",market="{market}"}}'
    )
    missing = [
        name for name, value in
        (("measurement", observed), ("threshold", bar),
         ("drift shape", systematic), ("drift direction", direction))
        if value is None
    ]
    if missing:
        raise Unplannable(
            f"cannot plan a sync repair for {market}: no {', no '.join(missing)}. "
            f"Planning on a partial picture is how a repair gets aimed at the "
            f"wrong thing."
        )
    return observed, bar, systematic, direction  # type: ignore[return-value]


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
    observed, bar, systematic, direction = _sync_evidence(signal, title, market)
    current = float(observed.value)
    signed = float(direction.value)
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
        # A uniform offset is undone by shifting the stem back by that offset.
        # Built from the SIGNED drift, not the magnitude: positive means the
        # dub arrives late and must be pulled earlier, negative means it
        # arrives early and must be pushed later. Using the magnitude here
        # always shifted earlier, which fixed a late stem and doubled the error
        # on an early one.
        shift = -round(signed, 1)
        params = {"shift_ms": shift}
        late = "late" if signed > 0 else "early"
        rationale = (
            f"every line drifts by a similar amount, and the stem runs "
            f"{abs(signed):g} ms {late} on average against a {limit:g} ms "
            f"limit, so it is offset rather than mistimed. Shifting it by "
            f"{shift:g} ms should bring every onset back."
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


def plan_loudness_repair(
    signal: Signal, *, title: str, market: str, scene: str,
    supersedes: str | None = None,
) -> RepairIntent:
    """Propose a REMIX for a stem outside its market's loudness band.

    Loudness is a two-sided tolerance, so the prediction needs care. Aiming at
    the target itself would be wrong: normalising to -23 LUFS lands somewhere
    near it, not on it, and a repair that arrived at -22.5 inside a +/- 1 band
    would be recorded as having failed its own prediction.

    When a measurement is OUTSIDE a band, though, the repair is one-sided --
    we know which edge we are beyond, so the honest prediction is "cross into
    the band", i.e. reach the near edge. That is falsifiable, and it is true
    exactly when the requirement is met.
    """
    observed = signal.measurement(LOUDNESS, title=title, market=market,
                                  scene=scene)
    target = signal.threshold(market, "loudness_target_lufs")
    tolerance = signal.threshold(market, "loudness_tolerance_lu")
    peak = signal.threshold(market, "true_peak_max_dbtp")
    missing = [n for n, v in (("measurement", observed), ("target", target),
                              ("tolerance", tolerance), ("peak ceiling", peak))
               if v is None]
    if missing:
        raise Unplannable(
            f"cannot plan a loudness repair for {market}: no "
            f"{', no '.join(missing)}."
        )

    current = float(observed.value)          # type: ignore[union-attr]
    aim = float(target.value)                # type: ignore[union-attr]
    band = float(tolerance.value)            # type: ignore[union-attr]
    ceiling = float(peak.value)              # type: ignore[union-attr]

    if abs(current - aim) <= band:
        raise Unplannable(
            f"{market} loudness is {current:g} LUFS, inside the {aim:g} "
            f"+/- {band:g} band; there is nothing to repair"
        )

    too_loud = current > aim
    edge = aim + band if too_loud else aim - band
    return RepairIntent(
        strategy=Strategy.REMIX,
        target_asset_id=f"{title}:{scene}:dub_stem:{market}",
        params={"target_lufs": aim, "true_peak_max": ceiling},
        prediction=Prediction(
            series=LOUDNESS, market=market, scene=scene,
            direction=Direction.DECREASE if too_loud else Direction.INCREASE,
            target_value=edge, baseline=current,
        ),
        justification=[observed, target, tolerance],  # type: ignore[list-item]
        tier=earned_tier(**signal.repair_history(Strategy.REMIX.value, market)),
        supersedes=supersedes,
        rationale=(
            f"the stem is {abs(current - aim):.1f} LU "
            f"{'louder' if too_loud else 'quieter'} than the {aim:g} LUFS "
            f"target, outside the +/- {band:g} band. Normalising touches no "
            f"timing, so it cannot disturb the sync."
        ),
    )


# Which planner handles which failing requirement. A subject with no entry is
# not something these rules know how to fix, which is a different statement
# from "nothing can fix it" -- the model may still have a strategy, and a
# rights failure has none at all.
PLANNERS = {
    "dub_sync": plan_sync_repair,
    "loudness": plan_loudness_repair,
}


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
        planner = PLANNERS.get(subject)
        if planner is None:
            continue
        try:
            intents.append(
                planner(signal, title=title, market=market, scene=scene)
            )
        except Unplannable:
            # Kept out of the plan, not silenced: an empty plan against a
            # non-empty investigation is what tells the Conductor to escalate.
            continue
    return intents


def unrepairable(investigation: Investigation) -> list[str]:
    """Subjects this planner has no strategy for. The escalation payload."""
    return [s for s in investigation.subjects if s not in REPAIRABLE]
