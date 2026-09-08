"""The tools that make an agent good at pulling information, not just able to.

The direction bug is the argument for all three. The agent had a magnitude, no
way to ask which way the dub was out, and no way to see that its last repair
had made things worse -- so it guessed, three times, and every guess was caught
only after the audio had been changed. Better questions would have prevented it.

  metric_history   since when, and was it ever good
  blast_radius     what my fix would break
  compare_markets  is this peculiar to this market or common to all of them
"""

from __future__ import annotations

import json

import pytest

from agents.conductor import Toolbox
from agents.signal import Signal
from tests.test_investigate import sample

RANGE = "dub_sync_offset_ms"


class RangeStub:
    """Answers instant and range queries from separate tables."""

    def __init__(self, instant=None, ranged=None) -> None:
        self.instant = instant or {}
        self.ranged = ranged or {}
        self.asked: list[tuple[str, str]] = []

    def call(self, name: str, arguments: dict):
        assert name == "query_prometheus", name
        kind = arguments.get("queryType", "instant")
        expr = arguments["expr"]
        self.asked.append((kind, expr))
        table = self.ranged if kind == "range" else self.instant
        return {"data": table.get(expr, [])}


def _series(labels: dict, values: list[float]) -> dict:
    return {"metric": labels,
            "values": [[1788790000.0 + i * 120, str(v)]
                       for i, v in enumerate(values)]}


def _toolbox(client) -> Toolbox:
    return Toolbox(Signal(client), title="SINTEL", market="de-DE")


# ---------------------------------------------------------------------------
# metric_history
# ---------------------------------------------------------------------------


def test_history_summarises_the_window_not_just_the_last_value():
    """A market red for four minutes is an incident; red all day is a plan."""
    box = _toolbox(RangeStub(ranged={
        RANGE: [_series({"market": "de-DE"}, [273.0, 375.2, 17.4])],
    }))
    result = box.dispatch("metric_history", {"expr": RANGE, "hours": 6})
    assert result["first"] == 273.0
    assert result["last"] == 17.4
    assert result["max"] == 375.2      # the over-correction, still visible
    assert result["changed"] is True


def test_history_is_remembered_as_citable_evidence():
    """A proposal reasoning from a trend must be able to cite the trend."""
    box = _toolbox(RangeStub(ranged={
        RANGE: [_series({"market": "de-DE"}, [10.0, 20.0])],
    }))
    box.dispatch("metric_history", {"expr": RANGE, "hours": 6})
    assert RANGE in box.gathered


def test_several_matching_series_are_reported_rather_than_picked():
    """The bug this check exists for, found live.

    An instant read said 17.4 ms while the history reported 273 flat, because
    two exporter processes had both published across the window and the first
    series happened to be the stale one. Summarising an arbitrary member of an
    ambiguous match is how an agent reasons confidently from a number that is
    not the number it asked for.
    """
    box = _toolbox(RangeStub(ranged={
        RANGE: [
            _series({"market": "de-DE", "instance": "a"}, [273.0, 273.0]),
            _series({"market": "de-DE", "instance": "b"}, [17.4, 17.4]),
        ],
    }))
    result = box.dispatch("metric_history", {"expr": RANGE, "hours": 6})
    assert result["ambiguous"] is True
    assert {m["last"] for m in result["matched"]} == {273.0, 17.4}
    assert "first" not in result, "must not summarise one of them"
    assert "max by" in result["note"], "say how to resolve it"


def test_an_empty_window_says_so_rather_than_returning_zero():
    box = _toolbox(RangeStub(ranged={}))
    result = box.dispatch("metric_history", {"expr": RANGE, "hours": 6})
    assert result["points"] == []
    assert "no samples" in result["note"]


@pytest.mark.parametrize("asked,clamped", [(0, 1), (99, 24), (6, 6)])
def test_the_window_is_clamped(asked, clamped):
    """An unbounded window is an unbounded query against a shared datasource."""
    client = RangeStub(ranged={RANGE: [_series({}, [1.0])]})
    _toolbox(client).dispatch("metric_history", {"expr": RANGE, "hours": asked})
    assert client.asked[0][0] == "range"


# ---------------------------------------------------------------------------
# compare_markets
# ---------------------------------------------------------------------------


