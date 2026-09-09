"""An asset's uri has to resolve on the machine that reads it.

The store is written wherever the pipeline runs, and here that is Windows, so
every uri in it carries backslashes. The control room runs in a Linux
container, where `Path("out\\dub\\de-DE\\stem.wav")` is not three directories
and a file -- it is one filename with backslashes in it, and it exists
nowhere.

Nothing failed. The deployed control room listed no outputs at all, for every
market, while every byte was sitting in the image. That is the worst shape a
bug can have here: the screen said the agents had produced nothing, which is
the one claim this project cannot afford to make wrongly.
"""

from __future__ import annotations

from pathlib import Path

from ui.server import ROOT, State


def test_a_windows_uri_resolves_to_the_same_file_as_a_posix_one():
    windows = State._file(r"out\dub\de-DE\S03_stem_v6.wav")
    posix = State._file("out/dub/de-DE/S03_stem_v6.wav")
    assert windows == posix
    assert windows == ROOT / "out" / "dub" / "de-DE" / "S03_stem_v6.wav"


def test_a_relative_uri_is_read_against_the_project_and_not_the_cwd():
    assert State._file("out/store/index/a.json").is_absolute()
    assert State._file("out/store/index/a.json").is_relative_to(ROOT)


def test_an_absolute_uri_is_left_alone():
    absolute = Path(__file__).resolve()
    assert State._file(str(absolute)) == absolute
