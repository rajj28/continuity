"""The control room: one screen that answers "can we ship, and what is stopping us".

Not a dashboard. Grafana is the dashboard, and it is better at that than
anything written here would be. This is the operator surface -- the place where
a release manager sees every market at once, reads why one is blocked in the
words of the check that blocked it, and approves the repair an agent proposed
but is not yet trusted to run alone.

Everything it displays is read live from Grafana through the same `Signal` the
agents use, which is the point: the operator and the agent are looking at
exactly the same numbers, obtained the same way. A UI with its own data path
would eventually disagree with the system it is supposed to be showing.

The one thing it can do rather than show is approve a pending proposal. That is
the human half of the autonomy ladder, and it belongs on a screen rather than
in a shell flag -- RECOMMEND means a person decides, and a person deciding is
an interface, not a command-line argument.

Local by design. It holds Grafana credentials and can start work on real
assets, so it binds to localhost and stays there until the Cloud Run deployment
puts it behind a real identity.
"""

from __future__ import annotations

import json
import logging
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
from agents.ledger import Ledger  # noqa: E402
from agents.mcp import grafana_client  # noqa: E402
from agents.signal import Signal  # noqa: E402
from media.qc.profiles import load_profiles  # noqa: E402
from telemetry.exporters.thresholds import required_checks  # noqa: E402
from telemetry.otel import load_env  # noqa: E402

log = logging.getLogger("continuity.ui")

TITLE = "SINTEL"
STORE = ROOT / "out" / "store"

# Which dimension each requirement belongs to. The grouping is the whole point
# of the screen: "de-DE fails ad_collision" means nothing to a release manager,
# while "Accessibility: the audio description talks over dialogue" does.
DIMENSIONS: dict[str, tuple[str, str]] = {
    "dub_sync": ("Localisation", "Dub timing against picture"),
    "line_overrun": ("Localisation", "Lines running past their slots"),
    "speech_rate": ("Localisation", "Delivery pace"),
    "semantic_fidelity": ("Localisation", "Meaning preserved in translation"),
    "loudness": ("Audio delivery", "Integrated loudness"),
    "true_peak": ("Audio delivery", "True peak ceiling"),
    "subtitle_rate": ("Timed text", "Subtitle reading rate"),
    "ad_collision": ("Accessibility", "Audio description over dialogue"),
    "tech_video_height": ("Technical", "Resolution"),
    "tech_frame_rate": ("Technical", "Frame rate"),
    "tech_video_codec": ("Technical", "Video codec"),
    "tech_pixel_format": ("Technical", "Pixel format"),
    "tech_audio_channels": ("Technical", "Audio configuration"),
    "tech_audio_sample_rate": ("Technical", "Sample rate"),
    "rights_cleared": ("Rights", "Territory clearance and windows"),
    "certified": ("Certification", "Age rating"),
    "deliverables_complete": ("Packaging", "Required deliverables"),
    "metadata_localised": ("Packaging", "Storefront record"),
    "forced_narrative": ("Packaging", "Forced narratives"),
}

