"""Content-addressed asset store.

The object's name IS its SHA-256. That single decision is what makes staleness
detection a pure function later: an asset records the hash of each parent it
consumed, so "am I stale?" is `recorded_parent_hash != parent.current_hash`,
with no invalidation messages and no bookkeeping that can drift.

Local filesystem now; the same interface backs GCS in the cloud worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("continuity.store")

CHUNK = 1 << 20  # 1 MiB

AssetKind = Literal[
    "MASTER", "DIALOGUE_LIST", "SCENE_VIDEO", "SCENE_AUDIO", "TRANSCRIPT",
    "TRANSLATION", "ADAPTED_LINE", "DUB_STEM", "SUBTITLE", "CAPTION",
    "AUDIO_DESCRIPTION", "FORCED_NARRATIVE", "METADATA", "PACKAGE",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class ParentRef:
    """A parent, plus the hash it had AT THE TIME THIS ASSET WAS BUILT.

    Storing the hash here rather than only the id is the whole staleness
    mechanism. Do not "simplify" this to a bare id.
    """

    asset_id: str
    sha256: str
    role: str = "input"


@dataclass
class Asset:
    id: str
    kind: AssetKind
    sha256: str
    uri: str
    bytes: int
    title_id: str
    scene_id: str | None = None
    market: str | None = None
    version: int = 1
    duration_ms: float | None = None
    parents: list[ParentRef] = field(default_factory=list)
    produced_by: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    """Content-addressed store rooted at a directory.

    Writes are idempotent: putting identical bytes twice yields one object and
    the same hash, which is what makes the whole pipeline safely re-runnable.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.objects = root / "objects"
        self.index = root / "index"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.index.mkdir(parents=True, exist_ok=True)

    def _object_path(self, digest: str, suffix: str) -> Path:
        # Two-level fan-out keeps directory listings usable at scale.
        return self.objects / digest[:2] / digest[2:4] / f"{digest}{suffix}"

    def put_file(self, source: Path, *, suffix: str | None = None) -> tuple[str, Path]:
        digest = sha256_file(source)
        dest = self._object_path(digest, suffix or source.suffix)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
        return digest, dest

    def put_bytes(self, data: bytes, *, suffix: str) -> tuple[str, Path]:
        digest = sha256_bytes(data)
        dest = self._object_path(digest, suffix)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return digest, dest

    def _index_path(self, asset_id: str) -> Path:
        """Asset ids are colon-separated (`SINTEL:S01:dub_stem`) because that is
        how they key in Firestore and GCS. Windows forbids `:` in filenames, so
        the local index encodes it. The id itself is never rewritten -- only its
        on-disk filename -- and the mapping is reversible."""
        return self.index / f"{asset_id.replace(':', '~')}.json"

    def record(self, asset: Asset) -> Path:
        """Write the index entry, ASSIGNING the version rather than trusting it.

        A version is a property of the asset's history. A caller cannot know it,
        and this one learned that the hard way: re-running a build stage passes
        `version=1`, because as far as that stage is concerned it is producing
        the asset for the first time. It has no way to know that two repairs
        already took the same asset to v3.

        So a stage re-run silently replaced a repaired dub stem with an
        unrepaired one AND rewound the counter to 1. The board then reported a
        sync fault the system had already fixed, the lineage said the repair
        never happened, and the only evidence it ever had was an orphaned QC
        report nothing pointed at. Nothing errored. That is the worst shape a
        data bug can take.

        The store decides instead:

          - nothing recorded yet        -> version 1
          - identical bytes re-recorded -> keep the version, it is the same
                                           asset and re-recording is idempotent
          - different bytes             -> previous + 1, always forward

        Superseding content is logged, because replacing an asset someone may
        have repaired is worth a line in the record even when it is correct.
        """
        path = self._index_path(asset.id)
        version, previous = 1, None
        if path.exists():
            try:
                previous = self.load(asset.id)
            except (json.JSONDecodeError, TypeError, KeyError):
                previous = None
        if previous is not None:
            if previous.sha256 == asset.sha256:
                version = previous.version
            else:
                version = previous.version + 1
                log.info(
                    "superseding %s v%d (%s) with v%d (%s)",
                    asset.id, previous.version, previous.sha256[:12],
                    version, asset.sha256[:12],
                )
        if asset.version != version:
            asset = replace(asset, version=version)
        path.write_text(
            json.dumps(asset.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        return path

    def load(self, asset_id: str) -> Asset:
        raw = json.loads(self._index_path(asset_id).read_text(encoding="utf-8"))
        raw["parents"] = [ParentRef(**p) for p in raw.get("parents", [])]
        return Asset(**raw)

    def all_assets(self) -> list[Asset]:
        return [
            self.load(p.stem.replace("~", ":"))
            for p in sorted(self.index.glob("*.json"))
        ]

    def is_stale(self, asset: Asset) -> list[str]:
        """Parent ids whose current hash differs from the one recorded here.

        An empty list means current. This is the entire staleness rule; the
        Prometheus `asset_stale` gauge is just this function, scraped.
        """
        stale: list[str] = []
        for parent in asset.parents:
            if parent.asset_id == asset.id:
                # A repair records the version it replaced as a parent, which
                # is real lineage but not an input. Comparing an asset's hash
                # against its own predecessor's would mark every repaired
                # asset permanently stale the instant it succeeded -- the fix
                # itself becoming the reason the market stays blocked.
                #
                # Staleness asks whether something this was BUILT FROM has
                # moved. An asset cannot have been built from itself.
                continue
            try:
                current = self.load(parent.asset_id)
            except FileNotFoundError:
                stale.append(parent.asset_id)  # unknown provenance: treat as stale
                continue
            if current.sha256 != parent.sha256:
                stale.append(parent.asset_id)
        return stale
