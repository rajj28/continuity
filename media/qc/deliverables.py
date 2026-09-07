"""Package completeness -- is everything this market needs actually built?

The failure mode nobody instruments. Every other check in this system asks
whether an asset is *good*; this one asks whether it *exists*. A market can be
blocked because its SDH captions were never authored or its audio description
was never recorded, and no amount of measuring the dub will ever reveal that,
because the missing thing produces no metric to look at.

That asymmetry is exactly why the coverage gate exists in the verdict, and this
is the same idea applied one level up: absence has to be as visible as failure.

The mapping from a market's declared `requires` to the asset kinds that satisfy
it lives here rather than in the profile, because "what counts as an audio
description" is a fact about the pipeline, while "does this territory demand
one" is a fact about the market. Keeping them apart means a new territory is a
data change and a new deliverable type is a code change, which is the right way
round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Declared requirement -> the asset kinds that satisfy it. A requirement with
# several acceptable kinds is satisfied by any of them.
SATISFIED_BY: dict[str, tuple[str, ...]] = {
    "SDH_CAPTIONS": ("SUBTITLE", "CAPTION"),
    "AUDIO_DESCRIPTION": ("AUDIO_DESCRIPTION",),
    "DUB": ("DUB_STEM",),
    "FORCED_NARRATIVE": ("FORCED_NARRATIVE",),
}

# Deliverables every market needs whether or not it says so. A localised
# release without a dub or a subtitle is not a release, and leaving these
# implicit in each profile would mean a typo could silently excuse one.
UNIVERSAL = ("DUB", "SDH_CAPTIONS")


class UnknownRequirement(KeyError):
    """A market asked for a deliverable the pipeline has no concept of.

    Loud rather than ignored: silently skipping it would let a profile demand
    audio description, forced narratives or a dubbing script and have the
    market pass having produced none of them.
    """


@dataclass
class Completeness:
    market: str
    required: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def repairable(self) -> bool:
        """A missing deliverable is buildable, unlike a missing right.

        Which is the interesting distinction: the correct response is to go and
        make it, not to escalate. Whether the pipeline currently CAN make it is
        a separate question the Conductor answers from the strategies it has.
        """
        return True

    def reason(self) -> str:
        if self.complete:
            return f"all {len(self.required)} deliverables present"
        return "missing " + ", ".join(self.missing)


def requirements_for(profile: dict[str, Any]) -> list[str]:
    """Everything this market needs, declared plus universal."""
    declared = list(profile.get("requires", []))
    for requirement in UNIVERSAL:
        if requirement not in declared:
            declared.append(requirement)
    for requirement in declared:
        if requirement not in SATISFIED_BY:
            raise UnknownRequirement(
                f"{requirement!r} is required by a market but nothing in the "
                f"pipeline produces it -- add it to SATISFIED_BY, or the market "
                f"passes having built none of it"
            )
    return declared


def completeness(
    market: str, profile: dict[str, Any], assets: Iterable[Any]
) -> Completeness:
    """Which required deliverables exist for this market.

    `assets` are the store's assets. Only those belonging to this market count:
    a German dub does not satisfy Japan's requirement for one, which sounds
    obvious and is exactly the sort of thing a join gets wrong.
    """
    required = requirements_for(profile)
    kinds = {
        getattr(a, "kind", "") for a in assets
        if getattr(a, "market", None) == market
    }

    present, missing = [], []
    for requirement in required:
        if kinds & set(SATISFIED_BY[requirement]):
            present.append(requirement)
        else:
            missing.append(requirement)

    return Completeness(
        market=market,
        required=tuple(required),
        present=tuple(present),
        missing=tuple(missing),
        detail={"asset_kinds_present": sorted(kinds)},
    )
