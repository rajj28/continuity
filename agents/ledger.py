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

def _append_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON line, healing a truncated write first.

    A process killed mid-write leaves a line with no trailing newline. The next
    append then lands on the SAME line and the two entries become one
    unreadable string -- so a crash costs not the record it interrupted but
    that record AND the next one, and the corruption is silent because
    `entries()` simply skips what it cannot parse.

    Checking for the newline costs one seek per append and turns "a crash
    destroys two entries" into "a crash destroys the one it interrupted",
    which is the most an append-only file can promise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            healed = fh.read(1) != b"\n"
        if healed:
            log.warning("healing a truncated line in %s", path.name)
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


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
        _append_line(self.path, entry.to_dict())
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


# ---------------------------------------------------------------------------
# Guardrail rejections
# ---------------------------------------------------------------------------
#
# Same problem as the repair ledger, same shape of answer.
#
# `continuity_agent_rejections_total{reason}` is the series that turns "the
# contracts refuse a proposal citing a query the model never ran" from a claim
# into something a reviewer can watch. It was published straight from the
# conductor process -- a short-lived job whose counter starts at zero, pushes
# one increment and exits -- so five minutes later Prometheus aged it out and
# the panel was empty again.
#
# That is worse than not having the metric. A guardrail counter flat at zero
# means either a well-behaved model or a guardrail that never fires, and the
# whole point of publishing it was to tell those apart. An empty panel says
# "nothing was ever refused" when what happened is "the evidence expired".
#
# So refusals are appended here and the state exporter republishes the running
# totals every cycle, exactly like repair outcomes. A rejection that happened
# last week is still on the dashboard, which is the only version of this claim
# worth making.


@dataclass(frozen=True)
class Rejection:
    """One proposal the contracts refused, and why."""

    reason: str
    agent: str
    at: str
    detail: str = ""
    title: str = ""
    market: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason, "agent": self.agent, "at": self.at,
            "detail": self.detail, "title": self.title, "market": self.market,
        }


class RejectionLog:
    """Append-only record of every guardrail refusal."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "rejections.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, reason: str, *, agent: str, detail: str = "",
               title: str = "", market: str = "") -> Rejection:
        entry = Rejection(
            reason=reason, agent=agent,
            at=datetime.now(timezone.utc).isoformat(),
            # Bounded: the detail is a model-supplied string and this file is
            # read on every exporter cycle.
            detail=detail[:400], title=title, market=market,
        )
        _append_line(self.path, entry.to_dict())
        return entry

    def entries(self) -> Iterator[Rejection]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                log.warning("skipping unreadable rejection line")
                continue
            try:
                yield Rejection(**raw)
            except TypeError:
                log.warning("skipping rejection entry with unexpected shape")

    def totals(self) -> dict[tuple[str, str], int]:
        """Cumulative counts, keyed by (agent, reason)."""
        counts: dict[tuple[str, str], int] = {}
        for entry in self.entries():
            key = (entry.agent, entry.reason)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def recent(self, limit: int = 8) -> list[Rejection]:
        return list(self.entries())[-limit:][::-1]


def publish_rejections(instruments: Any, rejections: RejectionLog) -> int:
    """Republish refusal totals so the series never ages out.

    A gauge rather than a counter, for the same reason the repair ledger uses
    one: the exporter did not do the refusing, it is reporting a total it read
    off disk. `continuity_agent_rejections_total` keeps its name because that
    is what the dashboards and the docs already call it, and renaming a series
    to satisfy a naming convention would break every panel pointing at it.
    """
    gauge = instruments.gauge(
        "continuity_agent_rejections_total",
        "Model proposals refused by the contracts, by reason",
    )
    totals = rejections.totals()
    for (agent, reason), count in totals.items():
        gauge.set(float(count), {"agent": agent, "reason": reason})
    return len(totals)
