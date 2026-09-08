"""The market-level release checks, in one place both callers can use.

Technical conformance, rights clearance and package completeness are evaluated
identically whether a human is running the report or the exporter is
republishing state every thirty seconds. Keeping the evaluation here rather
than inside either caller is what stops those two answers drifting apart --
a report that disagreed with the dashboard would be worse than having neither.

Continuous republishing is not an optimisation. Prometheus ages a series out
five minutes after its last sample, so a one-shot `release_check.py` run puts
`market_check_met` into Grafana and then lets it evaporate, and the coverage
gate correctly reports the market as unmeasured. That is the gate working, and
it is also a market that silently stops being judged on its rights a few
minutes after anyone last looked. These facts are as perishable as every other
one, so they live on the same heartbeat.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .deliverables import completeness
from .technical import conformance, measure_technical
from .types import Measurement


def evaluate(
    market: str,
    profile: dict[str, Any],
    assets: Iterable[Any],
    technical: list[Measurement],
    *,
    on: date,
) -> list[tuple[str, bool, str]]:
    """Every market-level check, as (requirement, met, detail).

    The `tech_` prefix keeps these requirement labels disjoint from the
    scene-level ones, which is what lets both land in the same
    `market_requirement_met` series without colliding.
    """
    results: list[tuple[str, bool, str]] = [
        (f"tech_{name}", met, detail)
        for name, met, detail in conformance(technical, profile.get("technical", {}))
    ]

    from media.rights import clearance
    cleared = clearance(market, list(profile.get("rights_required", [])), on=on)
    results.append(("rights_cleared", cleared.cleared, cleared.reason()))

    complete = completeness(market, profile, assets)
    results.append(("deliverables_complete", complete.complete, complete.reason()))
    return results


def technical_of(path: Path) -> list[Measurement]:
    """Measured once per cycle, not once per market: every market judges the
    same master, and probing it five times would say the same thing five
    times more slowly."""
    return measure_technical(path)
