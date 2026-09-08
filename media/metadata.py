"""Localised metadata and forced narratives: the two dimensions nobody films.

Both are deliverables a release genuinely blocks on, and both are invisible to
anyone thinking about a title as "the video and the dub".

## Metadata

The title, synopsis and genre a viewer actually sees in the storefront. Every
platform publishes character limits for them, and they are hard limits: a
synopsis two characters over is not truncated politely, it is rejected at
ingest. They are also the most commonly forgotten localisation -- the dub gets
made and the storefront still says the English title.

Checkable without judgement: does a localised record exist for this market, is
each field inside its limit, and is the title actually different from the
source where the market's language differs. That last one catches the failure
mode that matters -- a record that exists but was never translated.

## Forced narratives

Subtitles for on-screen text and for foreign dialogue, which must appear even
when the viewer has subtitles switched OFF. A signpost, a letter, a line spoken
in another language. Miss them and a scene stops making sense; add them where
the dub already covers the line and you have written on the picture for no
reason.

The deliverable is required only where the source actually has such text, which
is a question about the picture rather than about the market -- so a market can
be blocked for a missing forced-narrative track only when the title has been
analysed and found to need one. `needs_forced_narrative` records that finding
against the master's hash, because re-cutting changes what is on screen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Storefront limits. Real platform specifications differ, but they all have
# them, and every one of them rejects rather than truncates.
FIELD_LIMITS = {
    "title": 120,
    "short_synopsis": 190,
    "synopsis": 1000,
}

# Markets whose language differs from the source. A localised record whose
# title is byte-identical to the English one in these markets was created and
# never translated -- which is the failure that looks like success.
SOURCE_LANGUAGE = "en"


class MetadataError(RuntimeError):
    pass


@dataclass
class LocalisedMetadata:
    market: str
    language: str
    title: str = ""
    short_synopsis: str = ""
    synopsis: str = ""
    genre: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market, "language": self.language,
            "title": self.title, "short_synopsis": self.short_synopsis,
            "synopsis": self.synopsis, "genre": self.genre,
        }


@dataclass
class MetadataResult:
    market: str
    complete: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def repairable(self) -> bool:
        """Buildable, unlike a right or a certificate: it is text, and the
        pipeline already knows how to translate text against a length budget."""
        return True


def check_metadata(
    market: str, record: LocalisedMetadata | None, *, source_title: str,
    source_synopsis: str = "",
) -> MetadataResult:
    """Is this market's storefront record present, in-limit and translated?"""
    if record is None:
        return MetadataResult(
            market=market, complete=False,
            reason="no localised metadata record",
        )

    missing = [
        name for name in ("title", "short_synopsis", "synopsis")
        if not getattr(record, name, "").strip()
    ]
    if missing:
        return MetadataResult(
            market=market, complete=False,
            reason=f"empty {', '.join(missing)}",
            detail={"missing": missing},
        )

    over = {
        name: (len(getattr(record, name)), limit)
        for name, limit in FIELD_LIMITS.items()
        if len(getattr(record, name)) > limit
    }
    if over:
        detail = ", ".join(f"{n} {got} > {limit}" for n, (got, limit) in over.items())
        return MetadataResult(
            market=market, complete=False,
            reason=f"over the storefront limit: {detail}",
            detail={"over": {n: got for n, (got, _l) in over.items()}},
        )

    # The failure that looks like success: a record that exists in the right
    # shape and was never actually translated.
    #
    # Judged on the SYNOPSIS, not the title. A proper-noun title legitimately
    # stays put -- Sintel is Sintel in Germany and in France, and real
    # distributors keep it -- so flagging an unchanged title would fail every
    # such record. An unchanged synopsis has no such excuse: prose does not
    # survive translation byte-identical.
    if record.language != SOURCE_LANGUAGE and source_synopsis and \
            record.short_synopsis.strip().casefold() == \
            source_synopsis.strip().casefold():
        return MetadataResult(
            market=market, complete=False,
            reason=(f"the synopsis is still the source text in a "
                    f"{record.language} record; it was never translated"),
            detail={"short_synopsis": record.short_synopsis[:80]},
        )

    return MetadataResult(
        market=market, complete=True,
        reason=f"{len(record.synopsis)} char synopsis, within limits",
        detail={"lengths": {n: len(getattr(record, n)) for n in FIELD_LIMITS}},
    )


class MetadataStore:
    """Localised storefront records, one file per market."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "metadata"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, title: str, market: str) -> Path:
        return self.root / f"{title}~{market}.json"

    def put(self, record: LocalisedMetadata, *, title: str) -> Path:
        path = self._path(title, record.market)
        path.write_text(json.dumps(record.to_dict(), indent=2,
                                   ensure_ascii=False, sort_keys=True),
                        encoding="utf-8")
        return path

    def get(self, title: str, market: str) -> LocalisedMetadata | None:
        path = self._path(title, market)
        if not path.exists():
            return None
        try:
            return LocalisedMetadata(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Forced narratives
# ---------------------------------------------------------------------------


@dataclass
class ForcedNarrativeNeed:
    """Whether the picture contains text or foreign dialogue needing subtitles.

    Recorded against the master's hash: re-cutting changes what is on screen,
    so a finding about an earlier cut says nothing about this one.
    """

    master_sha256: str
    needed: bool
    cues: int = 0
    note: str = ""

    def applies_to(self, master_sha256: str) -> bool:
        return bool(self.master_sha256) and self.master_sha256 == master_sha256


def check_forced_narrative(
    market: str, need: ForcedNarrativeNeed | None, has_track: bool,
    *, master_sha256: str,
) -> MetadataResult:
    """Judge a market's forced-narrative deliverable.

    A market cannot be blocked for a missing track until the picture has been
    analysed and found to need one. Blocking on an unanalysed title would
    penalise every market for work nobody has done yet, which the coverage
    gate already handles more honestly.
    """
    if need is None or not need.applies_to(master_sha256):
        return MetadataResult(
            market=market, complete=False,
            reason=("the picture has not been analysed for on-screen text "
                    "against this cut, so whether a forced-narrative track is "
                    "required is unknown"),
            detail={"analysed": False},
        )
    if not need.needed:
        return MetadataResult(
            market=market, complete=True,
            reason="no on-screen text or foreign dialogue in this cut",
            detail={"analysed": True, "cues": 0},
        )
    if has_track:
        return MetadataResult(
            market=market, complete=True,
            reason=f"forced-narrative track present for {need.cues} cue(s)",
            detail={"analysed": True, "cues": need.cues},
        )
    return MetadataResult(
        market=market, complete=False,
        reason=(f"{need.cues} on-screen cue(s) need forced narratives and no "
                f"track exists; without it those moments are unreadable with "
                f"subtitles switched off"),
        detail={"analysed": True, "cues": need.cues},
    )
