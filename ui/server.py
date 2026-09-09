"""The control room: one screen that answers "can we ship, and what is stopping us".

Not a dashboard. Grafana is the dashboard and is better at it than anything
written here would be. This is the operator surface -- the place a release
manager sees every market against every dimension at once, reads why one is
blocked in the words of the check that blocked it, watches the agent
investigate, and approves a repair it is not yet trusted to run alone.

Everything it shows is read live through the same `Signal` the agents use, and
every number carries the PromQL that produced it. A release manager who does
not believe a figure can paste it into Grafana and get the same answer. An
interface that cannot be checked is one that has to be trusted.

## What it can do, and who may

Reads are open. Anything that CHANGES something -- running an investigation,
approving a repair -- requires the operator bearer token, because this service
is public and those actions start real work on real assets. A read-only visitor
sees the whole truth and can alter none of it, which is the right shape for a
control room anyone in the building can put on a wall.

## Streaming

An investigation is not instant and it is not a black box, so `/api/investigate`
streams server-sent events as the agent works: every tool call, its arguments
and its result, then the conclusion. Watching an agent decide is the difference
between believing it reasoned and taking its word for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.contracts import earned_tier  # noqa: E402
from agents.intents import IntentStore  # noqa: E402
from agents.ledger import Ledger, RejectionLog  # noqa: E402
from agents.mcp import grafana_client  # noqa: E402
from agents.signal import Signal  # noqa: E402
from media.qc.profiles import load_profiles  # noqa: E402
from media.store import Store  # noqa: E402
from telemetry.exporters.thresholds import required_checks  # noqa: E402
from telemetry.otel import load_env  # noqa: E402
from ui.release import Busy, Release, save_upload  # noqa: E402

log = logging.getLogger("continuity.ui")

TITLE = "SINTEL"
STORE = ROOT / "out" / "store"

# Dimension, human label, and whether an agent here can do anything about it.
# The grouping is the point of the screen: "de-DE fails ad_collision" means
# nothing to a release manager, and "Accessibility — audio description talks
# over dialogue" does.
CHECKS: dict[str, tuple[str, str, bool]] = {
    "dub_sync":               ("Localisation", "Dub timing against picture", True),
    "line_overrun":           ("Localisation", "Lines past their slots", True),
    "speech_rate":            ("Localisation", "Delivery pace", True),
    "semantic_fidelity":      ("Localisation", "Meaning preserved", True),
    "loudness":               ("Audio", "Integrated loudness", True),
    "true_peak":              ("Audio", "True peak ceiling", True),
    "subtitle_rate":          ("Timed text", "Subtitle reading rate", True),
    "ad_collision":           ("Accessibility", "Description over dialogue", True),
    "tech_video_height":      ("Technical", "Resolution", True),
    "tech_frame_rate":        ("Technical", "Frame rate", True),
    "tech_video_codec":       ("Technical", "Video codec", True),
    "tech_pixel_format":      ("Technical", "Pixel format", True),
    "tech_audio_channels":    ("Technical", "Audio configuration", False),
    "tech_audio_sample_rate": ("Technical", "Sample rate", True),
    "rights_cleared":         ("Rights", "Territory and windows", False),
    "certified":              ("Certification", "Age rating", False),
    "deliverables_complete":  ("Packaging", "Required deliverables", True),
    "metadata_localised":     ("Packaging", "Storefront record", True),
    "forced_narrative":       ("Packaging", "Forced narratives", True),
}

DIMENSIONS = ["Localisation", "Audio", "Timed text", "Accessibility",
              "Technical", "Rights", "Certification", "Packaging"]

# The measurements the detail panel shows, each against the bar it is judged by.
MEASURED = [
    ("dub_sync_offset_ms", "dub_sync_max_ms", "Dub sync", "ms", "lte"),
    ("dub_line_overrun_ms", "line_overrun_max_ms", "Line overrun", "ms", "lte"),
    ("ad_collision_ms", "ad_collision_max_ms", "AD collision", "ms", "lte"),
    ("audio_loudness_lufs", "loudness_target_lufs", "Loudness", "LUFS", "band"),
    ("audio_true_peak_dbtp", "true_peak_max_dbtp", "True peak", "dBTP", "lte"),
    ("speech_rate_wpm", "speech_rate_max_wpm", "Speech rate", "wpm", "lte"),
    ("subtitle_reading_rate_cps", "subtitle_max_cps", "Subtitle rate", "cps", "lte"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    """Everything the screen shows, read through the agents' own eyes."""

    def __init__(self) -> None:
        self.env = load_env()
        self.signal = Signal(grafana_client(self.env))
        self.ledger = Ledger(STORE)
        self.rejections = RejectionLog(STORE)
        self.intents = IntentStore(STORE)
        self.store = Store(STORE)
        self.token = self.env.get("WAKE_TOKEN", "")
        # Published deliberately. Investigations only -- see `_authorised`.
        self.demo_token = self.env.get("DEMO_TOKEN", "")
        self.release = Release()
        self._lock = threading.Lock()

    # -- the readiness matrix ---------------------------------------------

    def board(self) -> dict[str, Any]:
        profiles = load_profiles()
        verdicts = {
            e.detail.get("market"): e.value for e in
            self.signal.observe_all(f'market_release_ready{{title="{TITLE}"}}')
        }
        # Every check, not only the failing ones: a matrix that showed failures
        # alone could not distinguish "passing" from "never measured", which is
        # the distinction the whole verdict is built on.
        results: dict[str, dict[str, float]] = {}
        for e in self.signal.observe_all(f'market_requirement_met{{title="{TITLE}"}}'):
            market = e.detail.get("market", "")
            results.setdefault(market, {})[e.detail.get("requirement", "")] = e.value

        markets = []
        for market, profile in profiles.items():
            owed = required_checks(profile)
            seen = results.get(market, {})
            cells = []
            for dimension in DIMENSIONS:
                checks = []
                for name in owed:
                    if CHECKS.get(name, ("", "", True))[0] != dimension:
                        continue
                    label, repairable = CHECKS[name][1], CHECKS[name][2]
                    value = seen.get(name)
                    checks.append({
                        "check": name, "label": label,
                        "state": "unmeasured" if value is None
                                 else ("pass" if value == 1 else "fail"),
                        "repairable": repairable,
                    })
                cells.append({"dimension": dimension, "checks": checks})

            present = sum(1 for c in cells for k in c["checks"]
                          if k["state"] != "unmeasured")
            failing = [k for c in cells for k in c["checks"] if k["state"] == "fail"]
            verdict = verdicts.get(market)
            markets.append({
                "market": market,
                "name": profile.get("name", market),
                "language": profile.get("language", ""),
                "body": profile.get("ratings_body", ""),
                "ready": None if verdict is None else bool(verdict == 1),
                "present": present, "owed": len(owed),
                "cells": cells,
                "failing": len(failing),
                "blocked_by": sorted({k["label"] for k in failing}),
                "unrepairable": sorted({k["label"] for k in failing
                                        if not k["repairable"]}),
                "pending": self.intents.get(TITLE, market) is not None,
            })
        markets.sort(key=lambda m: (m["ready"] is True, -m["failing"], m["market"]))
        return {
            "at": _now(), "title": TITLE, "dimensions": DIMENSIONS,
            "markets": markets,
            "ready": sum(1 for m in markets if m["ready"] is True),
            "blocked": sum(1 for m in markets if m["ready"] is not True),
            "checks_total": sum(m["owed"] for m in markets),
        }

    # -- one market --------------------------------------------------------

    def market(self, market: str) -> dict[str, Any]:
        rows = []
        for series, requirement, label, unit, how in MEASURED:
            measured = self.signal.measurement(series, title=TITLE, market=market)
            bar = self.signal.threshold(market, requirement)
            state = "unmeasured"
            if measured is not None and bar is not None:
                if how == "band":
                    tol = self.signal.threshold(market, "loudness_tolerance_lu")
                    width = float(tol.value) if tol else 1.0
                    state = ("pass" if abs(measured.value - bar.value) <= width
                             else "fail")
                else:
                    state = "pass" if measured.value <= bar.value else "fail"
            rows.append({
                "label": label, "unit": unit, "state": state,
                "value": None if measured is None else measured.value,
                "bar": None if bar is None else bar.value,
                "query": measured.query if measured else
                         f'{series}{{title="{TITLE}",market="{market}"}}',
            })

        diagnostics = []
        for series, label in (("dub_drift_systematic", "Drift is systematic"),
                              ("dub_overrunning_lines", "Lines overrunning"),
                              ("dub_sync_p95_ms", "p95 onset drift")):
            e = self.signal.measurement(series, title=TITLE, market=market)
            if e is not None:
                diagnostics.append({"label": label, "value": e.value,
                                    "query": e.query})

        return {
            "market": market,
            "verdict": self.verdict(market),
            "measurements": rows,
            "diagnostics": diagnostics,
            "proposal": self.proposal(market),
            "assets": self.assets(market),
            "outputs": self.outputs(market),
            "blast_radius": self.blast_radius(market),
            "grafana": self.grafana_link(market),
        }

    # The three factors that multiply into market_release_ready, read back
    # individually. The screen already shows the ANSWER; this shows that the
    # answer is a Grafana recording rule with named inputs rather than
    # something this service decided.
    #
    # Worth the extra queries: "Grafana owns the verdict" is the claim the
    # whole design rests on, and a claim a reader can only take on trust is
    # weaker than one they can watch being computed. Each factor carries the
    # PromQL that produced it, so it can be pasted into Explore and checked.
    # Each label is phrased as a statement that is TRUE when the factor is
    # good, so the word, the marker and the value never disagree. "Something is
    # stale: 0" beside a green marker made a reader stop and work out which of
    # the three signals to believe.
    FACTORS = (
        ("market_requirements_all_met", "Everything measured, passed"),
        ("market_coverage_complete", "Everything owed, was measured"),
        ("market_has_stale_assets", "Nothing is stale against its parent", True),
    )

    def verdict(self, market: str) -> dict[str, Any]:
        factors = []
        for entry in self.FACTORS:
            series, label = entry[0], entry[1]
            inverted = len(entry) > 2
            e = self.signal.measurement(series, title=TITLE, market=market)
            factors.append({
                "label": label,
                "series": series,
                # An absent factor is not a passing one. market_release_ready
                # is absent when any input is absent, and this has to say the
                # same thing or it would explain a blocked market as ready.
                "state": "unknown" if e is None else (
                    "fail" if (e.value == 1) == inverted else "pass"),
                "value": None if e is None else e.value,
                "query": e.query if e else
                         f'{series}{{title="{TITLE}",market="{market}"}}',
            })
        ready = self.signal.measurement("market_release_ready",
                                        title=TITLE, market=market)
        return {
            "ready": None if ready is None else bool(ready.value == 1),
            "rule": "market_release_ready",
            "query": ready.query if ready else
                     f'market_release_ready{{title="{TITLE}",market="{market}"}}',
            "factors": factors,
        }

    def grafana_link(self, market: str) -> str:
        """Where to go to check any of this against the source of truth.

        The control room is a reading of Grafana, not a replacement for it, and
        an operator who distrusts a number should be one click from the place
        it came from rather than reverse-engineering a URL.
        """
        base = self.env.get("GRAFANA_URL", "").rstrip("/")
        if not base:
            return ""
        # continuity-media-qc rather than the control-room dashboard: it is the
        # one carrying a `market` template variable, so the link lands on this
        # market's measurements instead of on a board the operator then has to
        # filter by hand.
        return f"{base}/d/continuity-media-qc/?var-market={market}"

    def assets(self, market: str) -> list[dict[str, Any]]:
        """The lineage for this market: what exists, and what it was built from."""
        out = []
        for asset in self.store.all_assets():
            if asset.market != market:
                continue
            out.append({
                "id": asset.id, "kind": asset.kind, "version": asset.version,
                "sha256": asset.sha256[:12],
                "parents": [f"{p.asset_id.split(':')[-1]}@{p.sha256[:8]}"
                            for p in asset.parents[:4]],
                "stale": bool(self.store.is_stale(asset)),
            })
        return sorted(out, key=lambda a: (a["kind"], a["id"]))

    def blast_radius(self, market: str) -> list[dict[str, Any]]:
        """What a change to each asset would invalidate downstream.

        The question an operator asks before approving anything: if this repair
        rewrites the dub stem, what else stops being valid? Nothing else on
        this screen answers it -- the lineage panel shows what an asset was
        built FROM, and the direction that matters before acting is the other
        one.

        ## Read from the store, not from traces

        `Signal.blast_radius` answers the same question with a TraceQL search
        over `continuity.asset.parent_sha256`, and that is the right tool for
        the agent: it corroborates the store against what was actually
        observed. It is the wrong tool here. Traces are sampled, expire after
        14 days on the free tier, and the Tempo MCP server is in preview -- so
        a panel built on them would show an empty blast radius for an asset
        with real children and call it "nothing downstream", which is the most
        dangerous possible wrong answer to this particular question.

        The index is exact, always present, and needs no network. It is also
        what `docs/LIMITATIONS.md` already names as the authoritative lineage
        record, so reading it here is the documented position rather than a
        shortcut.
        """
        assets = self.store.all_assets()
        children: dict[str, list] = {}
        for asset in assets:
            for parent in asset.parents:
                # A repair records the version it replaced as its own parent.
                # That is real lineage but not a dependency -- an asset is not
                # downstream of itself, and counting it would report every
                # repaired asset as its own blast radius.
                if parent.asset_id == asset.id:
                    continue
                children.setdefault(parent.asset_id, []).append(asset)

        out = []
        for asset in assets:
            if asset.market != market:
                continue
            downstream = children.get(asset.id, [])
            if not downstream:
                continue
            out.append({
                "id": asset.id.split(":", 1)[1] if ":" in asset.id else asset.id,
                "kind": asset.kind,
                "affects": [{
                    "id": (child.id.split(":", 1)[1] if ":" in child.id
                           else child.id),
                    # Already stale means the change has ALREADY happened and
                    # this child has not caught up -- a present-tense problem
                    # rather than a consequence of the next repair.
                    "stale": bool(self.store.is_stale(child)),
                } for child in sorted(downstream, key=lambda a: a.id)],
            })
        return sorted(out, key=lambda a: -len(a["affects"]))

    def proposal(self, market: str) -> dict[str, Any] | None:
        intent = self.intents.get(TITLE, market)
        if intent is None:
            return None
        return {
            "strategy": intent.strategy.value, "params": intent.params,
            "prediction": intent.prediction.describe(), "tier": intent.tier.name,
            "rationale": intent.rationale,
            "proposed_at": self.intents.proposed_at(TITLE, market),
            "evidence": [e.cite() for e in intent.justification],
        }

    # -- the agent's record ------------------------------------------------

    def autonomy(self) -> list[dict[str, Any]]:
        rows = []
        for strategy in ("RETIME", "REMIX", "REWRITE"):
            for market in load_profiles():
                history = self.ledger.history(strategy, market)
                if not any(history.values()):
                    continue
                rows.append({"strategy": strategy, "market": market, **history,
                             "tier": earned_tier(**history).name})
        return rows

    def history(self, hours: int = 6) -> dict[str, Any]:
        """Each market's verdict over a window, as a timeline.

        A board that only shows the present cannot answer the first question a
        release manager actually asks, which is not "is it red" but "how long
        has it been red, and was it ever green". A market that went red four
        minutes ago is an incident; one that has been red all day is a plan.

        Drawn as bands rather than a line chart because the value is binary --
        a line between 0 and 1 implies intermediate states that do not exist,
        and reads as a sawtooth. Gaps stay gaps: the window before a market was
        ever measured is not "not ready", it is nothing, and it is drawn as
        nothing.
        """
        step = max(60, (hours * 3600) // 180)     # ~180 points, never sub-minute
        rows = self.signal.history(
            f'market_release_ready{{title="{TITLE}"}}',
            since=f"now-{hours}h", step_s=step,
        )
        markets = {}
        for row in rows:
            market = row["labels"].get("market", "")
            if market:
                markets[market] = [
                    {"t": int(t), "v": ("ready" if v == 1 else "blocked")}
                    for t, v in row["points"]
                ]
        return {"hours": hours, "step": step, "markets": markets,
                "query": f'market_release_ready{{title="{TITLE}"}}'}

    # What each kind of output is called on screen, and the order a person
    # would want to hear them in: the original first, so the dub has something
    # to be compared against.
    PLAYABLE = (
        ("SCENE_AUDIO", "Original audio", "the scene as delivered"),
        ("DUB_STEM", "Dubbed dialogue", "adapted and spoken by Gemini"),
        ("AUDIO_DESCRIPTION", "Audio description",
         "narration written into the gaps between lines"),
        ("PACKAGE", "The deliverable",
         "picture, dub, described track and subtitles in one file"),
    )
    MEDIA_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                   ".mp4": "video/mp4", ".m4a": "audio/mp4",
                   ".srt": "text/plain; charset=utf-8"}

    def outputs(self, market: str, scene: str = "S03") -> list[dict[str, Any]]:
        """The media this market's agents actually produced.

        Everything else on this screen is a representation of the work -- a
        pip, a number, a hash. This is the work. A dub you cannot hear is a
        row in a table, and a viewer has no way to tell it apart from one that
        was never made.
        """
        out = []
        for kind, label, note in self.PLAYABLE:
            for asset in self.store.all_assets():
                if asset.kind != kind or asset.scene_id != scene:
                    continue
                # Scene audio and picture belong to the title, not a market.
                if asset.market not in (market, None, ""):
                    continue
                path = Path(asset.uri)
                if not path.is_absolute():
                    path = ROOT / path
                if not path.exists():
                    continue
                out.append({
                    "asset": asset.id, "kind": kind, "label": label,
                    "note": note, "version": asset.version,
                    "media": self.MEDIA_TYPES.get(path.suffix.lower(), ""),
                    "bytes": path.stat().st_size,
                })
                break
        return out

    def media(self, asset_id: str) -> tuple[Path, str] | None:
        """Resolve an asset id to a file, or nothing.

        By id, never by path. The id is looked up in the index and the result
        is checked to be inside the project before a single byte is read --
        this endpoint is reachable from a public service, and "serve the file
        this string points at" is how a media route becomes a way to read
        anything on the disk.
        """
        try:
            asset = self.store.load(asset_id)
        except (OSError, ValueError, KeyError):
            return None
        path = Path(asset.uri)
        if not path.is_absolute():
            path = ROOT / path
        try:
            path = path.resolve(strict=True)
            path.relative_to(ROOT.resolve())
        except (OSError, ValueError):
            return None
        return path, self.MEDIA_TYPES.get(path.suffix.lower(),
                                          "application/octet-stream")

    def guardrails(self, limit: int = 6) -> dict[str, Any]:
        """What the contracts have refused, and how often.

        Read from disk rather than Prometheus so the panel is right the instant
        the page loads, and read at all because a guardrail nobody can watch
        fire is a guardrail nobody should believe. The same totals are in
        Grafana as `continuity_agent_rejections_total`; this is the operator's
        view of the same fact.
        """
        totals: dict[str, int] = {}
        for (_agent, reason), count in self.rejections.totals().items():
            totals[reason] = totals.get(reason, 0) + count
        return {
            "total": sum(totals.values()),
            "by_reason": sorted(totals.items(), key=lambda kv: -kv[1]),
            "recent": [
                {"reason": r.reason, "at": r.at, "market": r.market,
                 "detail": r.detail}
                for r in self.rejections.recent(limit)
            ],
        }

    def activity(self, limit: int = 10) -> list[dict[str, Any]]:
        entries = list(self.ledger.entries())[-limit:]
        return [{"at": e.at, "strategy": e.strategy, "market": e.market,
                 "outcome": e.outcome, "predicted": e.predicted,
                 "baseline": e.baseline, "observed": e.observed}
                for e in reversed(entries)]

    # -- actions -----------------------------------------------------------

    def has_media(self) -> bool:
        """Whether the actual audio is present, not merely its index.

        The hosted image carries the small half of the store -- lineage, QC
        reports, the ledger -- and none of the 240 MB of content-addressed
        media. A container is the wrong place for a film, so the deployed
        control room reads and investigates and cannot repair. Better to say
        that than to fail with a stack trace at the moment someone clicks.

        Asks whether the directory has anything IN it, not whether it exists:
        `Store.__init__` creates `objects/` unconditionally, so the hosted
        service reported it could repair while holding not one byte of audio.
        """
        objects = STORE / "objects"
        return objects.is_dir() and any(objects.iterdir())

    def approve(self, market: str) -> dict[str, Any]:
        """Carry out the pending proposal by running the same command an
        operator would. A UI that reimplemented the repair would be a second
        implementation to keep honest."""
        if not self.has_media():
            return {
                "ok": False,
                "output": "",
                "error": (
                    "This control room has the asset index and the repair "
                    "history but not the media itself, so it can read and "
                    "investigate and cannot repair. Run "
                    "`python scripts/repair.py --market " + market +
                    " --approve` where the pipeline holds the files."
                ),
            }
        with self._lock:
            proc = subprocess.run(
                [sys.executable, "scripts/repair.py", "--market", market,
                 "--approve"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900,
            )
        return {"ok": proc.returncode == 0,
                "output": (proc.stdout or "")[-6000:],
                "error": (proc.stderr or "")[-2000:] if proc.returncode else ""}

    def _dependencies(self, market: str) -> dict[str, list[str]]:
        """Who is built from whom in this market -- the apply order."""
        children: dict[str, list[str]] = {}
        for asset in self.store.all_assets():
            if asset.market != market:
                continue
            for parent in asset.parents:
                if parent.asset_id != asset.id:
                    children.setdefault(parent.asset_id, []).append(asset.id)
        return children

    def investigate(self, market: str, sink) -> None:
        """Wake every specialist this market's failures call for, at once.

        Streams the roster before they run and each conclusion as it lands, so
        an operator watches the swarm work rather than being handed its
        summary. That the compliance specialist has no repair tool is visible
        here and nowhere else.
        """
        from agents.investigate import investigate as run_investigation
        from agents.specialists import dispatch_for
        from agents.swarm import order_repairs
        from agents.swarm import swarm as run_swarm
        from agents.wake import Incident
        from media.model import client as model_client
        from telemetry.genai import GenAI
        from telemetry.metrics import Instruments
        from telemetry.otel import setup, shutdown

        tracer, meter = setup("continuity-ui")
        genai = GenAI(tracer, Instruments(meter), "conductor",
                      rejections=self.rejections)
        incident = Incident(
            fingerprint=f"ui:{TITLE}:{market}", alertname="MarketNotReleaseReady",
            action="investigate", status="firing", title_id=TITLE,
            market=market, severity="critical", started_at="",
        )
        try:
            sink({"type": "step", "name": "investigate",
                  "detail": "reading the verdict and what is failing"})
            inv = run_investigation(self.signal, incident)
            for finding in inv.findings:
                sink({"type": "finding", "claim": finding.claim,
                      "evidence": [e.cite() for e in finding.evidence]})
            for gap in inv.gaps:
                sink({"type": "gap", "detail": gap})
            if inv.resolved_before_we_arrived:
                sink({"type": "conclusion", "action": "none",
                      "detail": "the market recovered before we looked"})
                return

            failing = sorted(f.subject for f in inv.findings if f.subject)
            work = dispatch_for(failing)
            if not work:
                sink({"type": "conclusion", "action": "none",
                      "detail": "nothing failing to investigate"})
                return

            # Who is being woken, and what each may do. Sent before they run
            # so an operator watching sees the roster form -- including that
            # the compliance specialist has no repair tool at all, which is
            # the point and is invisible if you only see conclusions.
            sink({"type": "roster", "specialists": [
                {"name": s.name, "checks": checks, "may_repair": s.may_repair}
                for s, checks in work
            ]})
            sink({"type": "step", "name": "reason",
                  "detail": f"{len(work)} specialist(s) working in parallel"})

            result = asyncio.run(run_swarm(
                model_client(self.env), self.signal, inv, genai,
                failing=failing, scene="S03",
            ))

            for verdict in result.verdicts:
                who = verdict.name
                if verdict.error:
                    sink({"type": "conclusion", "agent": who,
                          "action": "error", "detail": verdict.error[:300]})
                    continue
                conclusion = verdict.conclusion
                if conclusion is None:
                    sink({"type": "conclusion", "agent": who,
                          "action": "none", "detail": "no conclusion"})
                elif conclusion.acted and conclusion.intent is not None:
                    i = conclusion.intent
                    sink({"type": "conclusion", "agent": who,
                          "action": "repair",
                          "strategy": i.strategy.value, "params": i.params,
                          "prediction": i.prediction.describe(),
                          "tier": i.tier.name, "rationale": i.rationale,
                          "evidence": [e.cite() for e in i.justification]})
                else:
                    sink({"type": "conclusion", "agent": who,
                          "action": conclusion.action,
                          "reason": conclusion.reason,
                          "detail": conclusion.summary[:400]})
                for name, why in (conclusion.rejections if conclusion else []):
                    sink({"type": "rejected", "agent": who,
                          "detail": f"{name}: {why}"})

            # The order the proposals would be applied in, and why there is an
            # order at all: repairing an asset gives it a new hash, so anything
            # built from it is stale until rebuilt.
            ordered = order_repairs(result.repairs, self._dependencies(market))
            if len(ordered) > 1:
                sink({"type": "plan", "order": [
                    {"agent": v.name,
                     "strategy": v.conclusion.intent.strategy.value,
                     "asset": v.conclusion.intent.target_asset_id}
                    for v in ordered
                ]})
        finally:
            shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "continuity-control/1.0"
    state: State

    # -- plumbing ----------------------------------------------------------

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _media(self, asset_id: str) -> None:
        """Stream a produced file, honouring Range.

        Range matters more than it looks: without it a browser cannot seek an
        audio element and Safari will not play one at all, so the dub would be
        listed and unplayable -- which is worse than not listing it.
        """
        found = self.state.media(asset_id)
        if found is None:
            return self._json({"error": "no such asset"}, 404)
        path, kind = found
        size = path.stat().st_size

        start, end = 0, size - 1
        header = self.headers.get("Range", "")
        partial = header.startswith("bytes=")
        if partial:
            first, _, last = header[6:].partition("-")
            try:
                start = int(first) if first else 0
                end = int(last) if last else size - 1
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(remaining, 256 * 1024))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # A browser that seeks abandons the request it had open.
                    # Entirely normal; not worth a stack trace in the log.
                    return
                remaining -= len(chunk)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _authorised(self, *, investigate_only: bool = False) -> bool:
        """Reads are open; anything that changes something needs the token.

        This service is public and these actions start real work on real
        assets. A visitor should see the whole truth and be able to alter none
        of it.

        Two tokens, and the difference is the point. The operator token opens
        everything. `DEMO_TOKEN` opens investigations and nothing else, so it
        can be published -- in a README, in a submission -- and let someone
        drive the agents without also handing them the ability to start a
        repair on real media. An investigation costs model quota and changes
        no asset; approving a repair rewrites audio.

        The wake receiver never accepts the demo token at all. It shares this
        service's image but not its door: a forged alert would start the whole
        autonomous path.
        """
        offered = self.headers.get("Authorization", "")
        if self.state.token and offered == f"Bearer {self.state.token}":
            return True
        if (investigate_only and self.state.demo_token
                and offered == f"Bearer {self.state.demo_token}"):
            return True
        # No operator token configured at all: local development, everything
        # open. Deploys refuse to start without one (see deploy/cloudrun.py).
        return not self.state.token

    def _drain(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            page = (Path(__file__).parent / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        # /api/health and not /healthz. Cloud Run's frontend reserves the
        # latter and answers it itself with a Google error page, so a probe
        # there reports the service down while it is serving every other path
        # perfectly -- which is worse than having no probe at all. Both are
        # answered so a local check written either way still works.
        if path in ("/api/health", "/healthz"):
            return self._json({"ok": True, "at": _now()})
        try:
            if path == "/api/board":
                board = self.state.board()
                board["autonomy"] = self.state.autonomy()
                board["activity"] = self.state.activity()
                board["locked"] = bool(self.state.token)
                board["can_repair"] = self.state.has_media()
                board["demo_open"] = bool(self.state.demo_token)
                board["guardrails"] = self.state.guardrails()
                return self._json(board)
            if path == "/api/media":
                from urllib.parse import parse_qs, urlparse
                asset = parse_qs(urlparse(self.path).query).get("asset", [""])[0]
                return self._media(asset)
            if path == "/api/sources":
                return self._json(self.state.release.sources())
            if path == "/api/history":
                return self._json(self.state.history())
            if path.startswith("/api/market/"):
                return self._json(self.state.market(path.rsplit("/", 1)[1]))
        except Exception as exc:                              # noqa: BLE001
            log.exception("read failed")
            return self._json({"error": str(exc)[:400]}, 502)
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # Upload first, and before the drain: the drain exists to empty a body
        # nobody wanted, and this is the one request whose body IS the point.
        if path == "/api/upload":
            if not self._authorised():
                self._drain()
                return self._json({"error": "operator token required"}, 401)
            return self._upload()

        self._drain()
        if path == "/api/release":
            if not self._authorised():
                return self._json({"error": (
                    "operator token required: a build speaks every line of "
                    "dialogue through a model and rewrites this title's assets"
                )}, 401)
            return self._start_release()
        if not (path.startswith("/api/approve/")
                or path.startswith("/api/investigate/")):
            return self._json({"error": "not found"}, 404)
        investigating = path.startswith("/api/investigate/")
        if not self._authorised(investigate_only=investigating):
            return self._json({"error": (
                "operator token required for this action"
                if not investigating else
                "a token is required to run an investigation; it costs model "
                "quota and this service is public"
            )}, 401)

        market = path.rsplit("/", 1)[1]
        if path.startswith("/api/approve/"):
            try:
                return self._json(self.state.approve(market))
            except Exception as exc:                          # noqa: BLE001
                log.exception("approve failed")
                return self._json({"ok": False, "error": str(exc)[:400]}, 500)
        return self._stream_investigation(market)

    # -- uploading a master ------------------------------------------------

    UPLOAD_MAX = 600 * 1024 * 1024

    def _upload(self) -> None:
        """Take a file off the wire and write it where a build can find it.

        Read in chunks and never into memory whole: a master is measured in
        hundreds of megabytes, and a service that holds one in a string to save
        eight lines here is a service that dies on the file it was built for.

        Hosted, Cloud Run caps a request body at 32 MB, so a full-length master
        arrives by being in the image rather than through this door. That is
        why the stock master is offered in the picker: the upload path is for
        the scene-length clips a person actually has to hand.
        """
        from urllib.parse import parse_qs, urlparse

        name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
        length = int(self.headers.get("Content-Length") or 0)
        if not name or length <= 0:
            self._drain()
            return self._json({"error": "expected ?name= and a body"}, 400)
        if length > self.UPLOAD_MAX:
            self._drain()
            return self._json({"error": "file is larger than 600 MB"}, 413)

        def chunks():
            left = length
            while left > 0:
                block = self.rfile.read(min(left, 1024 * 1024))
                if not block:
                    return
                left -= len(block)
                yield block

        try:
            return self._json(save_upload(name, chunks()))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except OSError as exc:                                # noqa: BLE001
            log.exception("upload failed")
            return self._json({"error": str(exc)[:300]}, 500)

    # -- building a release --------------------------------------------------

    def _start_release(self) -> None:
        """Validate the request, then hand the build to the event stream.

        Validation happens before a single byte of `text/event-stream` is
        written, because once the response is committed as a stream the only
        way left to report "you asked for a market that does not exist" is an
        event the page has to be written to notice.
        """
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        markets = [m for m in query.get("market", []) if m]
        scene = (query.get("scene", ["S03"])[0] or "S03")
        known = set(load_profiles())
        unknown = [m for m in markets if m not in known]
        if not markets:
            return self._json({"error": "choose at least one market"}, 400)
        if unknown:
            return self._json(
                {"error": "no profile for " + ", ".join(unknown)}, 400)
        if self.state.release.running is not None:
            return self._json({"error": "a build is already running"}, 409)

        try:
            master = self.state.release.resolve(
                query.get("master", [""])[0], subtitle=False)
            dialogue = self.state.release.resolve(
                query.get("dialogue", [""])[0], subtitle=True)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

        def work(sink) -> None:
            self.state.release.build(master, dialogue, markets, scene, sink)

        self._events(work, "build")

    def _stream_investigation(self, market: str) -> None:
        """Server-sent events, so an operator watches the agent rather than a
        spinner. Watching it decide is the difference between believing it
        reasoned and taking its word for it."""
        self._events(lambda sink: self.state.investigate(market, sink),
                     "investigation")

    def _events(self, worker, what: str) -> None:
        """One long response, one event per thing that happened.

        The worker runs on its own thread and posts into a queue rather than
        writing to the socket, so a slow client cannot slow the work down and a
        client that leaves cannot stop it. Both matter: an investigation costs
        model quota whether or not anyone is still watching, and a half-run
        build leaves assets whose parents were never recorded.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        events: queue.Queue = queue.Queue()
        done = object()

        def sink(event: dict) -> None:
            events.put(event)

        def work() -> None:
            try:
                worker(sink)
            except Busy as exc:
                events.put({"type": "error", "detail": str(exc)})
            except Exception as exc:                          # noqa: BLE001
                log.exception("%s failed", what)
                events.put({"type": "error", "detail": str(exc)[:400]})
            finally:
                events.put(done)

        threading.Thread(target=work, daemon=True).start()
        while True:
            event = events.get()
            if event is done:
                break
            try:
                self.wfile.write(
                    f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # The operator navigated away. The work keeps going -- an
                # agent's conclusion is written to the intent store and a
                # build's output to the asset store either way.
                log.info("client disconnected mid-%s", what)
                return
        try:
            self.wfile.write(b"data: {\"type\": \"end\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or 8090))
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 in a container; localhost everywhere else")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    handler = type("H", (Handler,), {"state": State()})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"control room  http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
