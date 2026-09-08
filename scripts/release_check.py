"""Market-level release checks: technical, rights, deliverables.

Localisation is one dimension of "can we release this here", and on its own it
is the smallest one. A package that sounds perfect still cannot ship if it is
the wrong frame rate for the platform, if the music cue was never cleared in
that territory, or if the audio description nobody recorded is a legal
requirement there.

These checks are market-level rather than scene-level because that is what they
are. A scene inherits its frame rate from the master; a right is granted to a
territory, not to a shot; a deliverable either exists for a market or does not.
Measuring them per scene would multiply series without adding information and
would let one conformant scene mask a master that never was.

They publish `market_check_met`, distinct from the `market_requirement_met`
that the recording rules roll up from scenes. Two series rather than one
because a recording rule cannot write into a series that is also being pushed
-- the ruler would be fighting the exporter over the same name -- and because
keeping them apart makes it obvious on a dashboard which failures are about a
scene and which are about a market.

Four kinds of failure, and the difference between them is the whole point:

    technical      repairable by processing, sometimes. A wrong sample rate is
                   a resample. A stereo master where 5.1 is required is not.
    deliverables   repairable by BUILDING the missing thing.
    rights         not repairable at all, by anyone in this system.
    certification  likewise, and tied to a specific cut: a certificate granted
                   against an earlier master is not a certificate for this one.

The evaluation itself lives in media/qc/release.py, because the state exporter
runs exactly the same checks every thirty seconds. A report that computed them
independently would eventually disagree with the dashboard, and then nobody
would know which to believe.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media.qc.profiles import load_profiles  # noqa: E402
from media.qc.release import evaluate  # noqa: E402
from media.qc.technical import measure_technical  # noqa: E402
from media.qc.types import ProbeError  # noqa: E402
from media.store import Store  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import asset_span, setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.release")

CHECK_METRIC = "market_check_met"
TITLE = "SINTEL"


def publish_check(
    instruments: Instruments, *, title: str, market: str,
    requirement: str, met: bool,
) -> None:
    instruments.gauge(
        CHECK_METRIC,
        "1 when a market-level release check is satisfied "
        "(technical, rights, deliverables)",
    ).set(
        1.0 if met else 0.0,
        {"title": title, "market": market, "requirement": requirement},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="out/store")
    parser.add_argument("--master", default="out/scenes/S03.mp4",
                        help="the file judged against the technical spec")
    parser.add_argument("--on", help="evaluate rights windows on this ISO date "
                                     "(default: today)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    when = date.fromisoformat(args.on) if args.on else datetime.utcnow().date()
    store = Store(Path(args.store))
    assets = store.all_assets()

    master = Path(args.master)
    if not master.exists():
        raise SystemExit(f"{master} is missing; run scripts/stage1.py first")
    technical = measure_technical(master)
    master_sha = next((a.sha256 for a in assets if a.kind == "MASTER"), "")

    tracer, meter = setup("continuity-release-check")
    instruments = Instruments(meter)
    blocked: dict[str, list[str]] = {}

    try:
        for market, profile in load_profiles().items():
            with asset_span(
                tracer, f"release_check.{market}", stage="release_check",
                title_id=TITLE, market=market,
                extra={"continuity.rights.evaluated_on": when.isoformat()},
            ) as span:
                # The SAME evaluation the state exporter runs every thirty
                # seconds. Calling it rather than repeating it is the point of
                # media/qc/release.py -- this script had drifted into its own
                # copy, which is precisely how a report ends up disagreeing
                # with a dashboard about whether a market can ship.
                results = evaluate(
                    market, profile, assets, technical,
                    on=when, master_sha256=master_sha,
                    store_root=store.root,
                )
                reasons = [
                    f"{requirement}: {detail}"
                    for requirement, met, detail in results if not met
                ]
                for requirement, met, _detail in results:
                    publish_check(instruments, title=TITLE, market=market,
                                  requirement=requirement, met=met)

                if reasons:
                    blocked[market] = reasons
                span.set_attribute("continuity.checks.failing", len(reasons))
                log.info("%-7s %-8s %d of %d checks met", market,
                         "BLOCKED" if reasons else "clear",
                         len(results) - len(reasons), len(results))
    except ProbeError as exc:
        log.error("release check failed: %s", exc)
        return 1
    finally:
        shutdown()

    print()
    if not blocked:
        print("every market clears the market-level checks")
    for market, reasons in blocked.items():
        print(f"{market}")
        for reason in reasons:
            print(f"    {reason}")
    print("\nNot judged here. Grafana decides:")
    print(f'  market_release_ready{{title="{TITLE}"}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
