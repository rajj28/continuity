"""Metadata and forced narratives -- the deliverables nobody films.

The tests worth reading here are the two that catch a *present* deliverable
being wrong: a metadata record that exists and was never translated, and a
market blocked for a missing forced-narrative track before anyone checked
whether the picture needs one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from media.metadata import (
    FIELD_LIMITS,
    ForcedNarrativeNeed,
    LocalisedMetadata,
    MetadataStore,
    check_forced_narrative,
    check_metadata,
)

CUT = "a" * 64
SOURCE_TITLE = "Sintel"


def record(**kw) -> LocalisedMetadata:
    base = dict(
        market="de-DE", language="de", title="Sintel — Die Drachenjägerin",
        short_synopsis="Eine einsame Kriegerin sucht den Drachen, "
                       "den sie einst grosszog.",
        synopsis="Sintel durchquert eine feindliche Welt auf der Suche nach "
                 "dem Drachenjungen, das sie aufgezogen hat.",
        genre=["Animation", "Fantasy"],
    )
    base.update(kw)
    return LocalisedMetadata(**base)


# ---------------------------------------------------------------------------
# Storefront metadata
# ---------------------------------------------------------------------------


def test_a_complete_translated_record_passes():
    result = check_metadata("de-DE", record(), source_title=SOURCE_TITLE)
    assert result.complete
    assert result.detail["lengths"]["title"] > 0


def test_a_missing_record_blocks():
    result = check_metadata("de-DE", None, source_title=SOURCE_TITLE)
    assert not result.complete
    assert "no localised metadata" in result.reason


def test_an_empty_field_blocks_and_names_itself():
    result = check_metadata("de-DE", record(synopsis="   "),
                            source_title=SOURCE_TITLE)
    assert not result.complete
    assert result.detail["missing"] == ["synopsis"]


def test_over_a_storefront_limit_blocks():
    """Hard limits: a synopsis two characters over is rejected at ingest, not
    truncated politely."""
    result = check_metadata(
        "de-DE", record(short_synopsis="x" * (FIELD_LIMITS["short_synopsis"] + 1)),
        source_title=SOURCE_TITLE,
    )
    assert not result.complete
    assert "over the storefront limit" in result.reason
    assert "short_synopsis" in result.detail["over"]


SOURCE_SYNOPSIS = ("A lone warrior searches a hostile world for the dragon "
                   "she raised from a hatchling.")


def test_a_record_that_was_never_translated_is_caught():
    """The failure that looks like success: the right shape, every field
    filled, and the copy still in English."""
    result = check_metadata(
        "de-DE", record(short_synopsis=SOURCE_SYNOPSIS),
        source_title=SOURCE_TITLE, source_synopsis=SOURCE_SYNOPSIS,
    )
    assert not result.complete
    assert "never translated" in result.reason


def test_a_proper_noun_title_may_stay_the_same():
    """Sintel is Sintel in Germany and in France, and real distributors keep
    it. Judging translation on the title would fail every such record; prose
    is the honest signal, because a synopsis does not survive translation
    byte-identical."""
    result = check_metadata(
        "de-DE", record(title="Sintel"),
        source_title=SOURCE_TITLE, source_synopsis=SOURCE_SYNOPSIS,
    )
    assert result.complete


def test_an_english_market_may_legitimately_keep_the_source_copy():
    result = check_metadata(
        "en-GB",
        record(market="en-GB", language="en", title="Sintel",
               short_synopsis=SOURCE_SYNOPSIS),
        source_title=SOURCE_TITLE, source_synopsis=SOURCE_SYNOPSIS,
    )
    assert result.complete


def test_metadata_is_repairable_unlike_a_right():
    """It is text, and the pipeline already knows how to translate text against
    a length budget."""
    assert check_metadata("de-DE", None, source_title=SOURCE_TITLE).repairable


def test_records_round_trip(tmp_path):
    store = MetadataStore(tmp_path)
    store.put(record(), title="SINTEL")
    back = store.get("SINTEL", "de-DE")
    assert back is not None
    assert back.title == record().title
    assert back.genre == ["Animation", "Fantasy"]


def test_a_corrupt_record_reads_as_absent_not_as_valid(tmp_path):
    store = MetadataStore(tmp_path)
    (store.root / "SINTEL~de-DE.json").write_text("{not json", encoding="utf-8")
    assert store.get("SINTEL", "de-DE") is None


# ---------------------------------------------------------------------------
# Forced narratives
# ---------------------------------------------------------------------------


def test_an_unanalysed_picture_does_not_block_for_a_missing_track():
    """Blocking every market for work nobody has done yet would be dishonest.
    It reports the gap as unknown, which the coverage gate handles properly."""
    result = check_forced_narrative("de-DE", None, has_track=False,
                                    master_sha256=CUT)
    assert not result.complete
    assert "has not been analysed" in result.reason
    assert result.detail["analysed"] is False


def test_an_analysis_of_a_different_cut_does_not_apply():
    """Re-cutting changes what is on screen, so a finding about an earlier
    master says nothing about this one."""
    need = ForcedNarrativeNeed(master_sha256="b" * 64, needed=True, cues=3)
    result = check_forced_narrative("de-DE", need, has_track=True,
                                    master_sha256=CUT)
    assert not result.complete
    assert result.detail["analysed"] is False


def test_a_picture_with_no_on_screen_text_needs_no_track():
    need = ForcedNarrativeNeed(master_sha256=CUT, needed=False)
    result = check_forced_narrative("de-DE", need, has_track=False,
                                    master_sha256=CUT)
    assert result.complete
    assert "no on-screen text" in result.reason


def test_on_screen_text_with_no_track_blocks_and_says_why():
    need = ForcedNarrativeNeed(master_sha256=CUT, needed=True, cues=4)
    result = check_forced_narrative("de-DE", need, has_track=False,
                                    master_sha256=CUT)
    assert not result.complete
    assert "4 on-screen cue(s)" in result.reason
    assert "subtitles switched off" in result.reason


def test_on_screen_text_with_a_track_passes():
    need = ForcedNarrativeNeed(master_sha256=CUT, needed=True, cues=4)
    result = check_forced_narrative("de-DE", need, has_track=True,
                                    master_sha256=CUT)
    assert result.complete
    assert "4 cue(s)" in result.reason