# Which dimensions an agent can act on at all. Shown in the UI because the
# honest answer to "why is nobody fixing this" is sometimes "nobody here can".
UNREPAIRABLE = {"Rights", "Certification"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    """Reads the whole picture through the agents' own eyes."""

    def __init__(self) -> None:
        env = load_env()
        self.signal = Signal(grafana_client(env))
        self.ledger = Ledger(STORE)
        self.intents = IntentStore(STORE)
        self._lock = threading.Lock()

    def markets(self) -> list[dict[str, Any]]:
        profiles = load_profiles()
        verdicts = {
            e.detail.get("market"): e.value
            for e in self.signal.observe_all(f'market_release_ready{{title="{TITLE}"}}')
        }
        present = {
            e.detail.get("market"): e.value
            for e in self.signal.observe_all(f'market_checks_present{{title="{TITLE}"}}')
        }
        failing: dict[str, list[str]] = {}
        for evidence in self.signal.observe_all(
            f'market_requirement_met{{title="{TITLE}"}} == 0'
        ):
            failing.setdefault(evidence.detail.get("market", ""), []).append(
                evidence.detail.get("requirement", "?")
            )

        out = []
        for market, profile in profiles.items():
            owed = len(required_checks(profile))
            blockers = []
            for requirement in sorted(failing.get(market, [])):
                dimension, label = DIMENSIONS.get(
                    requirement, ("Other", requirement)
                )
                blockers.append({
                    "requirement": requirement,
                    "dimension": dimension,
                    "label": label,
                    "repairable": dimension not in UNREPAIRABLE,
                })
            verdict = verdicts.get(market)
            out.append({
                "market": market,
                "name": profile.get("name", market),
                "language": profile.get("language", ""),
                "ready": None if verdict is None else bool(verdict == 1),
                "present": int(present.get(market, 0)),
                "owed": owed,
                "blockers": blockers,
                "pending": self.intents.get(TITLE, market) is not None,
            })
        return sorted(out, key=lambda m: (m["ready"] is not True,
                                          -len(m["blockers"]), m["market"]))

    def measurements(self, market: str) -> list[dict[str, Any]]:
        """The numbers behind a market, each with the query that produced it.

        The queries are shown, not hidden. A release manager who does not
        believe a number should be able to paste it into Grafana, and an
        interface that cannot be checked is one that has to be trusted.
        """
        wanted = [
            ("dub_sync_offset_ms", "dub_sync_max_ms", "Dub sync", "ms"),
            ("dub_line_overrun_ms", "line_overrun_max_ms", "Line overrun", "ms"),
            ("ad_collision_ms", "ad_collision_max_ms", "AD collision", "ms"),
            ("audio_loudness_lufs", "loudness_target_lufs", "Loudness", "LUFS"),
            ("audio_true_peak_dbtp", "true_peak_max_dbtp", "True peak", "dBTP"),
            ("speech_rate_wpm", "speech_rate_max_wpm", "Speech rate", "wpm"),
            ("subtitle_reading_rate_cps", "subtitle_max_cps",
             "Subtitle rate", "cps"),
        ]
        rows = []
        for series, requirement, label, unit in wanted:
            measured = self.signal.measurement(series, title=TITLE, market=market)
            bar = self.signal.threshold(market, requirement)
            rows.append({
                "label": label, "unit": unit,
                "value": None if measured is None else measured.value,
                "bar": None if bar is None else bar.value,
                "query": measured.query if measured else
                         f'{series}{{title="{TITLE}",market="{market}"}}',
            })
        return rows

    def autonomy(self) -> list[dict[str, Any]]:
        rows = []
        for strategy in ("RETIME", "REMIX", "REWRITE"):
            for market in load_profiles():
                history = self.ledger.history(strategy, market)
                if not any(history.values()):
                    continue
                rows.append({
                    "strategy": strategy, "market": market, **history,
                    "tier": earned_tier(**history).name,
                })
        return rows

    def activity(self, limit: int = 12) -> list[dict[str, Any]]:
        entries = list(self.ledger.entries())[-limit:]
        return [
            {"at": e.at, "strategy": e.strategy, "market": e.market,
             "outcome": e.outcome, "predicted": e.predicted,
             "baseline": e.baseline, "observed": e.observed, "note": e.note}
            for e in reversed(entries)
        ]

    def proposal(self, market: str) -> dict[str, Any] | None:
        intent = self.intents.get(TITLE, market)
        if intent is None:
            return None
        return {
            "strategy": intent.strategy.value,
            "params": intent.params,
            "prediction": intent.prediction.describe(),
            "tier": intent.tier.name,
            "rationale": intent.rationale,
            "proposed_at": self.intents.proposed_at(TITLE, market),
            "evidence": [e.cite() for e in intent.justification],
        }

    def approve(self, market: str) -> dict[str, Any]:
        """Carry out the pending proposal. The human half of the ladder.

        Runs the same command an operator would, so there is one code path for
        a repair however it was started -- a UI that reimplemented the repair
        would be a second implementation to keep honest.
        """
        with self._lock:
            proc = subprocess.run(
                [sys.executable, "scripts/repair.py", "--market", market,
                 "--approve"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900,
            )
        return {
            "ok": proc.returncode == 0,
            "output": (proc.stdout or "")[-4000:],
            "error": (proc.stderr or "")[-2000:] if proc.returncode else "",
        }


class Handler(BaseHTTPRequestHandler):
    state: State

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            page = (Path(__file__).parent / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        if self.path == "/api/state":
            try:
                markets = self.state.markets()
                return self._json({
                    "at": _now(), "title": TITLE, "markets": markets,
                    "autonomy": self.state.autonomy(),
                    "activity": self.state.activity(),
                    "ready": sum(1 for m in markets if m["ready"] is True),
                    "blocked": sum(1 for m in markets if m["ready"] is not True),
                })
            except Exception as exc:                    # noqa: BLE001
                log.exception("state failed")
                return self._json({"error": str(exc)[:400]}, 502)
        if self.path.startswith("/api/market/"):
            market = self.path.rsplit("/", 1)[1]
            try:
                return self._json({
                    "market": market,
                    "measurements": self.state.measurements(market),
                    "proposal": self.state.proposal(market),
                })
            except Exception as exc:                    # noqa: BLE001
                return self._json({"error": str(exc)[:400]}, 502)
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/api/approve/"):
            return self._json({"error": "not found"}, 404)
        market = self.path.rsplit("/", 1)[1]
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        log.info("approving pending proposal for %s", market)
        try:
            return self._json(self.state.approve(market))
        except Exception as exc:                        # noqa: BLE001
            log.exception("approve failed")
            return self._json({"ok": False, "error": str(exc)[:400]}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    handler = type("H", (Handler,), {"state": State()})
    # localhost only: this holds Grafana credentials and can start work on real
    # assets. It goes public when Cloud Run puts a real identity in front of it.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"control room  http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
