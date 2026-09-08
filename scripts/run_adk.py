"""Run one incident through the ADK Conductor against the live stack.

Same investigation, same tools, same contracts as scripts/run_conductor.py --
the Agent Development Kit runs the loop instead of a hand-rolled one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.adk_conductor import conduct_adk  # noqa: E402
from agents.investigate import investigate  # noqa: E402
from agents.mcp import grafana_client  # noqa: E402
from agents.signal import Signal  # noqa: E402
from agents.wake import Incident  # noqa: E402
from telemetry.genai import GenAI  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import load_env, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.adk_run")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--title", default="SINTEL")
    ap.add_argument("--scene", default="S03")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    env = load_env()
    signal = Signal(grafana_client(env))
    tracer, meter = setup("continuity-adk")
    genai = GenAI(tracer, Instruments(meter), "conductor")

    from media.model import describe
    log.info("model backend: %s", describe())

    incident = Incident(
        fingerprint=f"adk:{args.title}:{args.market}",
        alertname="MarketNotReleaseReady", action="investigate",
        status="firing", title_id=args.title, market=args.market,
        severity="critical", started_at="",
    )

    try:
        with tracer.start_as_current_span(f"incident.{args.market}"):
            genai.step("investigate")
            inv = investigate(signal, incident)
            print(f"\ninvestigation: {inv.summary()}")
            for f in inv.findings:
                print(f"  - {f.claim}")
            if inv.resolved_before_we_arrived:
                print("\nnothing to do")
                return 0

            session = await conduct_adk(signal, inv, genai, scene=args.scene)

        print(f"\n--- ADK run complete ---")
        print(f"queries actually run: {len(session.toolbox.gathered)}")
        for q in list(session.toolbox.gathered)[:8]:
            print(f"   {q}")
        if session.rejections:
            print(f"\nrejected proposals: {len(session.rejections)}")
            for r in session.rejections:
                print(f"   x {r[:150]}")
        if session.intent:
            i = session.intent
            print(f"\nREPAIR   {i.strategy.value}  {i.params}")
            print(f"predicts {i.prediction.describe()}")
            print(f"tier     {i.tier.name}")
            print(f"because  {i.rationale[:220]}")
            print(f"evidence {len(i.justification)} cited")
        elif session.escalation:
            print(f"\nESCALATE {session.escalation['reason']}")
            print(f"         {session.escalation['summary'][:300]}")
        else:
            print("\nno conclusion reached")
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
