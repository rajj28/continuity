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
COVERAGE_METRIC = "market_required_checks"

# The scene-level requirements the recording rules evaluate, and the profile
# fields each one needs in order to be judged at all.
#
# This exists because "every requirement I measured passed" is NOT the same
# statement as "this is shippable". If a probe stops emitting -- a crashed
# worker, a market with no dub yet, a renamed series -- min() over the
# survivors happily returns 1 and the market goes green having never been
# checked for sync. Publishing the number of checks a market OWES lets the
# verdict demand that the count present equals the count required, so a
# missing measurement blocks exactly as a failing one does.
# The market-level checks published by scripts/release_check.py. Not derived
# from thresholds -- there is no numeric bar for "are the rights cleared" --
# but they are still checks a market owes, so they count toward coverage
# exactly as the scene-level ones do. Technical requirements are counted from
# what the market's spec actually declares, so a market that states no frame
# rate is not marked down for failing to be measured against one.
TECHNICAL_CHECKS = (
    "video_height", "frame_rate", "audio_channels",
    "audio_sample_rate", "video_codec", "pixel_format",
)
MARKET_CHECKS = ("rights_cleared", "deliverables_complete")

REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "dub_sync": ("delivery.sync_tolerance_ms",),
    "line_overrun": ("delivery.line_overrun_max_ms",),
    "subtitle_rate": ("delivery.subtitle_max_cps",),
    "true_peak": ("delivery.true_peak_max_dbtp",),
    "speech_rate": ("quality.speech_rate_max_wpm",),
    "semantic_fidelity": ("quality.semantic_fidelity_floor",),
    "loudness": ("delivery.loudness_target_lufs", "delivery.loudness_tolerance_lu"),
}

# Profile path -> the `requirement` label the recording rules join on.
# Keys are dotted paths into a MarketProfile.
PUBLISHED: dict[str, str] = {
    "delivery.sync_tolerance_ms": "dub_sync_max_ms",
    "delivery.line_overrun_max_ms": "line_overrun_max_ms",
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


def _has(profile: dict, paths: tuple[str, ...]) -> bool:
    for path in paths:
        try:
            float(_dig(profile, path))
        except (KeyError, TypeError, ValueError):
            return False
    return True


def required_checks(profile: dict) -> list[str]:
    """Every requirement this market owes, scene-level and market-level.

    Both kinds land in the same `market_requirement_met` series -- one written
    by a recording rule rolling up scenes, the other by a rule normalising the
    pushed market-level checks -- so coverage is one count over one series and
    a missing rights evaluation blocks exactly as a missing sync measurement
    does.
    """
    checks = [name for name, paths in REQUIRED_CHECKS.items() if _has(profile, paths)]
    spec = profile.get("technical", {})
    checks += [f"tech_{name}" for name in TECHNICAL_CHECKS if name in spec]
    checks += list(MARKET_CHECKS)
    return checks


def publish(instruments: Instruments) -> int:
    """Emit every threshold for every market. Returns the series count."""
    gauge = instruments.gauge(
        THRESHOLD_METRIC,
        "Per-market delivery threshold, from assets/market_profiles.json",
    )
    coverage = instruments.gauge(
        COVERAGE_METRIC,
        "How many scene-level checks this market must satisfy to be judged",
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
        coverage.set(float(len(required_checks(profile))), {"market": market})
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
