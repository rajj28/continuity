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

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .deliverables import completeness
from .technical import conformance, measure_technical

# The English copy a localised record must not still be.
SOURCE_SYNOPSIS = ("A lone warrior searches a hostile world for the "
                   "dragon she raised from a hatchling.")
from .types import Measurement


def evaluate(
    market: str,
    profile: dict[str, Any],
    assets: Iterable[Any],
    technical: list[Measurement],
    *,
    on: date,
    master_sha256: str = "",
    store_root: Path | None = None,
    source_title: str = "Sintel",
    source_synopsis: str = SOURCE_SYNOPSIS,
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

    # Certification. Like rights, granted by a body outside this system and
    # repairable by nobody in it -- but unlike rights it is tied to a specific
    # cut, so the master hash is part of the question.
    from media.ratings import certification
    certified = certification(
        market, profile.get("ratings_body", ""), master_sha256, on=on,
    )
    results.append(("certified", certified.valid, certified.reason))

    # The storefront record a viewer actually sees. Most commonly forgotten
    # localisation there is: the dub gets made and the store still says the
    # English title.
    # The stores are opened HERE rather than passed in. Every caller has to
    # supply the same inputs or the answers diverge, and the last time this was
    # a parameter the report told an operator a market had no metadata record
    # while the file sat on disk and the dashboard said otherwise.
    from media.metadata import (
        MetadataStore, check_forced_narrative, check_metadata,
    )
    root = Path(store_root) if store_root else None
    meta = check_metadata(
        market,
        MetadataStore(root).get("SINTEL", market) if root else None,
        source_title=source_title, source_synopsis=source_synopsis,
    )
    results.append(("metadata_localised", meta.complete, meta.reason))

    # Subtitles for on-screen text, which must appear even with subtitles off.
    # Only required where the picture actually has such text -- a question
    # about the cut, not about the market.
    has_track = any(
        getattr(a, "kind", "") == "FORCED_NARRATIVE"
        and getattr(a, "market", None) == market
        for a in assets
    )
    forced = check_forced_narrative(
        market, forced_narrative_need(root), has_track,
        master_sha256=master_sha256,
    )
    results.append(("forced_narrative", forced.complete, forced.reason))
    return results


def forced_narrative_need(root: Path | None):
    """The recorded on-screen-text analysis, if one exists.

    Absent means nobody has looked, which is a different answer from "there is
    nothing there" and is reported as such rather than assumed either way.
    """
    if root is None:
        return None
    from media.metadata import ForcedNarrativeNeed
    path = Path(root) / "forced_narrative.json"
    if not path.exists():
        return None
    try:
        return ForcedNarrativeNeed(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


def technical_of(path: Path) -> list[Measurement]:
    """Measured once per cycle, not once per market: every market judges the
    same master, and probing it five times would say the same thing five
    times more slowly."""
    return measure_technical(path)
