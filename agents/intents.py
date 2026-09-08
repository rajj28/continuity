"""Pending repair intents, so approval can happen later than reasoning.

RECOMMEND means "propose, and let a human decide". A human is not standing at
the terminal when the model finishes thinking, so an intent that only existed
in memory for the length of one process was never really proposing anything --
it was asking a question and hanging up.

So a proposal that needs a human is written down: the strategy, the parameters,
the falsifiable prediction, and every piece of evidence that was cited for it.
An operator can read the file, and approving it later runs exactly what was
proposed rather than whatever the model would say if asked again.

## Why a stored intent expires

The evidence in a proposal is a measurement of a moment. If the world moved
between the proposal and the approval -- another repair landed, the master was
re-graded, someone re-ran the dub -- then acting on it applies a fix computed
for a file that no longer exists. `is_stale` re-reads the baseline and refuses
when it has drifted, because a shift of exactly 298.2 ms is only correct for a
stem that is still 298.2 ms late.

That check is the reason this is safe to have at all. Without it, persistence
would just be a way to act on stale reasoning with a clean conscience.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.contracts import (
    AutonomyTier,
    Direction,
    Evidence,
    Prediction,
    RepairIntent,
    Strategy,
)

# How far the measured baseline may drift before a stored intent is refused.
# 5% rather than an absolute figure because the same tolerance has to be
# meaningful for a 298 ms sync offset and a -17.8 LUFS loudness reading.
DRIFT_TOLERANCE = 0.05


class StaleIntent(RuntimeError):
    """The world moved between proposal and approval."""


def to_dict(intent: RepairIntent, *, incident: str = "") -> dict[str, Any]:
    return {
        "incident": incident,
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "strategy": intent.strategy.value,
        "target_asset_id": intent.target_asset_id,
        "params": intent.params,
        "tier": intent.tier.name,
        "rationale": intent.rationale,
        "supersedes": intent.supersedes,
        "prediction": {
            "series": intent.prediction.series,
            "market": intent.prediction.market,
            "scene": intent.prediction.scene,
            "direction": intent.prediction.direction.value,
            "target_value": intent.prediction.target_value,
            "baseline": intent.prediction.baseline,
        },
        "justification": [asdict(e) for e in intent.justification],
    }


def from_dict(raw: dict[str, Any]) -> RepairIntent:
    prediction = raw["prediction"]
    return RepairIntent(
        strategy=Strategy(raw["strategy"]),
        target_asset_id=raw["target_asset_id"],
        params=raw["params"],
        prediction=Prediction(
            series=prediction["series"], market=prediction["market"],
            scene=prediction["scene"],
            direction=Direction(prediction["direction"]),
            target_value=float(prediction["target_value"]),
            baseline=float(prediction["baseline"]),
        ),
        justification=[Evidence(**e) for e in raw["justification"]],
        tier=AutonomyTier[raw["tier"]],
        supersedes=raw.get("supersedes"),
        rationale=raw.get("rationale", ""),
    )


class IntentStore:
    """Pending proposals, one file per market."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "intents"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, title: str, market: str) -> Path:
        return self.root / f"{title}~{market}.json"

    def put(self, intent: RepairIntent, *, title: str, market: str,
            incident: str = "") -> Path:
        path = self._path(title, market)
        path.write_text(
            json.dumps(to_dict(intent, incident=incident), indent=2,
                       sort_keys=True),
            encoding="utf-8",
        )
        return path

    def get(self, title: str, market: str) -> RepairIntent | None:
        path = self._path(title, market)
        if not path.exists():
            return None
        try:
            return from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # An unreadable proposal is no proposal. Better to re-reason than
            # to guess at what half a file meant.
            return None

    def proposed_at(self, title: str, market: str) -> str:
        path = self._path(title, market)
        if not path.exists():
            return ""
        try:
            return json.loads(path.read_text(encoding="utf-8"))["proposed_at"]
        except (json.JSONDecodeError, KeyError):
            return ""

    def clear(self, title: str, market: str) -> None:
        """Drop a proposal once it has been acted on.

        A consumed intent left lying around is one an operator could approve
        twice, applying a 298 ms shift to a stem that has already had one.
        """
        self._path(title, market).unlink(missing_ok=True)


def is_stale(intent: RepairIntent, observed: float,
             tolerance: float = DRIFT_TOLERANCE) -> bool:
    """Has the measurement moved since this was proposed?

    A shift of exactly 298.2 ms is only correct for a stem that is still
    298.2 ms late. If the number has drifted, the parameters were computed for
    a file that no longer exists.
    """
    baseline = intent.prediction.baseline
    if baseline == 0:
        return observed != 0
    return abs(observed - baseline) / abs(baseline) > tolerance


def check_fresh(intent: RepairIntent, observed: float | None) -> None:
    """Raise unless the stored proposal still applies."""
    if observed is None:
        raise StaleIntent(
            f"{intent.prediction.series} is no longer being measured, so the "
            f"stored proposal cannot be checked against anything. Re-run the "
            f"investigation."
        )
    if is_stale(intent, observed):
        raise StaleIntent(
            f"{intent.prediction.series} was {intent.prediction.baseline:g} "
            f"when this was proposed and is {observed:g} now. The parameters "
            f"were computed for a file that no longer exists. Re-run the "
            f"investigation."
        )
