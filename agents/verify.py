"""Verification: did it work, and were we right about why.

The step that makes autonomy safe to grant. A repair that reported its own
success would be worthless -- so nothing here asks the repair how it went. The
probes run again, on the new bytes, with the same code that produced the
failing measurement, and the answer is whatever they say.

Two questions, answered separately, and keeping them apart is the whole point:

    passed            the requirement is now met, against the bar Grafana
                      publishes. This is what the market cares about.

    prediction_held   the number moved where the agent SAID it would. This is
                      what the agent's competence is measured on.

They come apart more often than is comfortable. A concurrent re-render, an
edited threshold, a probe that was reading a stale file -- any of these can make
a repair pass for reasons the agent never anticipated. Counting that as earned
competence is how an agent talks itself into autonomy it has not demonstrated,
so it lands in a third bucket, `lucky`, which raises the denominator of the
autonomy ledger without raising the numerator. A strategy that keeps getting
away with it drifts DOWN the ladder.

The ledger itself is `continuity_repairs_total` in Prometheus: external,
append-only, and visible on a dashboard. The agent reads its own authority from
it at decision time and cannot write to it except by actually repairing
something and being measured.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from agents.contracts import Evidence, RepairIntent, VerificationResult
from agents.signal import Signal
from media.qc.types import Measurement

log = logging.getLogger("continuity.verify")

# Measurement key -> (published threshold name, comparator). The comparator is
# duplicated from media/qc/profiles.py deliberately: this module must be able
# to judge a repair using ONLY what Grafana publishes, without loading the
# profile file, so a verification cannot silently pass because someone edited a
# local JSON that the ruler never saw.
JUDGED_BY: dict[str, tuple[str, str]] = {
    "delivery.dub_sync_offset_ms": ("dub_sync_max_ms", "lte"),
    "delivery.line_overrun_ms": ("line_overrun_max_ms", "lte"),
    "delivery.audio_true_peak_dbtp": ("true_peak_max_dbtp", "lte"),
    "delivery.subtitle_reading_rate_cps": ("subtitle_max_cps", "lte"),
    "quality.speech_rate_wpm": ("speech_rate_max_wpm", "lte"),
    "quality.semantic_fidelity_score": ("semantic_fidelity_floor", "gte"),
    "accessibility.ad_collision_ms": ("ad_collision_max_ms", "lte"),
}

# Loudness is a two-sided band and needs both ends, so it is judged apart from
# the one-sided table above rather than being forced into it.
LOUDNESS_KEY = "delivery.audio_loudness_lufs"


class VerificationError(RuntimeError):
    pass


def _met(value: float, bar: float, comparator: str) -> bool:
    return value <= bar if comparator == "lte" else value >= bar


def requirement_results(
    signal: Signal, measurements: list[Measurement], *, market: str,
) -> list[tuple[str, bool, float, float]]:
    """Judge fresh measurements against the bars Grafana currently publishes.

    Returns (key, met, measured, bar). A measurement with no published
    threshold is skipped rather than assumed to pass -- an unjudged number is
    not a passing one, and the coverage gate is what notices its absence.
    """
    results: list[tuple[str, bool, float, float]] = []
    for measurement in measurements:
        if measurement.key == LOUDNESS_KEY:
            target = signal.threshold(market, "loudness_target_lufs")
            tolerance = signal.threshold(market, "loudness_tolerance_lu")
            if target is None or tolerance is None:
                continue
            deviation = abs(measurement.value - float(target.value))
            results.append((measurement.key,
                            deviation <= float(tolerance.value),
                            measurement.value, float(target.value)))
            continue

        if measurement.key not in JUDGED_BY:
            continue
        requirement, comparator = JUDGED_BY[measurement.key]
        bar = signal.threshold(market, requirement)
        if bar is None:
            log.warning("no published threshold for %s; %s left unjudged",
                        requirement, measurement.key)
            continue
        results.append((
            measurement.key,
            _met(measurement.value, float(bar.value), comparator),
            measurement.value, float(bar.value),
        ))
    return results


def verify(
    signal: Signal,
    intent: RepairIntent,
    repaired: Path,
    remeasure: Callable[[Path], list[Measurement]],
    *,
    market: str,
    before: list[Measurement] | None = None,
) -> VerificationResult:
    """Re-measure the repaired asset and judge both questions.

    `remeasure` is passed in rather than imported so verification runs the
    caller's probe set -- the same one that produced the failing number. A
    verifier measuring with different code than the builder used can disagree
    with itself for reasons nobody can debug.
    """
    if not repaired.exists():
        raise VerificationError(f"nothing to verify: {repaired} does not exist")

    measurements = remeasure(repaired)
    by_key = {m.key: m for m in measurements}

    # The predicted series, in the QC vocabulary. The prediction names a
    # Prometheus series; the probes speak measurement keys.
    from telemetry.metrics import MEASUREMENT_SERIES
    to_key = {series: key for key, series in MEASUREMENT_SERIES.items()}
    predicted_key = to_key.get(intent.prediction.series)
    if predicted_key is None or predicted_key not in by_key:
        raise VerificationError(
            f"the repair predicted {intent.prediction.series}, which the "
            f"probes did not re-measure. A prediction nothing re-measures "
            f"cannot be falsified, and an unfalsifiable prediction must not "
            f"count as held."
        )
    observed = float(by_key[predicted_key].value)

    # `passed` asks two things, and getting this wrong is expensive in both
    # directions.
    #
    # Demanding that EVERY requirement now pass records a correct repair as a
    # failure whenever the market has a second, unrelated fault -- RETIME fixed
    # sync exactly as predicted and was marked `failed` because the loudness it
    # never touched was still wrong. That poisons the autonomy ledger against a
    # strategy that did its job.
    #
    # Demanding only that the TARGETED requirement pass would let a repair fix
    # sync by wrecking true peak and call it a success.
    #
    # So: the targeted requirement must now be met, and nothing that was
    # passing before may have stopped. Other pre-existing faults are other
    # repairs' work, and the alert will bring the agent back for them.
    results = requirement_results(signal, measurements, market=market)
    was_met = {
        key: met for key, met, _v, _b in
        requirement_results(signal, before or [], market=market)
    }
    now_met = {key: met for key, met, _v, _b in results}

    targeted_ok = now_met.get(predicted_key, False)
    regressed = [
        key for key, met in now_met.items()
        if not met and was_met.get(key, False)
    ]
    passed = targeted_ok and not regressed
    failing = [key for key, met in now_met.items() if not met]

    evidence = [
        Evidence(
            kind="probe",
            query=f"{by_key[key].method}({repaired.name}) -> {key}",
            value=value,
            source=by_key[key].method,
            detail={"bar": bar, "met": met},
        )
        for key, met, value, bar in results if key in by_key
    ]
    if not evidence:
        raise VerificationError(
            "the repaired asset produced no judgeable measurement; there is "
            "nothing here to verify against"
        )

    result = VerificationResult(
        intent=intent, observed=observed, passed=passed, evidence=evidence,
    )
    log.info(
        "%s -> %s | %s | target %s%s%s",
        intent.strategy.value, result.outcome, result.summary(),
        "met" if targeted_ok else "NOT met",
        f" | regressed: {regressed}" if regressed else "",
        f" | still failing elsewhere: {failing}" if failing else "",
    )
    return result


def record(instruments, result: VerificationResult, *, market: str) -> None:
    """Append the outcome to the autonomy ledger.

    The only way an agent's authority ever changes. It cannot be argued
    upward in a prompt, because the numerator only moves when a prediction the
    agent committed to in advance turns out to be true.
    """
    instruments.repair(
        strategy=result.intent.strategy.value,
        outcome=result.outcome,
        market=market,
    )
