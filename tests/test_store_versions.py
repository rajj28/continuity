"""Versions move forward, and a rebuild can never rewind one.

This file exists because of a bug that produced no error and no warning, and
was only found by reading four QC reports in timestamp order:

    10:39  original dub          sync 298.2 ms   loudness -17.83
    10:40  RETIME repair         sync  13.3 ms   loudness -17.84
    11:05  REMIX repair          sync  13.0 ms   loudness -22.50   <- repaired
    12:23  stage 2 re-run        sync 273.0 ms   loudness -20.07   <- v1 again

Both repairs did exactly what they predicted. Then stage 2 was re-run while
building out another dimension, and because a build stage naturally believes it
is creating an asset for the FIRST time, it passed `version=1`. `record` wrote
it, and the index -- the only thing that says which bytes are current --
pointed back at an unrepaired stem at version 1.

Nothing failed. The board simply went back to reporting a fault that had
already been fixed, the lineage said the repair never happened, and the
evidence that it had was an orphaned QC report nothing referenced.

So the version is the store's to assign, never the caller's to assert.
"""

from __future__ import annotations

import json

from media.store import Asset, ParentRef, Store


def _asset(sha: str, version: int = 1, parents=None) -> Asset:
    return Asset(
        id="SINTEL:S03:dub_stem:de-DE", kind="DUB_STEM", sha256=sha,
        uri=f"/tmp/{sha[:8]}.wav", bytes=1024, title_id="SINTEL",
        scene_id="S03", market="de-DE", version=version,
        parents=parents or [],
    )


def test_first_record_is_version_one(tmp_path):
    store = Store(tmp_path)
    store.record(_asset("a" * 64, version=7))     # caller is simply wrong
    assert store.load("SINTEL:S03:dub_stem:de-DE").version == 1


def test_recording_identical_bytes_is_idempotent(tmp_path):
    """Re-running a stage that produces the same output is not a new version.

    The whole pipeline is meant to be safely re-runnable, and a version counter
    that climbed every time someone re-ran a deterministic step would make
    "which version is this" meaningless.
    """
    store = Store(tmp_path)
    store.record(_asset("a" * 64))
    store.record(_asset("a" * 64))
    store.record(_asset("a" * 64))
    assert store.load("SINTEL:S03:dub_stem:de-DE").version == 1


def test_a_repair_advances_the_version(tmp_path):
    store = Store(tmp_path)
    store.record(_asset("a" * 64))
    store.record(_asset("b" * 64, parents=[
        ParentRef("SINTEL:S03:dub_stem:de-DE", "a" * 64, "pre_repair")]))
    current = store.load("SINTEL:S03:dub_stem:de-DE")
    assert current.version == 2
    assert current.sha256 == "b" * 64


def test_a_rebuild_claiming_version_one_cannot_rewind_the_counter(tmp_path):
    """The exact regression. A stage re-run supersedes, it does not revert.

    The new bytes DO become current -- a rebuild is a legitimate new version of
    the asset, and pretending otherwise would strand the pipeline. What must not
    happen is the version going backwards, because that is the signal anyone
    reading the index has that a repair ever occurred.
    """
    store = Store(tmp_path)
    store.record(_asset("a" * 64))                       # built
    store.record(_asset("b" * 64))                       # RETIME  -> v2
    store.record(_asset("c" * 64))                       # REMIX   -> v3
    assert store.load("SINTEL:S03:dub_stem:de-DE").version == 3

    store.record(_asset("d" * 64, version=1))            # stage re-run
    current = store.load("SINTEL:S03:dub_stem:de-DE")
    assert current.version == 4, "a rebuild must not rewind the version"
    assert current.sha256 == "d" * 64


def test_the_version_on_disk_is_the_assigned_one(tmp_path):
    """Not merely what `load` returns -- what a reader of the JSON sees.

    The state exporter and the control room both read these files, so a version
    corrected only in memory would still publish the wrong number.
    """
    store = Store(tmp_path)
    store.record(_asset("a" * 64))
    path = store.record(_asset("b" * 64, version=1))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
