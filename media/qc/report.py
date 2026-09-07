"""QC results, stored the way the assets are: by content hash.

A QC measurement is a pure function of the bytes measured and the probe that
measured them. So the natural key is the asset's SHA-256, and re-measuring
identical bytes is a cache hit whose answer cannot have changed. That is not an
optimisation detail -- it is what makes a repair's before/after honest. The
"before" number was computed from bytes that still exist under their own hash,
so nobody can quietly re-measure the past into a better shape.

Probe versions are handled by storing the `method` string each Measurement
already carries and discarding, on load, any measurement whose method no longer
matches the probe that would produce it today. A changed probe therefore
invalidates exactly its own results and nothing else, without a migration and
without a stale number surviving into a verdict.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import Measurement


@dataclass
class QCReport:
    """Everything measured about one specific version of one asset."""

    sha256: str
    asset_id: str
    title_id: str
    scene_id: str = ""
    market: str = ""
    measurements: list[Measurement] = field(default_factory=list)
    measured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "measurements"},
            "measurements": [asdict(m) for m in self.measurements],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QCReport":
        raw = dict(raw)
        raw["measurements"] = [
            Measurement(**m) for m in raw.get("measurements", [])
        ]
        return cls(**raw)

    def methods(self) -> set[str]:
        return {m.method for m in self.measurements}

    def keeping(self, current_methods: set[str]) -> "QCReport":
        """This report with results from superseded probes dropped."""
        kept = [m for m in self.measurements if m.method in current_methods]
        return QCReport(
            sha256=self.sha256, asset_id=self.asset_id, title_id=self.title_id,
            scene_id=self.scene_id, market=self.market, measurements=kept,
            measured_at=self.measured_at, detail=self.detail,
        )


class QCStore:
    """Reports on disk, one file per asset version."""

    def __init__(self, root: Path) -> None:
        self.root = root / "qc"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self.root / f"{sha256}.json"

    def put(self, report: QCReport) -> Path:
        path = self._path(report.sha256)
        path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def get(self, sha256: str) -> QCReport | None:
        path = self._path(sha256)
        if not path.exists():
            return None
        return QCReport.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def all(self) -> list[QCReport]:
        reports = []
        for path in sorted(self.root.glob("*.json")):
            try:
                reports.append(
                    QCReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (json.JSONDecodeError, TypeError, KeyError):
                # A corrupt report is worth skipping loudly rather than
                # crashing the exporter that every other market depends on.
                continue
        return reports

    def fresh(self, sha256: str, current_methods: set[str]) -> QCReport | None:
        """A report only if every current probe has already run on these bytes.

        Returning a partial report would let the caller skip the probes that
        are missing, which is how a measurement silently disappears -- and an
        absent measurement now blocks a market, so it would look like a
        mysterious regression rather than a cache miss.
        """
        report = self.get(sha256)
        if report is None:
            return None
        if not current_methods <= report.methods():
            return None
        return report.keeping(current_methods)
