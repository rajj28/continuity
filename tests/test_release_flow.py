"""What the control room is allowed to start, and with what.

The build endpoint takes a file path and a market name from a browser and
turns them into a command line. Two things therefore have to be true and
neither is obvious from reading the happy path:

  * the path must be one the server offered, compared as a path and not
    pattern-matched for "..", because what is on the other end of it is
    ffmpeg reading a file;
  * each stage must be called in its own vocabulary. `release_check.py` takes
    no market -- a frame rate belongs to a master, not to Germany -- and
    passing one made the whole build halt on an argparse error four stages
    in, which on the screen looked like the pipeline was broken.

Both were real failures, in that order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ui import release as rel


# -- the command lines -------------------------------------------------------


def _line(key: str, market: str = "pt-BR") -> list[str]:
    stage = next(s for s in rel.PLAN if s.key == key)
    return rel._argv(stage, market, Path("M.mp4"), Path("D.srt"), "S03")[2:]


def test_ingest_gets_the_master_the_dialogue_and_one_scene():
    argv = _line("ingest", market="")
    assert argv[0].endswith("stage1.py")
    assert argv[1:] == ["--master", "M.mp4", "--dialogue", "D.srt",
                        "--scene", "S03"]


def test_release_check_is_not_given_a_market():
    argv = _line("check", market="")
    assert argv[0].endswith("release_check.py")
    assert "--market" not in argv
    assert "--scene" not in argv
    assert argv[1] == "--master"
    assert argv[2].endswith("S03.mp4")


@pytest.mark.parametrize("key", ["dub", "subtitles", "description",
                                 "storefront", "package"])
def test_every_per_market_stage_gets_scene_and_market(key):
    argv = _line(key)
    assert argv[1:] == ["--scene", "S03", "--market", "pt-BR"]


def test_the_plan_covers_every_market_for_the_stages_that_are_per_market():
    steps = rel.Release().steps(["de-DE", "fr-FR"])
    per_market = sum(1 for s in rel.PLAN if s.per_market)
    once = len(rel.PLAN) - per_market
    assert len(steps) == per_market * 2 + once
    assert len({s["id"] for s in steps}) == len(steps)


# -- what may be built from ---------------------------------------------------


def test_only_files_the_server_offered_can_be_built_from(monkeypatch, tmp_path):
    offered = tmp_path / "clip.mp4"
    offered.write_bytes(b"0")
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    monkeypatch.setattr(rel, "STOCK_MASTER", offered)
    monkeypatch.setattr(rel, "STOCK_DIALOGUE", tmp_path / "absent.srt")
    monkeypatch.setattr(rel, "UPLOADS", tmp_path / "uploads")

    assert rel.Release().resolve("clip.mp4", subtitle=False) == offered
    for attempt in ("../../etc/passwd", "/etc/passwd", "", "clip.mp4.bak"):
        with pytest.raises(ValueError):
            rel.Release().resolve(attempt, subtitle=False)


def test_a_master_cannot_be_passed_where_a_dialogue_list_is_wanted(
        monkeypatch, tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"0")
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    monkeypatch.setattr(rel, "STOCK_MASTER", tmp_path / "clip.mp4")
    monkeypatch.setattr(rel, "STOCK_DIALOGUE", tmp_path / "absent.srt")
    monkeypatch.setattr(rel, "UPLOADS", tmp_path / "uploads")
    with pytest.raises(ValueError):
        rel.Release().resolve("clip.mp4", subtitle=True)


# -- uploads -----------------------------------------------------------------


def test_an_upload_never_decides_where_it_lands(monkeypatch, tmp_path):
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    monkeypatch.setattr(rel, "UPLOADS", tmp_path / "uploads")
    saved = rel.save_upload("../../../etc/cron.mp4", [b"abc"])
    written = tmp_path / saved["path"]
    assert written.parent == tmp_path / "uploads"
    assert written.read_bytes() == b"abc"
    assert saved["bytes"] == 3


def test_a_name_that_could_mean_something_to_a_shell_is_stripped(
        monkeypatch, tmp_path):
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    monkeypatch.setattr(rel, "UPLOADS", tmp_path / "uploads")
    saved = rel.save_upload("a b;rm -rf $HOME.srt", [b"1"])
    assert saved["name"] == "a_b_rm_-rf__HOME.srt"
    assert saved["subtitle"] is True


def test_a_file_that_is_not_media_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    monkeypatch.setattr(rel, "UPLOADS", tmp_path / "uploads")
    with pytest.raises(ValueError):
        rel.save_upload("payload.sh", [b"#!/bin/sh"])


def test_a_half_written_upload_is_not_offered(monkeypatch, tmp_path):
    """The staging file must not be picked up as something buildable."""
    monkeypatch.setattr(rel, "ROOT", tmp_path)
    monkeypatch.setattr(rel, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(rel, "STOCK_MASTER", tmp_path / "absent.mp4")
    monkeypatch.setattr(rel, "STOCK_DIALOGUE", tmp_path / "absent.srt")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "cut.mp4.part").write_bytes(b"half")
    assert rel.Release().sources()["masters"] == []


# -- what the operator reads --------------------------------------------------


def test_the_sdk_chatter_is_dropped_and_nothing_else_is():
    noise = [
        "2026-09-09 18:09:30 INFO    HTTP Request: POST https://us-central1-...",
        "2026-09-09 18:09:30 INFO    AFC is enabled with max remote calls: 10.",
    ]
    kept = [
        "    delivery.audio_loudness_lufs              -17.16 LUFS",
        "  gap 0  dropped after 2 attempt(s): 1011 ms will not fit a 1000 ms gap",
        "Traceback (most recent call last):",
    ]
    assert all(rel.NOISE.search(line) for line in noise)
    assert not any(rel.NOISE.search(line) for line in kept)


def test_lines_carrying_a_measurement_are_marked_notable():
    assert rel.NOTABLE.search("delivery.audio_true_peak_dbtp   -2.78 dBTP")
    assert rel.NOTABLE.search("dialogue 4ed7e1f1.. 26 cues -> 5 scenes")
    assert not rel.NOTABLE.search("2026-09-09 18:09:30 INFO    starting")
