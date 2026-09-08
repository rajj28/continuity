"""Wake every specialist a market's failures call for, at once.

    python scripts/run_swarm.py --market de-DE
    python scripts/run_swarm.py --market ja-JP --approve

Where `scripts/repair.py` runs one general-purpose agent over the whole market
and concludes with one action, this runs the specialists whose dimension is
actually failing, concurrently, and collects what each of them concluded.

The difference is not speed, though it is faster. It is that a market blocked
on lip-sync AND an uncleared music cue gets a localisation agent that knows
about isochrony and a compliance agent that has no repair tool at all --
rather than one agent holding both problems and a briefing long enough to blunt
both.

## Investigating is parallel; acting is not

Every specialist only reads, so they investigate simultaneously. Their
proposals are then applied ONE AT A TIME, in an order taken from the asset
lineage: if the package is built from the dub stem, the stem is repaired first,
because rebuilding the package before the repair lands is work thrown away.

Nothing is applied without `--approve`, exactly as in the single-agent path.
The autonomy tier still governs each proposal individually.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.investigate import investigate  # noqa: E402
from agents.ledger import RejectionLog  # noqa: E402
from agents.mcp import grafana_client  # noqa: E402
from agents.signal import Signal  # noqa: E402
from agents.swarm import order_repairs, swarm  # noqa: E402
from agents.wake import Incident  # noqa: E402
from telemetry.genai import GenAI  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import load_env, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.swarm.cli")


def _dependencies(store_root: Path, market: str) -> dict[str, list[str]]:
    """Who is built from whom, for this market. The schedule, from lineage."""
    from media.store import Store

    children: dict[str, list[str]] = {}
    for asset in Store(store_root).all_assets():
        for parent in asset.parents:
            if parent.asset_id == asset.id:
                continue                   # a repair's own predecessor
            if asset.market == market:
                children.setdefault(parent.asset_id, []).append(asset.id)
    return children


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--scene", default="S03")
    ap.add_argument("--title", default="SINTEL")
    ap.add_argument("--store", default="out/store")
    ap.add_argument("--limit", type=int, default=4,
                    help="how many specialists may reason at once")
    ap.add_argument("--approve", action="store_true",
                    help="carry out the proposals, in lineage order")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    env = load_env()
    signal = Signal(grafana_client(env))
    tracer, meter = setup("continuity-swarm")
    genai = GenAI(tracer, Instruments(meter), "conductor",
                  rejections=RejectionLog(Path(args.store)))

    incident = Incident(
        fingerprint=f"swarm:{args.title}:{args.market}",
        alertname="MarketNotReleaseReady", action="investigate",
        status="firing", title_id=args.title, market=args.market,
        severity="critical", started_at="",
    )

    try:
        with tracer.start_as_current_span(f"swarm.{args.market}") as root:
            root.set_attribute("continuity.market", args.market)

            inv = investigate(signal, incident)
            print(f"\ninvestigation: {inv.summary()}")
            if inv.resolved_before_we_arrived:
                print("\nthe market recovered before we looked; nothing to do")
                return 0

            failing = sorted(f.subject for f in inv.findings if f.subject)
            print(f"failing dimensions: {', '.join(failing) or 'none'}")

            from media.model import client as model_client
            result = asyncio.run(swarm(
                model_client(env), signal, inv, genai,
                failing=failing, scene=args.scene, limit=args.limit,
            ))

            print(f"\n{len(result.verdicts)} specialist(s) reported:\n")
            for verdict in result.verdicts:
                head = f"  {verdict.name:<14} [{', '.join(verdict.checks)}]"
                if verdict.error:
                    print(f"{head}\n      ERROR {verdict.error[:120]}")
                    continue
                conclusion = verdict.conclusion
                if conclusion is None:
                    print(f"{head}\n      no conclusion")
                elif conclusion.acted and conclusion.intent is not None:
                    intent = conclusion.intent
                    print(f"{head}\n      PROPOSES {intent.strategy.value} "
                          f"{intent.params}")
                    print(f"      predicts {intent.prediction.describe()}")
                    print(f"      because  {intent.rationale[:150]}")
                else:
                    print(f"{head}\n      ESCALATES {conclusion.reason}")
                    print(f"      {conclusion.summary[:200]}")

            proposals = order_repairs(
                result.repairs, _dependencies(Path(args.store), args.market))
            if proposals:
                print(f"\napply in this order (from asset lineage):")
                for i, verdict in enumerate(proposals, 1):
                    intent = verdict.conclusion.intent
                    print(f"  {i}. {verdict.name}: {intent.strategy.value} on "
                          f"{intent.target_asset_id.split(':', 1)[-1]}")

            if not args.approve:
                print("\nNOT ACTING. Re-run with --approve to carry these out, "
                      "or use scripts/repair.py --approve to run one.")
                return 0

            # Applying is left to the single-agent path deliberately: it owns
            # the act -> re-measure -> verify -> ledger sequence, and a second
            # implementation of that would be a second set of rules to keep
            # honest. The swarm decides WHAT and in what order; repair.py is
            # what does it.
            print("\nRun each in order:")
            for verdict in proposals:
                print(f"  python scripts/repair.py --market {args.market} "
                      f"--approve   # {verdict.name}")
            return 0
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
