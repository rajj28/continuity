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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from media.qc.report import QCStore  # noqa: E402
from media.store import Store  # noqa: E402
from telemetry.exporters.thresholds import publish as publish_thresholds  # noqa: E402
from telemetry.metrics import MEASUREMENT_SERIES, Instruments  # noqa: E402
from telemetry.otel import setup, shutdown  # noqa: E402

log = logging.getLogger("continuity.state")

# Matches the recording rules' evaluation interval. Publishing slower than the
# ruler evaluates would let a series lapse between scrapes; publishing much
# faster just burns free-tier ingestion.
DEFAULT_INTERVAL_S = 30.0


@dataclass
class Cycle:
    """What one pass published. Returned so the caller can assert on it."""

    thresholds: int = 0
    measurements: int = 0
    assets: int = 0
    stale: int = 0
    unmeasured: int = 0

    def describe(self) -> str:
        return (
            f"{self.thresholds} thresholds, {self.measurements} measurements "
            f"across {self.assets} assets ({self.stale} stale, "
            f"{self.unmeasured} unmeasured)"
        )


def publish_once(
    instruments: Instruments, store: Store, qc: QCStore
) -> Cycle:
    cycle = Cycle(thresholds=publish_thresholds(instruments))

    for asset in store.all_assets():
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

        for measurement in report.measurements:
            if measurement.key not in MEASUREMENT_SERIES:
                continue
            instruments.record_measurement(
                measurement, title=asset.title_id, scene=asset.scene_id,
                market=asset.market,
            )
            cycle.measurements += 1

    return cycle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default="out/store")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
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
            cycle = publish_once(instruments, store, qc)
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
