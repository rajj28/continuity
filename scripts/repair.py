"""The whole loop, in one command: investigate, reason, act, verify, learn.

    python scripts/repair.py --market de-DE            # propose only
    python scripts/repair.py --market de-DE --approve  # and carry it out

This is what the Grafana webhook will call. Running it by hand differs only in
where the incident comes from.

## Why --approve exists

The first repair a strategy ever attempts runs at RECOMMEND, because
`earned_tier` has no history to trust. RECOMMEND means propose and let a human
decide, so the flag is that human. It is not a debug convenience: it is the
autonomy ladder working, and after three verified successes the same command
stops needing it because the strategy has earned AUTO_FIX_VERIFY.

An operator can also force the issue with --approve on a strategy that has
lost its privileges, which is deliberate -- a human overriding downward-adjusted
autonomy is a normal act, and it is recorded in the trace like everything else.

## Nothing is edited in place

The repair writes a new version of the asset, hashed, with the failing version
recorded as its parent. So "before" is a fact that still exists on disk rather
than a number someone remembers -- which is what makes the verification honest
and a rollback a lookup instead of a rebuild.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.conductor import conduct  # noqa: E402
from agents.contracts import AutonomyTier  # noqa: E402
from agents.intents import IntentStore, StaleIntent, check_fresh  # noqa: E402
from agents.ledger import Ledger  # noqa: E402
from agents.investigate import investigate  # noqa: E402
from agents.mcp import grafana_client  # noqa: E402
from agents.plan import plan as plan_repairs  # noqa: E402
from agents.repair import apply  # noqa: E402
from agents.signal import Signal  # noqa: E402
from agents.verify import verify  # noqa: E402
from agents.wake import Incident  # noqa: E402
from media.qc.loudness import measure_loudness  # noqa: E402
from media.qc.report import QCReport, QCStore  # noqa: E402
from media.qc.sync import measure_dub  # noqa: E402
from media.qc.types import Interval  # noqa: E402
from media.store import Asset, ParentRef, Store  # noqa: E402
from telemetry.genai import GenAI  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import asset_span, load_env, record_measurement, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.repair")


def reference_intervals(scene: dict) -> list[Interval]:
    origin = scene["in_ms"]
    return [
        Interval(start_ms=float(u["start_ms"] - origin),
                 end_ms=float(u["end_ms"] - origin))
        for u in scene["utterances"]
    ]


def _deterministic(signal, args, genai, investigation):
    """The model-free planner, for when the model is unavailable or unwanted.

    It handles the cases it was explicitly built for and refuses the rest --
    which is the honest boundary. No plan here means the deterministic rules do
    not cover this failure, not that nothing can be done.
    """
    intents = plan_repairs(signal, investigation, scene=args.scene)
    if not intents:
        print(f"\nNO PLAN: the deterministic rules do not cover any of "
              f"{investigation.subjects}. That is not the same as nothing "
              f"being fixable -- the model may still have a strategy, and a "
              f"rights failure has none at all.")
        genai.decision("planner", "unplannable")
        return None
    # One repair per run, deliberately. Each is verified against its own
    # prediction before the next is planned, so a second repair is decided with
    # the first one's measured result in hand rather than guessed alongside it.
    genai.decision("planner", "deterministic")
    return intents[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--scene", default="S03")
    ap.add_argument("--title", default="SINTEL")
    ap.add_argument("--manifest", default="out/scenes/manifest.json")
    ap.add_argument("--store", default="out/store")
    ap.add_argument("--planner", choices=["model", "deterministic"],
                    default="model",
                    help="model reasons over every dimension; deterministic "
                         "covers the cases the rules were built for, with no "
                         "model call at all")
    ap.add_argument("--approve", action="store_true",
                    help="stand in for the human a RECOMMEND tier requires")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    env = load_env()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    scene = next(s for s in manifest["scenes"] if s["id"] == args.scene)

    store = Store(Path(args.store))
    qc = QCStore(Path(args.store))
    intents = IntentStore(Path(args.store))
    ledger = Ledger(Path(args.store))
    signal = Signal(grafana_client(env))
    tracer, meter = setup("continuity-repair")
    instruments = Instruments(meter)
    genai = GenAI(tracer, instruments, "conductor")

    incident = Incident(
        fingerprint=f"manual:{args.title}:{args.market}",
        alertname="MarketNotReleaseReady", action="investigate",
        status="firing", title_id=args.title, market=args.market,
        severity="critical", started_at="",
    )

    try:
        with tracer.start_as_current_span(f"incident.{args.market}") as root:
            root.set_attribute("continuity.market", args.market)

            # -- observe, detect, investigate ---------------------------
            genai.step("investigate")
            inv = investigate(signal, incident)
            print(f"\ninvestigation: {inv.summary()}")
            for finding in inv.findings:
                print(f"  - {finding.claim}")
            if inv.resolved_before_we_arrived:
                print("\nthe market recovered before we looked; nothing to do")
                return 0

            # -- reason and plan ----------------------------------------
            # A proposal made earlier and awaiting a human is preferred over
            # re-reasoning, because that is what RECOMMEND means: the approval
            # happens later, and it has to carry out what was actually
            # proposed rather than whatever the model would say if asked
            # again. Freshness is checked before it is trusted.
            intent = intents.get(args.title, args.market) if args.approve else None
            if intent is not None:
                print(f"\napproving the proposal made at "
                      f"{intents.proposed_at(args.title, args.market)}")
                observed = signal.measurement(
                    intent.prediction.series, title=args.title,
                    market=args.market, scene=args.scene,
                )
                try:
                    check_fresh(intent,
                                None if observed is None else float(observed.value))
                except StaleIntent as exc:
                    print(f"\nREFUSED: {exc}")
                    intents.clear(args.title, args.market)
                    genai.decision("authority", "stale_proposal")
                    return 1
            elif args.planner == "deterministic":
                intent = _deterministic(signal, args, genai, inv)
                if intent is None:
                    return 0
            else:
                from media.model import client as build_client
                conclusion = conduct(
                    build_client(), signal, inv, genai, scene=args.scene,
                )
                if conclusion.acted:
                    intent = conclusion.intent
                elif conclusion.reason == "budget_exhausted":
                    # The model is a better planner, not a required one. When
                    # its budget is spent the deterministic planner still
                    # covers the cases it was built for, so a market does not
                    # stay broken because the reasoning ran out of money.
                    # Which planner decided is recorded, because "the model
                    # chose this" and "the rules chose this" are different
                    # claims about the same repair.
                    print("\nmodel budget spent; falling back to the "
                          "deterministic planner")
                    genai.decision("planner", "fell_back")
                    intent = _deterministic(signal, args, genai, inv)
                    if intent is None:
                        return 0
                else:
                    print(f"\nESCALATED: {conclusion.reason}")
                    print(conclusion.summary[:500])
                    return 0
            print(f"\nproposed  {intent.strategy.value}  {intent.params}")
            print(f"predicts  {intent.prediction.describe()}")
            print(f"authority {intent.tier.name}")
            print(f"because   {intent.rationale[:220]}")

            # -- the authority gate -------------------------------------
            if intent.needs_human and not args.approve:
                path = intents.put(intent, title=args.title,
                                   market=args.market,
                                   incident=incident.fingerprint)
                print(
                    f"\nNOT ACTING. {intent.strategy.value} is at "
                    f"{intent.tier.name} in {args.market}: it has no measured "
                    f"history here, so it proposes and a human decides.\n"
                    f"Proposal written to {path}\n"
                    f"Re-run with --approve to carry out exactly this."
                )
                genai.decision("authority", "withheld")
                return 0
            if intent.tier is AutonomyTier.BLOCK:
                print(f"\nBLOCKED: {intent.strategy.value} may not run in "
                      f"{args.market} at any authority.")
                return 0
            if intent.needs_human:
                log.info("acting on human approval at tier %s", intent.tier.name)
                genai.decision("authority", "approved_by_human")

            # -- act ----------------------------------------------------
            genai.step("act")
            asset = store.load(intent.target_asset_id)
            source = Path(asset.uri)
            # Strip any existing version suffix before adding the new one, or
            # a third repair produces S03_stem_v2_v3.wav and the name stops
            # saying which version it is.
            base = re.sub(r"_v\d+$", "", source.stem)
            repaired_path = source.with_name(
                f"{base}_v{asset.version + 1}{source.suffix}"
            )
            with asset_span(
                tracer, f"repair.{intent.strategy.value}", stage="repair",
                title_id=args.title, scene_id=args.scene, market=args.market,
                asset_id=asset.id, asset_kind=asset.kind,
                parents=[(asset.id, asset.sha256)],
                extra={
                    "continuity.repair.strategy": intent.strategy.value,
                    "continuity.repair.params": json.dumps(intent.params),
                    "continuity.repair.prediction": intent.prediction.describe(),
                    "continuity.repair.tier": intent.tier.name,
                },
            ):
                outcome = apply(intent, source, repaired_path,
                                total_ms=scene["duration_ms"])
            print(f"\nacted     {outcome.detail.get('filter', outcome.params)}")

            # -- verify -------------------------------------------------
            genai.step("verify")
            words = sum(u["words"] for u in scene["utterances"])

            def remeasure(path: Path):
                return (measure_dub(path, reference_intervals(scene), words)
                        + measure_loudness(path))

            # The pre-repair report is what makes "did anything regress?"
            # answerable. It is on disk under the failing version's own hash,
            # so the comparison is against bytes that still exist rather than
            # against a number someone remembered.
            previous = qc.get(asset.sha256)
            result = verify(signal, intent, repaired_path, remeasure,
                            market=args.market,
                            before=previous.measurements if previous else None)

            # -- persist the new version, with the old one as its parent --
            sha, _ = store.put_file(repaired_path)
            measurements = remeasure(repaired_path)
            with asset_span(
                tracer, "repair.verified", stage="verify", title_id=args.title,
                scene_id=args.scene, market=args.market, asset_id=asset.id,
                asset_kind=asset.kind, asset_sha=sha,
                parents=[(asset.id, asset.sha256)],
                extra={
                    "continuity.verify.outcome": result.outcome,
                    "continuity.verify.passed": result.passed,
                    "continuity.verify.prediction_held": result.prediction_held,
                    "continuity.verify.observed": result.observed,
                },
            ) as span:
                for measurement in measurements:
                    record_measurement(span, measurement)

            store.record(Asset(
                id=asset.id, kind=asset.kind, sha256=sha,
                uri=str(repaired_path), bytes=repaired_path.stat().st_size,
                title_id=args.title, scene_id=args.scene, market=args.market,
                version=asset.version + 1, duration_ms=asset.duration_ms,
                parents=[ParentRef(asset.id, asset.sha256, "pre_repair")]
                        + list(asset.parents),
                produced_by={
                    "stage": "repair", "strategy": intent.strategy.value,
                    "params": intent.params, "outcome": result.outcome,
                    "predicted": intent.prediction.describe(),
                    "observed": result.observed,
                },
            ))
            qc.put(QCReport(
                sha256=sha, asset_id=asset.id, title_id=args.title,
                scene_id=args.scene, market=args.market,
                measurements=measurements,
                detail={"repaired_by": intent.strategy.value,
                        "outcome": result.outcome},
            ))

            # -- learn --------------------------------------------------
            genai.step("learn")
            # Appended to the durable ledger and NOT pushed as a counter from
            # here. The state exporter is the single publisher of
            # continuity_repairs_total; two writers under different job labels
            # would double-count every repair in `sum by (outcome)`, and an
            # autonomy figure that counts each success twice is worse than one
            # that counts none.
            entry = ledger.append(result, market=args.market,
                                  asset_id=asset.id, sha256=sha)
            log.info("ledger: %s/%s in %s", entry.strategy, entry.outcome,
                     entry.market)
            # A consumed proposal must not linger: an operator could otherwise
            # approve it twice and shift a stem that has already been shifted.
            intents.clear(args.title, args.market)
            root.set_attribute("continuity.repair.outcome", result.outcome)

        print(f"\n--- {result.outcome.upper()} ---")
        print(f"{result.summary()}")
        print(f"requirement met     {result.passed}")
        print(f"prediction held     {result.prediction_held}")
        if result.outcome == "lucky":
            print(
                "\nThe market improved but not for the reason given. Recorded "
                "as `lucky`: it raises the denominator of this strategy's "
                "record without raising the numerator, so getting away with it "
                "costs authority rather than earning it."
            )
        print(f"\nnew version         v{asset.version + 1}  {sha[:16]}")
        print(f"                    {repaired_path}")
        print("\nGrafana decides whether that is enough:")
        print(f'  market_release_ready{{title="{args.title}",market="{args.market}"}}')
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