def test_comparing_markets_shows_whether_a_fault_is_local_or_common():
    """A fault every market shares is usually the source, not the dub."""
    box = _toolbox(RangeStub(instant={
        f'{RANGE}{{title="SINTEL"}}': [
            sample(17.4, market="de-DE"), sample(13.7, market="fr-FR"),
        ],
    }))
    result = box.dispatch("compare_markets", {"series": RANGE})
    assert result["this_market"] == "de-DE"
    assert result["markets"] == [
        {"market": "de-DE", "value": 17.4},
        {"market": "fr-FR", "value": 13.7},
    ]


def test_comparing_a_series_nobody_publishes_says_so():
    box = _toolbox(RangeStub(instant={}))
    result = box.dispatch("compare_markets", {"series": "invented_metric"})
    assert result["markets"] == []
    assert "no market publishes this" in result["note"]


# ---------------------------------------------------------------------------
# blast_radius
# ---------------------------------------------------------------------------


def test_blast_radius_reports_what_a_repair_would_invalidate(tmp_path,
                                                             monkeypatch):
    """Read from the asset index, never from traces.

    Traces are sampled and expire, so an empty search there means "not observed
    lately" and would read here as "nothing depends on this" -- the most
    dangerous possible wrong answer to "what will my fix break".
    """
    from media.store import Asset, ParentRef, Store
    import agents.conductor as conductor

    store = Store(tmp_path)
    line = Asset(id="T:S:dub_line:de-DE:00", kind="ADAPTED_LINE", sha256="a" * 64,
                 uri="x", bytes=1, title_id="T", scene_id="S", market="de-DE")
    stem = Asset(id="T:S:dub_stem:de-DE", kind="DUB_STEM", sha256="b" * 64,
                 uri="x", bytes=1, title_id="T", scene_id="S", market="de-DE",
                 parents=[ParentRef(line.id, line.sha256, "adapted_line")])
    package = Asset(id="T:S:package:de-DE", kind="PACKAGE", sha256="c" * 64,
                    uri="x", bytes=1, title_id="T", scene_id="S", market="de-DE",
                    parents=[ParentRef(stem.id, stem.sha256, "dub_stem")])
    for asset in (line, stem, package):
        store.record(asset)
    monkeypatch.setattr(conductor, "_STORE_ROOT", tmp_path)

    result = _toolbox(RangeStub()).dispatch("blast_radius", {})
    dependencies = {d["asset"]: d["would_invalidate"]
                    for d in result["dependencies"]}
    assert dependencies["T:S:dub_stem:de-DE"] == ["T:S:package:de-DE"]
    assert dependencies["T:S:dub_line:de-DE:00"] == ["T:S:dub_stem:de-DE"]
    assert "T:S:package:de-DE" not in dependencies, "a leaf breaks nothing"


def test_a_repairs_own_predecessor_is_not_downstream_of_it(tmp_path,
                                                           monkeypatch):
    """A repair records the version it replaced as a parent.

    That is real lineage and not a dependency. Counting it would report every
    repaired asset as its own blast radius, which is both wrong and alarming.
    """
    from media.store import Asset, ParentRef, Store
    import agents.conductor as conductor

    store = Store(tmp_path)
    store.record(Asset(
        id="T:S:dub_stem:de-DE", kind="DUB_STEM", sha256="b" * 64, uri="x",
        bytes=1, title_id="T", scene_id="S", market="de-DE",
        parents=[ParentRef("T:S:dub_stem:de-DE", "a" * 64, "pre_repair")]))
    monkeypatch.setattr(conductor, "_STORE_ROOT", tmp_path)

    result = _toolbox(RangeStub()).dispatch("blast_radius", {})
    assert result["dependencies"] == []


def test_blast_radius_only_reports_this_market(tmp_path, monkeypatch):
    """An agent working de-DE must not be handed fr-FR's dependency graph."""
    from media.store import Asset, ParentRef, Store
    import agents.conductor as conductor

    store = Store(tmp_path)
    for market in ("de-DE", "fr-FR"):
        line = Asset(id=f"T:S:dub_line:{market}", kind="ADAPTED_LINE",
                     sha256=market.ljust(64, "x"), uri="x", bytes=1,
                     title_id="T", scene_id="S", market=market)
        store.record(line)
        store.record(Asset(
            id=f"T:S:dub_stem:{market}", kind="DUB_STEM",
            sha256=market.rjust(64, "y"), uri="x", bytes=1, title_id="T",
            scene_id="S", market=market,
            parents=[ParentRef(line.id, line.sha256, "adapted_line")]))
    monkeypatch.setattr(conductor, "_STORE_ROOT", tmp_path)

    result = _toolbox(RangeStub()).dispatch("blast_radius", {})
    assert all("de-DE" in d["asset"] for d in result["dependencies"])
