"""A content-addressed cache for model output.

The Gemini free tier allows ten TTS requests per project per day. A twelve-line
scene needs twelve. That arithmetic is what makes this file load-bearing rather
than an optimisation: without it a run that dies on line eleven has spent the
day's entire allowance and produced nothing, and the next run starts from zero.

With it, every successful call is permanent. A re-run replays the lines already
spoken for free and spends its quota only on what is genuinely new -- so a
REWRITE that changes three lines costs three calls, not twelve, and a scene can
be completed across two days if it has to be.

Keyed by everything that determines the answer -- model, voice, and the exact
text -- because that is what makes a hit correct rather than merely convenient.
A line that changed by one word is a different line and gets a different key.

Not a general-purpose cache. It never expires entries and it has no size bound,
because the corpus is a film's dialogue and the entries are worth more than the
disk they occupy.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class Cache:
    """Bytes on disk, keyed by a hash of whatever produced them."""

    def __init__(self, root: Path, *, suffix: str = ".bin") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.suffix = suffix
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(*parts: str) -> str:
        # "\x00" as the separator because it cannot occur in any of the parts,
        # so no combination of model, voice and text can collide with another
        # by moving the boundary between them.
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}{self.suffix}"

    def get_bytes(self, *parts: str) -> bytes | None:
        path = self._path(self.key(*parts))
        if path.exists():
            self.hits += 1
            return path.read_bytes()
        self.misses += 1
        return None

    def put_bytes(self, data: bytes, *parts: str) -> None:
        self._path(self.key(*parts)).write_bytes(data)

    def get_json(self, *parts: str) -> Any | None:
        raw = self.get_bytes(*parts)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A corrupt entry is a miss, not a crash. It cost one call to make
            # and costs one call to replace; taking down the run instead would
            # cost the rest of the day's quota.
            return None

    def put_json(self, obj: Any, *parts: str) -> None:
        self.put_bytes(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"), *parts
        )

    @property
    def summary(self) -> str:
        return f"{self.hits} hit(s), {self.misses} miss(es)"
