"""The verdict rules, tested as code.

`market_release_ready` decides whether content ships. It is the most
consequential logic in the system and the one thing no agent may write, so it
gets promtool's rule-unit-test treatment inside the normal pytest run rather
than a manual check against a dashboard.

Also asserts the two invariants that make the rules trustworthy at all:
the thresholds the rules join against are actually publishable from the
versioned profiles, and the test suite genuinely fails when the verdict is
weakened.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "grafana" / "rules"
TESTS = RULES / "tests" / "verdict_test.yaml"
PROMTOOL = ROOT / "bin" / "promtool.exe"


def _promtool() -> str:
    if PROMTOOL.exists():
        return str(PROMTOOL)
    found = shutil.which("promtool")
    if found:
        return found
    pytest.skip("promtool not available; see docs/SETUP.md")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_promtool(), *args], cwd=ROOT, capture_output=True, text=True
    )


def test_rule_files_are_valid():
    result = _run("check", "rules",
                  str(RULES / "recording.yaml"), str(RULES / "alerts.yaml"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS" in result.stdout


def test_verdict_unit_tests_pass():
    result = _run("test", "rules", str(TESTS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS" in result.stdout


def test_the_verdict_tests_actually_bite(tmp_path):
    """A suite that cannot fail proves nothing.

    Weaken the verdict so it ignores staleness -- the exact regression that
    would let a market ship on assets built from a superseded master -- and
    confirm the suite catches it.
    """
    original = (RULES / "recording.yaml").read_text(encoding="utf-8")
    weakened = original.replace(
        """          market_requirements_all_met
            * on (title, market) group_left ()
          (1 - market_has_stale_assets)""",
        """          market_requirements_all_met""",
    )
    assert weakened != original, "sabotage target not found; update this test"

    backup = tmp_path / "recording.yaml"
    backup.write_text(original, encoding="utf-8")
    try:
        (RULES / "recording.yaml").write_text(weakened, encoding="utf-8")
        result = _run("test", "rules", str(TESTS))
        assert result.returncode != 0, "weakened verdict still passed its tests"
        # promtool reports a passing run on stdout but writes failure detail
        # to stderr, so check both rather than assuming one.
        report = result.stdout + result.stderr
        assert "FAILED" in report
        assert "market_release_ready" in report
    finally:
        (RULES / "recording.yaml").write_text(original, encoding="utf-8")

    assert _run("test", "rules", str(TESTS)).returncode == 0


def test_every_threshold_the_rules_join_on_is_publishable():
    """The rules compare against `market_threshold{requirement=...}`. Each of
    those requirement labels must be derivable from the versioned profiles, or
    the join silently yields nothing and the verdict goes absent."""
    import re

    from media.qc.profiles import load_profiles
    from telemetry.exporters.thresholds import PUBLISHED, _dig

    rules_text = (RULES / "recording.yaml").read_text(encoding="utf-8")
    needed = set(re.findall(r'market_threshold\{requirement="([^"]+)"\}', rules_text))
    assert needed, "no threshold joins found; did the rules change shape?"

    publishable = set(PUBLISHED.values())
    assert needed <= publishable, (
        f"rules join on thresholds nothing publishes: {needed - publishable}"
    )

    for market, profile in load_profiles().items():
        for path, requirement in PUBLISHED.items():
            if requirement in needed:
                assert _dig(profile, path) is not None, (
                    f"{market} has no value at {path}"
                )
