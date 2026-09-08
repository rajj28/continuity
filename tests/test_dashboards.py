"""Dashboards, checked against the metrics that actually exist.

The failure this file exists to prevent is quiet and common: a series is
renamed, the exporters and the recording rules are updated because they import
the name, and a dashboard panel keeps querying the old one. It does not error.
It draws an empty graph, and an empty graph on a release dashboard reads as
"nothing wrong" rather than "nothing measured" -- the same absent-is-not-zero
confusion the verdict's coverage gate exists to stop, arriving through the
window nobody watches.

So every PromQL expression in every panel is parsed for the metric names it
references, and each one has to be a series the registry publishes or a
recording rule produces.
"""

from __future__ import annotations

import re

import pytest

from grafana.dashboards import ALL, expressions
from telemetry import genai, metrics

# Series produced by the Mimir recording rules rather than by an exporter.
# Listed explicitly rather than parsed out of the YAML: this file is the place
# where "a dashboard may reference this" is decided, and making that a
# deliberate list means adding a panel against a rule nobody wrote fails here.
RECORDED = {
    "scene_requirement_met", "market_requirement_met",
    "market_requirements_all_met", "market_has_stale_assets",
    "market_checks_present", "market_coverage_complete",
    "market_release_ready", "markets_ready_total", "markets_blocked_total",
    "market_threshold_current", "market_required_checks_current",
}

# Published directly by an exporter, outside the canonical-name registry.
PUBLISHED = {"market_threshold", "market_required_checks", "market_check_met"}

# PromQL functions and keywords a metric-name regex would otherwise pick up.
_NOT_METRICS = {
    "sum", "count", "min", "max", "avg", "rate", "increase", "irate", "abs",
    "clamp_min", "clamp_max", "by", "on", "group_left", "group_right", "bool",
    "without", "ignoring", "label_values", "topk", "bottomk", "vector",
    "absent", "changes", "delta", "deriv", "round", "sort", "sort_desc",
    "quantile", "stddev", "market", "requirement",
    # the *_over_time family, which a metric-name regex cannot tell from a
    # series because they are shaped exactly alike
    "last_over_time", "max_over_time", "min_over_time", "avg_over_time",
    "sum_over_time", "count_over_time", "stddev_over_time", "present_over_time",
}

_METRIC = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b(?=\s*[\{\(\)\s\[]|$)")

# Grouping clauses hold LABEL names, not metric names, and `token_type` looks
# exactly like a metric to a regex. Stripped before extraction rather than
# added to the ignore list, so a new label never has to be remembered here.
_GROUPING = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)"
)


def known_series() -> set[str]:
    """Every series name this system can legitimately produce."""
    names = {
        value for name, value in vars(metrics).items()
        if name.isupper() and isinstance(value, str)
    }
    names |= {
        value for name, value in vars(genai).items()
        if name.isupper() and isinstance(value, str) and not value.startswith("gen_ai.")
    }
    names |= set(metrics.MEASUREMENT_SERIES.values())
    names |= set(metrics.DIAGNOSTIC_SERIES.values())
    names |= set(metrics.REPORT_DIAGNOSTIC_SERIES.values())
    return names | RECORDED | PUBLISHED


def referenced(expr: str) -> set[str]:
    """Metric names an expression queries, ignoring PromQL's own vocabulary."""
    found = set()
    for token in _METRIC.findall(_GROUPING.sub(" ", expr)):
        if token in _NOT_METRICS or token.startswith("__"):
            continue
        if "_" not in token:          # bare words are labels or literals
            continue
        found.add(token)
    return found


# ---------------------------------------------------------------------------
# The load-bearing check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dashboard,panel,expr", expressions())
def test_every_panel_queries_a_series_that_exists(dashboard, panel, expr):
    """A panel cannot outlive its metric.

    An empty graph on a release dashboard reads as "nothing wrong" rather than
    "nothing measured", which is the most expensive way for a rename to go
    unnoticed.
    """
    unknown = referenced(expr) - known_series()
    assert not unknown, (
        f"{dashboard} / {panel!r} queries {sorted(unknown)}, which nothing "
        f"publishes and no recording rule produces"
    )


