"""Publish per-market thresholds as Prometheus series.

The verdict rules compare a measurement against `market_threshold`, so the
threshold has to exist in Prometheus rather than inside the rule text. That
indirection buys three things:

  - the rule is a pure join between two published facts, identical for every
    market, instead of a wall of per-market literals
  - changing a tolerance is a data change that is visible in Grafana, with
    history, rather than a rule edit
  - the agent can ask what standard it is being judged against, instead of
    being told -- `market_threshold{market="de-DE"}` is a legitimate step in
    an investigation

Source of truth stays assets/market_profiles.json, which is versioned in the
repo and reviewable in a diff.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from media.qc.profiles import load_profiles  # noqa: E402
from telemetry.metrics import Instruments  # noqa: E402
from telemetry.otel import setup, shutdown  # noqa: E402

THRESHOLD_METRIC = "market_threshold"

# Profile path -> the `requirement` label the recording rules join on.
# Keys are dotted paths into a MarketProfile.
PUBLISHED: dict[str, str] = {
    "delivery.sync_tolerance_ms": "dub_sync_max_ms",
    "delivery.subtitle_max_cps": "subtitle_max_cps",
    "delivery.subtitle_min_duration_ms": "subtitle_min_duration_ms",
    "delivery.subtitle_max_lines": "subtitle_max_lines",
    "delivery.true_peak_max_dbtp": "true_peak_max_dbtp",
    "delivery.loudness_target_lufs": "loudness_target_lufs",
    "delivery.loudness_tolerance_lu": "loudness_tolerance_lu",
    "quality.semantic_fidelity_floor": "semantic_fidelity_floor",
    "quality.speech_rate_max_wpm": "speech_rate_max_wpm",
}


def _dig(obj: dict, dotted: str):
    for part in dotted.split("."):
        obj = obj[part]
    return obj


def publish(instruments: Instruments) -> int:
    """Emit every threshold for every market. Returns the series count."""
    gauge = instruments.gauge(
        THRESHOLD_METRIC,
        "Per-market delivery threshold, from assets/market_profiles.json",
    )
    count = 0
    for market, profile in load_profiles().items():
        for path, requirement in PUBLISHED.items():
            try:
                value = float(_dig(profile, path))
            except (KeyError, TypeError, ValueError):
                # A market that does not define a requirement simply has no
                # threshold series, which makes its verdict absent rather than
                # passing. Absent is the safe direction.
                continue
            gauge.set(value, {"market": market, "requirement": requirement})
            count += 1
    return count


def main() -> int:
    _, meter = setup("continuity-thresholds")
    instruments = Instruments(meter)
    n = publish(instruments)
    shutdown()
    print(f"published {n} threshold series across "
          f"{len(load_profiles())} markets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
