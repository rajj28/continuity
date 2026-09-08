"""What Grafana is told about the world, and what it is deliberately not told.

The load-bearing assertion in this file is `test_an_unmeasured_asset_publishes
_no_measurement_at_all`. Publishing a zero for something we never measured
would read downstream as a passing measurement, and the verdict would turn a
market green on the strength of a probe that never ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from media.qc.report import QCReport, QCStore
from media.qc.types import Measurement
from media.store import Asset, ParentRef, Store
from telemetry.exporters.state import publish_once


class RecordingInstruments:
    """Stands in for the OTel instruments, remembering what was set.

    Deliberately not a mock of the OTel SDK: what matters is which series and
    labels were published, which is exactly what this records and nothing more.
    """

    def __init__(self) -> None:
        self.series: list[tuple[str, float, dict]] = []

    def gauge(self, name: str, description: str = ""):
        parent = self

        class _Gauge:
            def set(self, value, attributes=None):
                parent.series.append((name, float(value), attributes or {}))

        return _Gauge()

    def counter(self, name: str, description: str = ""):
        return self.gauge(name, description)

    def record_measurement(self, measurement, *, title, scene, market):
        from telemetry.metrics import MEASUREMENT_SERIES
        name = MEASUREMENT_SERIES[measurement.key]
        self.series.append((name, float(measurement.value),
                            {"title": title, "scene": scene, "market": market}))
        return name

    def set_stale(self, stale, *, title, scene, market):
        self.series.append(("asset_stale", 1.0 if stale else 0.0,
                            {"title": title, "scene": scene, "market": market}))

    def names(self) -> set[str]:
        return {name for name, _, _ in self.series}

    def value_of(self, name: str) -> float | None:
        for series, value, _ in self.series:
            if series == name:
                return value
        return None


SYNC = "delivery.dub_sync_offset_ms"
METHOD = "ffmpeg_silencedetect_isochrony_v1"


def measurement(value: float = 480.0, method: str = METHOD) -> Measurement:
    return Measurement(key=SYNC, value=value, unit="ms", method=method)


@pytest.fixture
def world(tmp_path: Path):
    """A store holding one master and one dubbed stem built from it."""
    store = Store(tmp_path)
    qc = QCStore(tmp_path)

    master = Asset(
        id="SINTEL:master", kind="MASTER", sha256="a" * 64,
        uri="gs://x/master.mp4", bytes=1, title_id="SINTEL",
    )
    store.record(master)
    stem = Asset(
        id="SINTEL:S01:dub_stem:de-DE", kind="DUB_STEM", sha256="b" * 64,
        uri="gs://x/de.wav", bytes=1, title_id="SINTEL", scene_id="S01",
        market="de-DE",
        parents=[ParentRef(asset_id="SINTEL:master", sha256="a" * 64)],
    )
    store.record(stem)
    return store, qc, master, stem


# ---------------------------------------------------------------------------
# The QC report store
# ---------------------------------------------------------------------------


def test_a_report_is_keyed_by_content_so_re_measuring_is_a_cache_hit(tmp_path):
    """And therefore the 'before' number of a repair cannot be re-measured into
    a better shape later: those bytes still exist under their own hash."""
    qc = QCStore(tmp_path)
    qc.put(QCReport(sha256="c" * 64, asset_id="a", title_id="SINTEL",
                    measurements=[measurement(480.0)]))
    again = qc.get("c" * 64)
    assert again is not None
    assert again.measurements[0].value == 480.0


def test_a_changed_probe_invalidates_exactly_its_own_results(tmp_path):
    qc = QCStore(tmp_path)
    qc.put(QCReport(
        sha256="d" * 64, asset_id="a", title_id="SINTEL",
        measurements=[measurement(480.0, method="old_probe_v1"),
                      measurement(17.0, method="loudness_v2")],
    ))
    kept = qc.get("d" * 64).keeping({"loudness_v2"})
    assert [m.method for m in kept.measurements] == ["loudness_v2"]


def test_a_partial_report_is_not_served_as_fresh(tmp_path):
    """Serving it would let the caller skip the probes that are missing, and a
    measurement that silently disappears now blocks a market -- which reads as
    a mysterious regression rather than a cache miss."""
    qc = QCStore(tmp_path)
    qc.put(QCReport(sha256="e" * 64, asset_id="a", title_id="SINTEL",
                    measurements=[measurement(480.0, method="probe_a")]))
    assert qc.fresh("e" * 64, {"probe_a"}) is not None
    assert qc.fresh("e" * 64, {"probe_a", "probe_b"}) is None


def test_a_corrupt_report_does_not_take_down_the_exporter(tmp_path):
    qc = QCStore(tmp_path)
    qc.put(QCReport(sha256="f" * 64, asset_id="a", title_id="SINTEL"))
    (tmp_path / "qc" / "broken.json").write_text("{not json", encoding="utf-8")
    assert [r.sha256 for r in qc.all()] == ["f" * 64]


# ---------------------------------------------------------------------------
# What gets published
# ---------------------------------------------------------------------------


def test_an_unmeasured_asset_publishes_no_measurement_at_all(world):
    """The one that matters. A zero would read as a passing measurement, and
    the market would go green on a probe that never ran. Absent reads as
    unmeasured, which is what it is, and the coverage gate blocks on it."""
    store, qc, _, _ = world
    inst = RecordingInstruments()
    cycle = publish_once(inst, store, qc)

    assert cycle.unmeasured == 1
    assert "dub_sync_offset_ms" not in inst.names()
    # staleness is still reported: we know that answer without a probe
    assert "asset_stale" in inst.names()


def test_a_measured_asset_publishes_its_numbers(world):
    store, qc, _, stem = world
    qc.put(QCReport(
        sha256=stem.sha256, asset_id=stem.id, title_id="SINTEL",
        scene_id="S01", market="de-DE", measurements=[measurement(480.0)],
    ))
    inst = RecordingInstruments()
    cycle = publish_once(inst, store, qc)

    assert cycle.measurements == 1
    assert cycle.unmeasured == 0
    assert inst.value_of("dub_sync_offset_ms") == 480.0


def test_staleness_is_computed_from_hashes_not_stored(world):
    """Change the master and the stem goes stale with nothing invalidated --
    no message sent, no flag flipped, no bookkeeping that can drift."""
    store, qc, master, _ = world
    inst = RecordingInstruments()
    assert publish_once(inst, store, qc).stale == 0
    assert inst.value_of("asset_stale") == 0.0

    master.sha256 = "z" * 64          # the master was re-graded
    store.record(master)

    inst = RecordingInstruments()
    assert publish_once(inst, store, qc).stale == 1
    assert inst.value_of("asset_stale") == 1.0


def test_assets_with_no_market_are_skipped(world):
    """A master has no market. Publishing one under a blank label would create
    a series the recording rules cannot join on."""
    store, qc, _, _ = world
    inst = RecordingInstruments()
    cycle = publish_once(inst, store, qc)
    assert cycle.assets == 1          # the stem only, not the master
    markets = {a.get("market") for n, _, a in inst.series if n == "asset_stale"}
    assert markets == {"de-DE"}


def test_thresholds_are_republished_every_cycle(world):
    """They are just as perishable as measurements: Prometheus ages a series
    out five minutes after its last sample, and a threshold that lapses takes
    the verdict's join with it."""
    store, qc, _, _ = world
    inst = RecordingInstruments()
    cycle = publish_once(inst, store, qc)
    assert cycle.thresholds > 0
    assert "market_threshold" in inst.names()
    assert "market_required_checks" in inst.names()


