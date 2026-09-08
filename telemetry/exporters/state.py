"""Keep Grafana's picture of the world current.

Prometheus marks a series stale five minutes after its last sample, so a
one-shot publish is not a fact -- it is a fact that evaporates. The verdict
rules evaluate every 30 seconds against whatever is present, and a series that
has aged out is indistinguishable from one that was never measured. Both mean
"unmeasured", both now block a market, and neither is what we meant if the
asset is sitting on disk with a perfectly good QC report next to it.

So this is a loop, and it is the bridge between the content-addressed store and
Prometheus. It publishes three kinds of fact:

  thresholds     what each market demands (from the versioned profiles)
  measurements   what each asset actually measures (from its QC report)
  diagnostics    what explains those measurements -- whether drift is
                 systematic, how far the worst line overran, how many lines
                 did. Nothing in the recording rules joins these and nothing
                 should: they explain a failure rather than constituting one.
                 They are published anyway so the agent reasons from facts a
                 human can also see on a dashboard.
  staleness      whether each asset's recorded parent hash still matches

Staleness is computed here rather than stored, because it is a pure function of
hashes that are already on disk -- `recorded_parent_hash != parent.current_hash`
-- and a stored flag would need invalidating, which is the bug this design
exists to avoid.

Run it as a sidecar locally, or as a Cloud Run service with min-instances=1.
It holds no state of its own: kill it, restart it, and within one interval
Grafana is correct again.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.ledger import Ledger  # noqa: E402
from agents.ledger import publish as publish_ledger  # noqa: E402
from media.qc.profiles import load_profiles  # noqa: E402
from media.qc.release import evaluate, technical_of  # noqa: E402
from media.qc.report import QCStore  # noqa: E402
from media.store import Store  # noqa: E402
from media.qc.types import ProbeError  # noqa: E402
from telemetry.exporters.thresholds import publish as publish_thresholds  # noqa: E402
from telemetry.metrics import (  # noqa: E402
    DIAGNOSTIC_SERIES,
    MEASUREMENT_SERIES,
    REPORT_DIAGNOSTIC_SERIES,
    Instruments,
    record_diagnostics,
)
from telemetry.otel import setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.state")

# Matches the recording rules' evaluation interval. Publishing slower than the
# ruler evaluates would let a series lapse between scrapes; publishing much
# faster just burns free-tier ingestion.
DEFAULT_INTERVAL_S = 30.0
TITLE = "SINTEL"


@dataclass
class Cycle:
    """What one pass published. Returned so the caller can assert on it."""

    thresholds: int = 0
    measurements: int = 0
    assets: int = 0
    market_checks: int = 0
    repairs: int = 0
    stale: int = 0
    unmeasured: int = 0
    diagnostics: int = 0

    def describe(self) -> str:
        return (
            f"{self.thresholds} thresholds, {self.measurements} measurements, "
            f"{self.diagnostics} diagnostics, {self.market_checks} market "
            f"checks, {self.repairs} ledger entries across {self.assets} "
            f"assets ({self.stale} stale, "
            f"{self.unmeasured} unmeasured)"
        )


def publish_once(
    instruments: Instruments, store: Store, qc: QCStore,
    *, master: Path | None = None, on: date | None = None,
) -> Cycle:
    cycle = Cycle(thresholds=publish_thresholds(instruments))
    assets = store.all_assets()
    # The autonomy ledger. Republished from disk rather than
    # incremented, because a repair is a short-lived job whose
    # counter would age out five minutes after it exits -- and a
    # ladder that forgets everything every five minutes is not one.
    cycle.repairs = publish_ledger(instruments, Ledger(store.root))

    # -- market-level: technical, rights, deliverables ---------------------
    # As perishable as every other fact here. A one-shot release check puts
    # these into Grafana and then lets them age out, at which point the
    # coverage gate correctly reports the market as unmeasured -- which is the
    # gate working, and also a market that quietly stopped being judged on its
    # rights a few minutes after anyone last looked.
    if master is not None and master.exists():
        try:
            technical = technical_of(master)
            gauge = instruments.gauge(
                "market_check_met",
                "1 when a market-level release check is satisfied",
            )
            for market, profile in load_profiles().items():
                for requirement, met, _detail in evaluate(
                    market, profile, assets, technical,
                    on=on or datetime.utcnow().date(),
                ):
                    gauge.set(1.0 if met else 0.0, {
                        "title": TITLE, "market": market,
                        "requirement": requirement,
                    })
                    cycle.market_checks += 1
        except ProbeError as exc:
            # A dead ffprobe must not take the measurement publishing with it.
            # The market-level series simply go absent, which the coverage gate
            # already treats as blocking -- the safe direction.
            log.error("market-level checks skipped: %s", exc)

    for asset in assets:
        # Only assets that belong to a market carry market-judged measurements.
        # A master or a scene cut has no market, and publishing one under a
        # blank label would create a series the rules cannot join on.
        if not asset.market or not asset.scene_id:
            continue
        cycle.assets += 1

        stale = bool(store.is_stale(asset))
        instruments.set_stale(
            stale, title=asset.title_id, scene=asset.scene_id,
            market=asset.market,
        )
        cycle.stale += int(stale)

        report = qc.get(asset.sha256)
        if report is None:
            # Deliberately no measurement series at all. Publishing a zero
            # would read as a passing measurement; absent reads as unmeasured,
            # which is what it is, and the coverage gate blocks on it.
            cycle.unmeasured += 1
            log.debug("no QC report for %s (%s)", asset.id, asset.sha256[:12])
            continue

        labels = {
            "title": asset.title_id, "scene": asset.scene_id,
            "market": asset.market,
        }
        for measurement in report.measurements:
            if measurement.key not in MEASUREMENT_SERIES:
                continue
            instruments.record_measurement(
                measurement, title=asset.title_id, scene=asset.scene_id,
                market=asset.market,
            )
            cycle.measurements += 1
            # Diagnostics ride along with the measurement that produced them.
            # Nothing in the recording rules joins these, and nothing should --
            # they explain a failure rather than constituting one -- but the
            # agent has to reason from facts a human can also see.
            cycle.diagnostics += record_diagnostics(
                instruments, measurement.detail, DIAGNOSTIC_SERIES, labels
            )

        cycle.diagnostics += record_diagnostics(
            instruments, report.detail, REPORT_DIAGNOSTIC_SERIES, labels
        )

    return cycle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="out/store")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--master", default="out/scenes/S03.mp4",
                        help="the file judged against each market's spec")
    parser.add_argument("--on", help="evaluate rights windows on this ISO date")
    parser.add_argument("--once", action="store_true",
                        help="publish a single cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    root = Path(args.store)
    store, qc = Store(root), QCStore(root)
    _, meter = setup("continuity-state")
    instruments = Instruments(meter)

    try:
        while True:
            cycle = publish_once(
                instruments, store, qc, master=Path(args.master),
                on=date.fromisoformat(args.on) if args.on else None,
            )
            log.info("published %s", cycle.describe())
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        # Mandatory: the periodic reader has an unflushed window, and a
        # short-lived process that skips this silently drops its last cycle.
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
