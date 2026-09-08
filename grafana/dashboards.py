"""Dashboards, generated from the metric registry rather than hand-written.

A dashboard exported from the Grafana UI is a four-thousand-line JSON blob that
nobody diffs and that silently keeps querying a series after it is renamed. So
these are built in Python from `telemetry/metrics.py` -- the same module the
exporters and the recording rules take their names from -- and a test asserts
that every PromQL expression in every panel references a series this system
actually publishes. A panel cannot outlive its metric.

## Colour

Status and identity are different jobs and get different palettes, which is the
one rule that stops a dashboard lying to a glance.

**Status** -- can this market ship -- uses the reserved four-step status
palette, and never carries meaning alone: every verdict panel also maps its
value to the words READY or BLOCKED, so the answer survives being printed in
greyscale or read by someone who cannot separate red from green.

**Identity** -- which tool, which strategy, which rejection reason -- uses the
validated eight-hue categorical order, assigned in fixed slot order and never
cycled. Slots are pinned per series by name, so filtering to three strategies
does not repaint the survivors.

The dark steps are used because Grafana's default theme is dark and that is
where these will be read. The light steps are in the same table if that changes.

## Panels

Three dashboards, because they answer three different questions:

    Release Control Room   can we ship, and what is stopping us
    Agent Observability    what did the agent do, and did the guardrails hold
    Media QC               the measurements, each against the bar it is judged by

The second is the one worth looking at twice. `continuity_agent_rejections_total`
turns "the model cannot cite evidence it never gathered" from an assertion into
a line on a graph.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry.genai import (  # noqa: E402
    AGENT_STEPS,
    DECISIONS,
    OPERATION_DURATION,
    REJECTIONS,
    TOKEN_USAGE,
    TOOL_CALLS,
)
from telemetry.metrics import (  # noqa: E402
    AD_COLLISION,
    ASSET_STALE,
    DRIFT_SYSTEMATIC,
    LINE_OVERRUN,
    LOUDNESS,
    RELEASE_READY,
    REPAIRS,
    SUBTITLE_RATE,
    SYNC_OFFSET,
    TRUE_PEAK,
)

PROM = {"type": "prometheus", "uid": "grafanacloud-prom"}

# Validated categorical order, dark steps. Assigned by slot, never cycled.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500",
          "#d55181", "#008300", "#9085e9", "#e66767"]

# Reserved status palette. Never reused for a series.
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"

# Verdict text, so colour is never the only carrier of the answer.
VERDICT_TEXT = [{
    "type": "value",
    "options": {
        "0": {"text": "BLOCKED", "color": CRITICAL, "index": 0},
        "1": {"text": "READY", "color": GOOD, "index": 1},
    },
}]


def _target(expr: str, legend: str = "", *, instant: bool = False) -> dict:
    return {
        "expr": expr, "refId": "A", "datasource": PROM,
        "legendFormat": legend or "{{market}}",
        "instant": instant, "range": not instant,
    }


def _panel(kind: str, title: str, targets: list[dict], *, w: int, h: int,
           x: int, y: int, defaults: dict | None = None,
           options: dict | None = None, overrides: list | None = None,
           description: str = "") -> dict:
    for i, t in enumerate(targets):
        t["refId"] = chr(ord("A") + i)
    return {
        "type": kind, "title": title, "description": description,
        "datasource": PROM, "targets": targets,
        "gridPos": {"w": w, "h": h, "x": x, "y": y},
        "fieldConfig": {"defaults": defaults or {}, "overrides": overrides or []},
        "options": options or {},
    }


def _by_name(mapping: dict[str, str]) -> list[dict]:
    """Pin a colour to a named series so a filter cannot repaint the rest."""
    return [{
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color",
                        "value": {"mode": "fixed", "fixedColor": colour}}],
    } for name, colour in mapping.items()]


_LEGEND = {"showLegend": True, "displayMode": "table",
           "placement": "bottom", "calcs": ["lastNotNull"]}
_TOOLTIP = {"mode": "multi", "sort": "desc"}

# Thin marks, recessive grid -- the chart's job is the data, not its furniture.
_LINE = {
    "custom": {
        "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
        "pointSize": 8, "axisSoftMin": 0,
    },
}


# ---------------------------------------------------------------------------
# Release Control Room
# ---------------------------------------------------------------------------


def control_room() -> dict:
    panels = [
        _panel(
            "stat", "Markets ready to ship",
            [_target(f"sum({RELEASE_READY})", "ready", instant=True)],
            w=4, h=5, x=0, y=0,
            description="Grafana's own answer. No agent can write this series.",
            defaults={
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": CRITICAL, "value": None},
                    {"color": GOOD, "value": 1},
                ]},
                "unit": "none", "min": 0,
            },
            options={"colorMode": "background", "graphMode": "none",
                     "textMode": "value_and_name",
                     "reduceOptions": {"calcs": ["lastNotNull"]}},
        ),
        _panel(
            "stat", "Markets blocked",
            [_target(f"count({RELEASE_READY}) - sum({RELEASE_READY})",
                     "blocked", instant=True)],
            w=4, h=5, x=4, y=0,
            defaults={
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": GOOD, "value": None},
                    {"color": CRITICAL, "value": 1},
                ]},
                "unit": "none", "min": 0,
            },
            options={"colorMode": "background", "graphMode": "none",
                     "textMode": "value_and_name",
                     "reduceOptions": {"calcs": ["lastNotNull"]}},
        ),
        _panel(
            "bargauge", "Checks measured, against checks owed",
            # An explicit join: market_checks_present carries {title, market}
            # while the owed count carries {market} alone, and a bare `/`
            # between mismatched label sets silently matches nothing.
            [_target("market_checks_present "
                     "/ on (market) group_left () "
                     "market_required_checks_current", "{{market}}",
                     instant=True)],
            w=16, h=5, x=8, y=0,
            description=(
                "A market can have nothing failing and still be unshippable. "
                "Anything under 100% is a requirement nobody measured, which "
                "blocks exactly as a failing one does."
            ),
            defaults={
                "unit": "percentunit", "min": 0, "max": 1,
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": CRITICAL, "value": None},
                    {"color": WARNING, "value": 0.75},
                    {"color": GOOD, "value": 1},
                ]},
            },
            options={"displayMode": "gradient", "orientation": "horizontal",
                     "reduceOptions": {"calcs": ["lastNotNull"]}},
        ),
        _panel(
            "state-timeline", "Release verdict over time",
            [_target(RELEASE_READY, "{{market}}")],
            w=24, h=8, x=0, y=5,
            description=(
                "Every transition here is an asset changing, because the only "
                "way to move this series is to change the content until the "
                "measurement changes."
            ),
            defaults={
                "mappings": VERDICT_TEXT, "min": 0, "max": 1,
                "custom": {"lineWidth": 0, "fillOpacity": 90},
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": CRITICAL, "value": None},
                    {"color": GOOD, "value": 1},
                ]},
            },
            options={"showValue": "auto", "mergeValues": True,
                     "legend": {"showLegend": True, "displayMode": "list"}},
        ),
        _panel(
            "table", "What is blocking each market",
            [_target("market_requirement_met == 0",
                     "{{market}} / {{requirement}}", instant=True)],
            w=24, h=9, x=0, y=13,
            description=(
                "Localisation, audio delivery, timed text, technical "
                "conformance, rights and packaging all land in one series. A "
                "right that is not cleared and a dub that is out of sync are "
                "both just reasons this market cannot ship."
            ),
            defaults={
                "custom": {"align": "left",
                           "cellOptions": {"type": "color-text"}},
                "mappings": [{"type": "value", "options": {
                    "0": {"text": "BLOCKING", "color": CRITICAL, "index": 0},
                }}],
            },
            options={"showHeader": True},
        ),
        _panel(
            "timeseries", "Assets stale against their parent",
            [_target(f"sum by (market) ({ASSET_STALE})", "{{market}}")],
            w=12, h=7, x=0, y=22,
            description=(
                "An asset whose recorded parent hash no longer matches. Pure "
                "function of hashes on disk -- nothing is invalidated, so "
                "nothing can drift."
            ),
            defaults={**_LINE, "unit": "none",
                      "color": {"mode": "palette-classic"}},
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "timeseries", "Repairs by verified outcome",
            [_target(f"sum by (outcome) (max_over_time({REPAIRS}[$__range]))",
                     "{{outcome}}")],
            w=12, h=7, x=12, y=22,
            description=(
                "`lucky` is a repair that passed while its prediction did NOT "
                "hold. It raises the denominator of earned autonomy without "
                "raising the numerator, so getting away with it costs a "
                "strategy privileges rather than earning them."
            ),
            defaults={**_LINE, "unit": "none"},
            overrides=_by_name({
                "succeeded": SERIES[2], "lucky": SERIES[3], "failed": SERIES[7],
            }),
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
    ]
    return _dashboard("continuity-control-room", "Continuity — Release Control Room",
                      panels, ["continuity", "release"])


# ---------------------------------------------------------------------------
# Agent Observability
# ---------------------------------------------------------------------------


def agent() -> dict:
    panels = [
        _panel(
            "timeseries", "Guardrail rejections, by reason",
            [_target(f"sum by (reason) (max_over_time({REJECTIONS}[$__range]))",
                     "{{reason}}")],
            w=24, h=8, x=0, y=0,
            description=(
                "The panel that makes the guardrails checkable rather than "
                "claimed. Each line is a model proposal the contracts refused: "
                "`uncited_evidence` is a query it never ran, "
                "`incoherent_prediction` is restating the current value as the "
                "goal, `unexecutable_strategy` is a repair with no "
                "implementation. Flat at zero means either a well-behaved "
                "model or a guardrail that never fires -- only the shape over "
                "time tells you which."
            ),
            defaults={**_LINE, "unit": "none"},
            overrides=_by_name({
                "uncited_evidence": SERIES[7], "no_evidence": SERIES[1],
                "incoherent_prediction": SERIES[3],
                "unexecutable_strategy": SERIES[4],
                "unknown_strategy": SERIES[6], "bad_params": SERIES[0],
                "unmeasurable_prediction": SERIES[2],
            }),
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "timeseries", "Tool calls the agent made",
            [_target(f"sum by (tool) (max_over_time({TOOL_CALLS}[$__range]))", "{{tool}}")],
            w=12, h=7, x=0, y=8,
            description=(
                "The sequence of tool calls IS the reasoning. A run that "
                "concluded wrongly and a run that never looked are otherwise "
                "indistinguishable."
            ),
            defaults={**_LINE, "unit": "none",
                      "color": {"mode": "palette-classic"}},
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "timeseries", "How each run ended",
            [_target(f"sum by (outcome) (max_over_time({DECISIONS}[$__range]))",
                     "{{outcome}}")],
            w=12, h=7, x=12, y=8,
            description=(
                "`escalate` is a first-class outcome, not a failure: a right "
                "that is not cleared cannot be repaired by anyone here."
            ),
            defaults={**_LINE, "unit": "none"},
            overrides=_by_name({
                "repair": SERIES[2], "escalate": SERIES[3],
                "stalled": SERIES[7], "budget_exhausted": SERIES[1],
            }),
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "timeseries", "Tokens per model call",
            [_target(f"sum by (token_type) (max_over_time({TOKEN_USAGE}[$__range]))", "{{token_type}}")],
            w=8, h=7, x=0, y=15,
            description="OTel GenAI semantic conventions, read unmodified.",
            defaults={**_LINE, "unit": "short"},
            overrides=_by_name({"input": SERIES[0], "output": SERIES[1]}),
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "timeseries", "Model call duration",
            [_target(f"max_over_time({OPERATION_DURATION}[$__range])", "{{model}}")],
            w=8, h=7, x=8, y=15,
            defaults={**_LINE, "unit": "s",
                      "color": {"mode": "palette-classic"}},
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "timeseries", "Control-loop steps",
            [_target(f"sum by (step) (max_over_time({AGENT_STEPS}[$__range]))", "{{step}}")],
            w=8, h=7, x=16, y=15,
            description=(
                "The loop begins in Grafana. An alert fires, its contact point "
                "posts to the receiver, and the Conductor starts -- with no "
                "prompt at the front of it."
            ),
            defaults={**_LINE, "unit": "none",
                      "color": {"mode": "palette-classic"}},
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _panel(
            "bargauge", "Earned autonomy: success rate by strategy",
            [_target(
                f'sum by (strategy) (max_over_time({REPAIRS}{{outcome="succeeded"}}[$__range])) / '
                f"clamp_min(sum by (strategy) (max_over_time({REPAIRS}[$__range])), 1)",
                "{{strategy}}", instant=True)],
            w=24, h=6, x=0, y=22,
            description=(
                "Read out of Prometheus at decision time, never held in agent "
                "memory. The ledger is external, append-only and visible, so "
                "autonomy cannot be argued upward inside a prompt -- and a "
                "strategy can be watched losing its privileges live."
            ),
            defaults={
                "unit": "percentunit", "min": 0, "max": 1,
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": CRITICAL, "value": None},
                    {"color": WARNING, "value": 0.3},
                    {"color": GOOD, "value": 0.6},
                ]},
            },
            options={"displayMode": "gradient", "orientation": "horizontal",
                     "reduceOptions": {"calcs": ["lastNotNull"]}},
        ),
    ]
    return _dashboard("continuity-agent", "Continuity — Agent Observability",
                      panels, ["continuity", "agent", "genai"])


# ---------------------------------------------------------------------------
# Media QC
# ---------------------------------------------------------------------------


def _measured_vs_bar(title: str, series: str, requirement: str, unit: str,
                     *, x: int, y: int, w: int = 12, h: int = 7,
                     description: str = "") -> dict:
    """One measurement and the bar it is judged against, on one axis.

    Both are milliseconds, or both LUFS -- never two scales on two axes. A
    dual-axis chart can make any measurement look like it clears any bar.
    """
    return _panel(
        "timeseries", title,
        [
            _target(f'{series}{{market=~"$market"}}', "{{market}}"),
            _target(f'market_threshold_current{{requirement="{requirement}",'
                    f'market=~"$market"}}', "limit — {{market}}"),
        ],
        w=w, h=h, x=x, y=y, description=description,
        defaults={**_LINE, "unit": unit,
                  "color": {"mode": "palette-classic"}},
        overrides=[{
            "matcher": {"id": "byRegexp", "options": "limit.*"},
            "properties": [
                {"id": "color", "value": {"mode": "fixed",
                                          "fixedColor": CRITICAL}},
                {"id": "custom.lineStyle",
                 "value": {"fill": "dash", "dash": [10, 6]}},
                {"id": "custom.lineWidth", "value": 1},
            ],
        }],
        options={"legend": _LEGEND, "tooltip": _TOOLTIP},
    )


def media_qc() -> dict:
    panels = [
        _measured_vs_bar(
            "Dub sync offset vs tolerance", SYNC_OFFSET, "dub_sync_max_ms",
            "ms", x=0, y=0,
            description=(
                "Worst onset drift across the scene. Reported as the maximum "
                "because a release blocks on its worst moment, not its average."
            ),
        ),
        _panel(
            "timeseries", "Is the drift systematic?",
            [_target(f'{DRIFT_SYSTEMATIC}{{market=~"$market"}}', "{{market}}")],
            w=12, h=7, x=12, y=0,
            description=(
                "The whole repair decision. 1 means every line drifts by a "
                "similar amount -- the stem is offset and shifting it fixes "
                "everything. 0 means the drift grows down the scene, so the "
                "lines are too long and no shift will help. Both look "
                "identical in the sync measurement above."
            ),
            defaults={
                "min": 0, "max": 1, "unit": "none",
                "custom": {"lineWidth": 2, "fillOpacity": 20,
                           "showPoints": "never"},
                "mappings": [{"type": "value", "options": {
                    "0": {"text": "progressive — REWRITE", "index": 0},
                    "1": {"text": "systematic — RETIME", "index": 1},
                }}],
                "color": {"mode": "palette-classic"},
            },
            options={"legend": _LEGEND, "tooltip": _TOOLTIP},
        ),
        _measured_vs_bar(
            "Line overrun vs tolerance", LINE_OVERRUN, "line_overrun_max_ms",
            "ms", x=0, y=7,
            description=(
                "How far the worst line runs past its slot. A distinct failure "
                "from onset drift: when the gaps between cues absorb an "
                "overrun, every onset lands exactly right while the line is "
                "still being spoken over the next shot."
            ),
        ),
        _measured_vs_bar(
            "Audio description collision", AD_COLLISION, "ad_collision_max_ms",
            "ms", x=12, y=7,
            description=(
                "Narration overlapping dialogue. The tolerance is zero, "
                "because there is no amount of talking over the dialogue that "
                "is acceptable."
            ),
        ),
        _measured_vs_bar(
            "Integrated loudness vs target", LOUDNESS, "loudness_target_lufs",
            "none", x=0, y=14,
            description=(
                "A two-sided band, not a ceiling: too quiet fails delivery "
                "exactly as surely as too loud."
            ),
        ),
        _measured_vs_bar(
            "True peak vs ceiling", TRUE_PEAK, "true_peak_max_dbtp",
            "none", x=12, y=14,
        ),
        _measured_vs_bar(
            "Subtitle reading rate vs limit", SUBTITLE_RATE, "subtitle_max_cps",
            "none", x=0, y=21,
            description=(
                "The subtitle is the dubbing script laid back onto the "
                "picture, so a line that grew to carry the meaning has to be "
                "read faster. Japan's 11 cps is a far tighter bar than "
                "Germany's 20, and the same adaptation can pass as audio and "
                "fail as text."
            ),
        ),
        _panel(
            "table", "Per-requirement result, every dimension",
            [_target('market_requirement_met{market=~"$market"}',
                     "{{market}} / {{requirement}}", instant=True)],
            w=12, h=7, x=12, y=21,
            defaults={
                "mappings": [{"type": "value", "options": {
                    "0": {"text": "FAIL", "color": CRITICAL, "index": 0},
                    "1": {"text": "pass", "color": GOOD, "index": 1},
                }}],
                "custom": {"align": "left", "cellOptions": {"type": "color-text"}},
            },
            options={"showHeader": True},
        ),
    ]
    return _dashboard(
        "continuity-media-qc", "Continuity — Media QC", panels,
        ["continuity", "qc"],
        templating=[{
            "name": "market", "type": "query", "datasource": PROM,
            "query": f"label_values({SYNC_OFFSET}, market)",
            "multi": True, "includeAll": True, "current": {
                "text": "All", "value": "$__all",
            },
            "refresh": 2, "label": "Market",
        }],
    )


# ---------------------------------------------------------------------------


def _dashboard(uid: str, title: str, panels: list[dict], tags: list[str],
               templating: list[dict] | None = None) -> dict:
    return {
        "uid": uid, "title": title, "tags": tags, "timezone": "browser",
        "schemaVersion": 39, "version": 0, "editable": True,
        "refresh": "30s",
        "time": {"from": "now-3h", "to": "now"},
        "templating": {"list": templating or []},
        "panels": panels,
    }


ALL = {"control_room": control_room, "agent": agent, "media_qc": media_qc}


def expressions() -> list[tuple[str, str, str]]:
    """Every PromQL expression, as (dashboard, panel, expr). For the tests."""
    out = []
    for name, build in ALL.items():
        dash = build()
        for panel in dash["panels"]:
            for target in panel.get("targets", []):
                out.append((name, panel["title"], target["expr"]))
    return out


def provision(env: dict[str, str]) -> int:
    token = env.get("GRAFANA_PROVISIONER_TOKEN") or env["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
    base = env["GRAFANA_URL"].rstrip("/")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    pushed = 0
    for name, build in ALL.items():
        dashboard = build()
        response = session.post(f"{base}/api/dashboards/db", json={
            "dashboard": dashboard, "overwrite": True,
            "message": "provisioned from grafana/dashboards.py",
        })
        if response.status_code >= 300:
            raise RuntimeError(
                f"{name} -> {response.status_code} {response.text[:300]}"
            )
        url = response.json().get("url", "")
        print(f"  {dashboard['title']:<44} {base}{url}")
        pushed += 1
    return pushed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", dest="show", action="store_true",
                        help="write the JSON to stdout instead of pushing it")
    args = parser.parse_args()

    if args.show:
        import json
        print(json.dumps({n: b() for n, b in ALL.items()}, indent=2))
        return 0

    from telemetry.otel import load_env
    print(f"provisioning {len(ALL)} dashboards")
    provision(load_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
