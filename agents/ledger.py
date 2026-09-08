"""The autonomy ledger, made durable.

`continuity_repairs_total` is what `earned_tier` reads to decide how much
authority a strategy has. The first version published it straight from the
repair process, which does not work: a repair is a short-lived job, its counter
starts at zero, it pushes one increment and exits, and five minutes later
Prometheus ages the series out. An autonomy ladder built on that forgets
everything a strategy ever did, every five minutes, which is not a ladder.

So outcomes are appended to a file and the state exporter republishes the
running totals on every cycle, exactly like the measurements. The ledger is
then durable across restarts and always current in Grafana, and the agent reads
its authority from the same series a human sees on a dashboard.

## Append-only, deliberately

There is no method here that edits or removes an entry. A strategy's record is
a record of what happened, including the times it did not work -- and a system
that could quietly forget its failures would produce an autonomy figure nobody
should trust. Correcting a mistaken entry means appending a correction with a
reason, which leaves both visible.

JSONL rather than a database because it is the smallest thing that is
append-only by construction, survives a crash mid-write with at most one
truncated line, and can be read by anyone with a text editor when they want to
know why an agent was allowed to do something.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("continuity.ledger")

OUTCOMES = ("succeeded", "lucky", "failed")


@dataclass(frozen=True)
class Entry:
    """One verified repair. Frozen: history does not get edited."""

    strategy: str
    market: str
    outcome: str
    predicted: str
    observed: float
    baseline: float
    at: str
    asset_id: str = ""
    sha256: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy, "market": self.market,
            "outcome": self.outcome, "predicted": self.predicted,
            "observed": self.observed, "baseline": self.baseline,
            "at": self.at, "asset_id": self.asset_id, "sha256": self.sha256,
            "note": self.note,
        }


class Ledger:
    """Append-only repair history on disk."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "repairs.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, result: Any, *, market: str, asset_id: str = "",
               sha256: str = "", note: str = "") -> Entry:
        entry = Entry(
            strategy=result.intent.strategy.value,
            market=market,
            outcome=result.outcome,
            predicted=result.intent.prediction.describe(),
            observed=float(result.observed),
            baseline=float(result.intent.prediction.baseline),
            at=datetime.now(timezone.utc).isoformat(),
            asset_id=asset_id, sha256=sha256, note=note,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
        return entry

    def entries(self) -> Iterator[Entry]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # A line truncated by a crash mid-write. Skipped rather than
                # fatal: losing one entry is better than losing the ability to
                # read the rest of the history.
                log.warning("skipping unreadable ledger line")
                continue
            try:
                yield Entry(**raw)
            except TypeError:
                log.warning("skipping ledger entry with unexpected shape")

    def totals(self) -> dict[tuple[str, str, str], int]:
        """Cumulative counts, keyed by (strategy, market, outcome)."""
        counts: dict[tuple[str, str, str], int] = {}
        for entry in self.entries():
            if entry.outcome not in OUTCOMES:
                continue
            key = (entry.strategy, entry.market, entry.outcome)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def history(self, strategy: str, market: str) -> dict[str, int]:
        """The shape `earned_tier` expects, computed from disk."""
        totals = self.totals()
        return {
            outcome: totals.get((strategy, market, outcome), 0)
            for outcome in OUTCOMES
        }


def publish(instruments: Any, ledger: Ledger) -> int:
    """Republish the running totals so the series never ages out.

    Called every state-exporter cycle. The counter is set from the file rather
    than incremented, because the exporter is not the thing that performed the
    repairs -- it is reporting a total it read.
    """
    gauge = instruments.gauge(
        "continuity_repairs_total",
        "Verified repair outcomes by strategy, market and outcome",
    )
    totals = ledger.totals()
    for (strategy, market, outcome), count in totals.items():
        gauge.set(float(count), {
            "strategy": strategy, "market": market, "outcome": outcome,
        })
    return len(totals)