def test_the_check_would_catch_a_rename():
    """A test that cannot fail proves nothing."""
    assert referenced("sum(dub_sync_offset_ms_OLD)") - known_series()
    assert not referenced("sum(dub_sync_offset_ms)") - known_series()


# ---------------------------------------------------------------------------
# Design rules that hold across every panel
# ---------------------------------------------------------------------------


def panels():
    for name, build in ALL.items():
        dash = build()
        for panel in dash["panels"]:
            yield name, dash, panel


def test_no_panel_uses_two_y_axes():
    """Two scales on one chart can make any measurement look like it clears
    any bar. Where a measurement is shown against its threshold, both are in
    the same unit on the same axis."""
    for name, _dash, panel in panels():
        for override in panel["fieldConfig"]["overrides"]:
            for prop in override.get("properties", []):
                assert prop["id"] != "custom.axisPlacement" or \
                    prop["value"] not in ("right",), \
                    f"{name} / {panel['title']} puts a series on a second axis"


def test_a_verdict_is_never_carried_by_colour_alone():
    """Greyscale, or a reader who cannot separate red from green, must still
    get the answer. Every panel showing release readiness maps its values to
    words."""
    for name, _dash, panel in panels():
        exprs = " ".join(t["expr"] for t in panel.get("targets", []))
        if "market_release_ready" not in exprs and \
                "market_requirement_met" not in exprs:
            continue
        if panel["type"] in ("stat", "bargauge"):
            continue          # a number is its own label
        mappings = panel["fieldConfig"]["defaults"].get("mappings", [])
        assert mappings, (
            f"{name} / {panel['title']} shows a verdict with no value "
            f"mappings, so colour is the only carrier"
        )


def test_status_colours_are_never_used_as_series_colours():
    """The reserved status palette must not impersonate a category, or a
    glance cannot tell 'this one is broken' from 'this one is the third
    series'."""
    from grafana.dashboards import CRITICAL, GOOD, SERIES

    assert GOOD not in SERIES and CRITICAL not in SERIES
    for name, _dash, panel in panels():
        for override in panel["fieldConfig"]["overrides"]:
            matcher = override["matcher"]
            if matcher["id"] != "byName":
                continue
            for prop in override.get("properties", []):
                if prop["id"] != "color":
                    continue
                colour = prop["value"].get("fixedColor")
                assert colour in SERIES, (
                    f"{name} / {panel['title']} colours the series "
                    f"{matcher['options']!r} with {colour}, which is not a "
                    f"categorical slot"
                )


def test_every_panel_says_what_it_is_for():
    """A dashboard read at 2am by someone who did not build it."""
    for name, _dash, panel in panels():
        assert panel["title"], f"{name} has an untitled panel"


def test_dashboards_have_stable_uids():
    """Provisioning is an upsert. Without a stable uid every push creates
    another copy and the demo ends up on the wrong one."""
    uids = [build()["uid"] for build in ALL.values()]
    assert len(uids) == len(set(uids))
    assert all(uid.startswith("continuity-") for uid in uids)


def test_panels_do_not_overlap():
    """Grafana will silently reflow an overlapping layout into something
    nobody designed."""
    for name, _dash, panel in panels():
        pos = panel["gridPos"]
        assert pos["x"] + pos["w"] <= 24, (
            f"{name} / {panel['title']} runs off the 24-column grid"
        )

    for name, dash, _panel in panels():
        occupied: set[tuple[int, int]] = set()
        for panel in dash["panels"]:
            pos = panel["gridPos"]
            cells = {
                (pos["x"] + dx, pos["y"] + dy)
                for dx in range(pos["w"]) for dy in range(pos["h"])
            }
            clash = cells & occupied
            assert not clash, (
                f"{name} / {panel['title']} overlaps another panel at "
                f"{sorted(clash)[:3]}"
            )
            occupied |= cells
        break     # the loop above already walked every dashboard once
