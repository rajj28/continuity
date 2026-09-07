"""Rights clearance and release windows.

The dimension that makes this a release system rather than a QC tool. A
perfectly dubbed, perfectly conformant package still cannot ship into a
territory if the music cue is not cleared there, if the stock footage licence
excludes it, or if the window has not opened. In a real operation this is the
single most common reason a title is blocked, and it has nothing to do with
how the file sounds.

It is also the dimension where the autonomy ladder earns its keep. Every other
failure in this system has a repair: shift the stem, normalise the level,
shorten the line. **A rights failure has none.** No agent may clear a right,
and no processing can manufacture one. The correct behaviour is to block, name
the missing grant, and escalate to a human who can go and buy it -- which is
why `Clearance.repairable` is always False and why that is a property of the
domain rather than a limitation of the implementation.

The ledger is a versioned JSON file, reviewable in a diff, exactly like the
market profiles. In a real deployment it would be a rights-management system;
the shape of the question does not change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[1] / "assets" / "rights_ledger.json"


class RightsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Grant:
    """One right, in one territory, for one window."""

    right: str
    territories: tuple[str, ...]
    starts: str = ""            # ISO date; empty means already open
    ends: str = ""              # ISO date; empty means no expiry
    note: str = ""

    def covers(self, market: str) -> bool:
        return "WORLD" in self.territories or market in self.territories

    def open_on(self, when: date) -> bool:
        if self.starts and when < date.fromisoformat(self.starts):
            return False
        if self.ends and when > date.fromisoformat(self.ends):
            return False
        return True


@dataclass
class Clearance:
    """Whether a market's required rights are all granted and in window."""

    market: str
    required: tuple[str, ...]
    granted: tuple[str, ...]
    missing: tuple[str, ...]
    not_yet_open: tuple[tuple[str, str], ...] = ()   # (right, opens-on)
    expired: tuple[tuple[str, str], ...] = ()        # (right, ended-on)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def cleared(self) -> bool:
        return not (self.missing or self.not_yet_open or self.expired)

    @property
    def repairable(self) -> bool:
        """Always False, and that is the point.

        A missing right is not a defect in an asset. It is an absent legal
        permission, and the only thing that resolves it happens outside this
        system entirely. An agent that "repaired" this would be inventing
        authority it does not have.
        """
        return False

    def reason(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"no grant for {', '.join(self.missing)}")
        for right, when in self.not_yet_open:
            parts.append(f"{right} opens {when}")
        for right, when in self.expired:
            parts.append(f"{right} expired {when}")
        return "; ".join(parts) or "all rights cleared and in window"


@lru_cache(maxsize=1)
def load_ledger(path: Path | None = None) -> list[Grant]:
    source = path or LEDGER
    if not source.exists():
        raise RightsError(
            f"no rights ledger at {source}. A release system that cannot read "
            f"its rights cannot answer whether anything may ship, and guessing "
            f"is not an option here."
        )
    raw = json.loads(source.read_text(encoding="utf-8"))
    return [
        Grant(
            right=g["right"],
            territories=tuple(g.get("territories", [])),
            starts=g.get("starts", ""),
            ends=g.get("ends", ""),
            note=g.get("note", ""),
        )
        for g in raw["grants"]
    ]


def clearance(
    market: str,
    required: list[str],
    *,
    on: date | None = None,
    ledger: list[Grant] | None = None,
) -> Clearance:
    """Can this market ship, as far as rights are concerned?

    `on` defaults to today. Passing it explicitly is what makes a window test
    deterministic, and what lets an operator ask "will this be clear on the
    30th?" -- which is the question a release calendar is actually made of.
    """
    when = on or datetime.utcnow().date()
    grants = ledger if ledger is not None else load_ledger()

    granted: list[str] = []
    missing: list[str] = []
    not_yet: list[tuple[str, str]] = []
    expired: list[tuple[str, str]] = []

    for right in required:
        applicable = [g for g in grants if g.right == right and g.covers(market)]
        if not applicable:
            missing.append(right)
            continue
        # Any one grant covering the territory and open today is enough.
        if any(g.open_on(when) for g in applicable):
            granted.append(right)
            continue
        # There is a grant, but not now. Which way it failed matters: a window
        # that has not opened is a scheduling fact an operator can plan around,
        # while an expired one is a renewal nobody did.
        future = [g for g in applicable if g.starts and when < date.fromisoformat(g.starts)]
        if future:
            not_yet.append((right, min(g.starts for g in future)))
        else:
            past = [g for g in applicable if g.ends]
            expired.append((right, max(g.ends for g in past) if past else "unknown"))

    return Clearance(
        market=market,
        required=tuple(required),
        granted=tuple(granted),
        missing=tuple(missing),
        not_yet_open=tuple(not_yet),
        expired=tuple(expired),
        detail={"evaluated_on": when.isoformat(), "grants_considered": len(grants)},
    )
