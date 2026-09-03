"""Hard end-to-end check of Stage 1 against the real Sintel master.

These are the properties everything downstream depends on. If any of them is
false, every sync measurement taken later is measuring against a reference that
moved, and the whole product is built on sand.

Skipped when the master is absent (it is 224 MB and gitignored); run
`python scripts/stage1.py --master ... --dialogue ...` first.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from media.dialogue import load_dialogue, segment_scenes
from media.qc.ffmpeg import duration_ms
from media.store import Store

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "assets" / "sintel" / "master_v1.mp4"
DIALOGUE = ROOT / "assets" / "sintel" / "sintel_en.srt"
STORE = ROOT / "out" / "store"
MANIFEST = ROOT / "out" / "scenes" / "manifest.json"

FRAME_MS = 1000.0 / 24.0  # Sintel is 24 fps

pytestmark = pytest.mark.skipif(
    not (MASTER.exists() and MANIFEST.exists()),
    reason="run scripts/stage1.py first (master is gitignored)",
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def store() -> Store:
    return Store(STORE)


# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------

def test_segmentation_is_stable_and_covers_every_cue(manifest):
    cues = load_dialogue(DIALOGUE)
    scenes = segment_scenes(cues, limit_ms=int(manifest["master"]["duration_ms"]))

    assert sum(len(s.utterances) for s in scenes) == len(cues) == 26
    assert [s.id for s in scenes] == ["S01", "S02", "S03", "S04", "S05"]
    # scenes must not overlap, or an asset could belong to two of them
    for a, b in zip(scenes, scenes[1:]):
        assert a.out_ms <= b.in_ms


def test_demo_scene_has_enough_dialogue_to_be_worth_dubbing(manifest):
    s01 = next(s for s in manifest["scenes"] if s["id"] == "S01")
    assert s01["utterance_count"] == 12
    assert s01["words"] == 68
    assert s01["speech_ms"] > 30_000


# --------------------------------------------------------------------------
# the cut -- if the in-point drifts, every later measurement is wrong
# --------------------------------------------------------------------------

def test_cut_duration_matches_request_within_one_frame(manifest):
    s01 = next(s for s in manifest["scenes"] if s["id"] == "S01")
    requested = s01["duration_ms"]
    for kind in ("SCENE_VIDEO", "SCENE_AUDIO"):
        actual = s01["assets"][kind]["duration_ms"]
        assert abs(actual - requested) < FRAME_MS, (
            f"{kind} drifted {actual - requested:.1f} ms from the requested cut"
        )


def test_input_seek_agrees_with_accurate_output_seek(tmp_path, manifest):
    """`-ss` before `-i` is fast but seeks on the input. Confirm it lands in the
    same place as the slow, unambiguously accurate output-seek form -- otherwise
    the reference boundaries and the audio disagree by an unknown offset."""
    s01 = next(s for s in manifest["scenes"] if s["id"] == "S01")
    slow = tmp_path / "slow.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(MASTER),
         "-ss", f"{s01['in_ms']/1000:.3f}", "-t", f"{s01['duration_ms']/1000:.3f}",
         "-vn", "-ac", "1", "-ar", "48000", str(slow)],
        check=True,
    )
    fast = s01["assets"]["SCENE_AUDIO"]["duration_ms"]
    assert abs(duration_ms(slow) - fast) < FRAME_MS


def test_reference_intervals_fall_inside_the_clip(manifest):
    for scene in manifest["scenes"]:
        intervals = scene["reference_intervals_ms"]
        assert len(intervals) == scene["utterance_count"]
        for start, end in intervals:
            assert 0 <= start < end <= scene["duration_ms"]
        # strictly ordered and non-overlapping
        for (_, prev_end), (next_start, _) in zip(intervals, intervals[1:]):
            assert prev_end <= next_start


# --------------------------------------------------------------------------
# content addressing
# --------------------------------------------------------------------------

def test_rerun_is_idempotent_and_creates_no_duplicate_objects(manifest):
    """Re-running Stage 1 on identical bytes must not fork the store."""
    before_objects = len(list((STORE / "objects").rglob("*.*")))
    before_index = len(list((STORE / "index").glob("*.json")))

    subprocess.run(
        ["python", "scripts/stage1.py", "--master", str(MASTER),
         "--dialogue", str(DIALOGUE), "--scene", "S01"],
        cwd=ROOT, check=True, capture_output=True,
    )

    assert len(list((STORE / "objects").rglob("*.*"))) == before_objects
    assert len(list((STORE / "index").glob("*.json"))) == before_index
    assert json.loads(MANIFEST.read_text(encoding="utf-8")) == manifest


def test_every_scene_asset_records_both_parent_hashes(store, manifest):
    master_sha = manifest["master"]["sha256"]
    dl_sha = manifest["dialogue_list"]["sha256"]

    for kind in ("SCENE_VIDEO", "SCENE_AUDIO"):
        asset = store.load(f"SINTEL:S01:{kind.lower()}")
        recorded = {p.role: p.sha256 for p in asset.parents}
        assert recorded["master"] == master_sha
        assert recorded["dialogue_list"] == dl_sha
        assert store.is_stale(asset) == []


def test_a_new_master_makes_descendants_stale(store, manifest, tmp_path):
    """The property the whole blast-radius feature rests on: change the parent's
    bytes, and every asset that consumed the old bytes reports itself stale --
    with no invalidation message sent to anyone."""
    master = store.load("SINTEL:master")
    original_sha = master.sha256
    child = store.load("SINTEL:S01:scene_audio")
    assert store.is_stale(child) == []

    try:
        master.sha256 = "0" * 64  # stand in for a re-conformed master v2
        store.record(master)
        assert store.is_stale(child) == ["SINTEL:master"]
    finally:
        master.sha256 = original_sha
        store.record(master)

    assert store.is_stale(child) == []


def test_unknown_provenance_counts_as_stale_never_as_current(store):
    """An asset whose parent we cannot resolve must never be treated as fresh."""
    asset = store.load("SINTEL:S01:scene_audio")
    asset.parents[0].asset_id = "SINTEL:does_not_exist"
    assert store.is_stale(asset) == ["SINTEL:does_not_exist"]