def test_a_repaired_asset_is_not_stale_against_its_own_predecessor(world):
    """The bug the first successful repair created. A repair records the
    version it replaced as a parent -- real lineage, but not an input -- and
    comparing an asset's hash against its own predecessor marks every repaired
    asset permanently stale the instant it succeeds. The fix becomes the reason
    the market stays blocked.

    Staleness asks whether something this was BUILT FROM has moved. An asset
    cannot have been built from itself.
    """
    store, qc, _master, stem = world
    repaired = Asset(
        id=stem.id, kind=stem.kind, sha256="c" * 64, uri="gs://x/de_v2.wav",
        bytes=1, title_id="SINTEL", scene_id="S01", market="de-DE", version=2,
        parents=[
            ParentRef(asset_id=stem.id, sha256=stem.sha256, role="pre_repair"),
            *stem.parents,
        ],
    )
    store.record(repaired)

    assert store.is_stale(repaired) == []
    inst = RecordingInstruments()
    assert publish_once(inst, store, qc).stale == 0
    assert inst.value_of("asset_stale") == 0.0


def test_a_genuinely_moved_parent_is_still_stale(world):
    """The negative control: skipping self-references must not skip real ones."""
    store, qc, master, stem = world
    master.sha256 = "z" * 64
    store.record(master)
    repaired = Asset(
        id=stem.id, kind=stem.kind, sha256="c" * 64, uri="gs://x/de_v2.wav",
        bytes=1, title_id="SINTEL", scene_id="S01", market="de-DE", version=2,
        parents=[
            ParentRef(asset_id=stem.id, sha256=stem.sha256, role="pre_repair"),
            *stem.parents,
        ],
    )
    store.record(repaired)
    assert store.is_stale(repaired) == ["SINTEL:master"]
