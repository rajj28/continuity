"""Run one control loop against the live stack: investigate, reason, decide.

Not a demo harness. This is the path the Grafana webhook takes -- the same
Investigation, the same Conductor, the same guardrails -- with the incident
supplied on the command line instead of by an alert, so it can be exercised
before the webhook has a public address.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.conductor import conduct  # noqa: E402
from agents.ledger import RejectionLog  # noqa: E402
from agents.investigate import investigate  # noqa: E402
from agents.mcp import grafana_client  # noqa: E402
from agents.signal import Signal  # noqa: E402
from agents.wake import Incident  # noqa: E402
from telemetry.genai import GenAI  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import load_env, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.run")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True)
    ap.add_argument("--title", default="SINTEL")
    ap.add_argument("--scene", default="S03")
    ap.add_argument("--store", default="out/store")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    env = load_env()
    signal = Signal(grafana_client(env))
    tracer, meter = setup("continuity-conductor")
    instruments = Instruments(meter)
    genai = GenAI(tracer, instruments, "conductor",
                  rejections=RejectionLog(Path(args.store)))

    incident = Incident(
        fingerprint=f"manual:{args.title}:{args.market}",
        alertname="MarketNotReleaseReady", action="investigate",
        status="firing", title_id=args.title, market=args.market,
        severity="critical", started_at="",
    )

    try:
        with tracer.start_as_current_span(f"incident.{args.market}") as span:
            span.set_attribute("continuity.market", args.market)
            genai.step("investigate")
            inv = investigate(signal, incident)
            print(f"\ninvestigation: {inv.summary()}")
            for f in inv.findings:
                print(f"  - {f.claim}")
            for g in inv.gaps:
                print(f"  ? {g}")
            if inv.resolved_before_we_arrived:
                print("\nnothing to do")
                return 0

            from media.model import client as build_client, describe
            log.info("model backend: %s", describe())
            conclusion = conduct(build_client(), signal, inv, genai,
                                 scene=args.scene)

        print(f"\n--- {conclusion.action.upper()} after {conclusion.turns} turn(s) ---")
        if conclusion.rejections:
            print(f"{len(conclusion.rejections)} proposal(s) rejected:")
            for _, why in conclusion.rejections:
                print(f"  x {why[:150]}")
        if conclusion.acted:
            i = conclusion.intent
            print(f"strategy   {i.strategy.value}  {i.params}")
            print(f"predicts   {i.prediction.describe()}")
            print(f"authority  {i.tier.name}")
            print(f"because    {i.rationale[:300]}")
            print(f"evidence   {len(i.justification)} cited:")
            for e in i.justification:
                print(f"   {e.cite()[:110]}")
        else:
            print(f"reason     {conclusion.reason}")
            print(f"summary    {conclusion.summary[:400]}")
        print(f"\ngathered {len(conclusion.evidence)} piece(s) of evidence")
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
