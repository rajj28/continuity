"""The verdict rules, tested as code.

`market_release_ready` decides whether content ships. It is the most
consequential logic in the system and the one thing no agent may write, so it
gets promtool's rule-unit-test treatment inside the normal pytest run rather
than a manual check against a dashboard.

Also asserts the invariants that make the rules trustworthy at all: the
thresholds the rules join against are publishable from the versioned profiles,
the coverage gate counts exactly the checks the rules evaluate, and the suite
genuinely fails when any clause of the verdict is weakened.
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


# Each entry drops one clause from the verdict. Both are regressions a
# plausible "simplification" would introduce, and both would ship content that
# was never validated -- so the suite has to reject them.
SABOTAGE = {
    "ignores_staleness": (
        """          market_coverage_complete
            * on (title, market) group_left ()
          (1 - market_has_stale_assets)""",
        """          market_coverage_complete""",
    ),
    "ignores_coverage": (
        """          market_requirements_all_met
            * on (title, market) group_left ()
          market_coverage_complete""",
        """          market_requirements_all_met""",
    ),
}


@pytest.mark.parametrize("name", sorted(SABOTAGE))
def test_the_verdict_tests_actually_bite(name):
    """A suite that cannot fail proves nothing.

    Weaken the verdict one clause at a time and confirm the suite catches each
    one. `ignores_staleness` would ship assets built from a superseded master;
    `ignores_coverage` would ship a market whose sync was never measured
    because min() over the surviving checks returns 1.
    """
    path = RULES / "recording.yaml"
    original = path.read_text(encoding="utf-8")
    target, replacement = SABOTAGE[name]
    assert target in original, f"sabotage target for {name} not found; update this test"
    weakened = original.replace(target, replacement, 1)

    try:
        path.write_text(weakened, encoding="utf-8")
        result = _run("test", "rules", str(TESTS))
        assert result.returncode != 0, f"{name}: weakened verdict still passed"
        # promtool reports a passing run on stdout but writes failure detail
        # to stderr, so check both rather than assuming one.
        report = result.stdout + result.stderr
        assert "FAILED" in report
        assert "market_release_ready" in report
    finally:
        path.write_text(original, encoding="utf-8")

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


def test_coverage_counts_exactly_the_checks_the_rules_evaluate():
    """`market_required_checks` is what the coverage gate compares against, so
    it has to name the same set of requirements the recording rules actually
    produce. Add a seventh `scene_requirement_met` rule without adding it to
    REQUIRED_CHECKS and the gate silently under-counts -- the market goes green
    one check short. This test is what stops that."""
    import re

    from telemetry.exporters.thresholds import CONDITIONAL_CHECKS, REQUIRED_CHECKS

    rules_text = (RULES / "recording.yaml").read_text(encoding="utf-8")
    # the `requirement:` label attached to each scene_requirement_met rule
    evaluated = set(re.findall(r"^\s+requirement:\s*(\S+)\s*$", rules_text, re.M))
    assert evaluated, "no requirement labels found; did the rules change shape?"
    # A rule cannot be conditional, so the rules evaluate every scene-level
    # check for everyone; coverage is where the condition is applied.
    countable = set(REQUIRED_CHECKS) | set(CONDITIONAL_CHECKS)
    assert evaluated == countable, (
        f"rules evaluate {sorted(evaluated)} but coverage counts "
        f"{sorted(countable)}"
    )


def test_a_conditional_check_is_counted_only_where_it_applies():
    """Counting audio description everywhere would penalise markets that never
    asked for it; counting it nowhere would excuse the ones that did."""
    from media.qc.profiles import load_profiles
    from telemetry.exporters.thresholds import CONDITIONAL_CHECKS, required_checks

    profiles = load_profiles()
    for check, deliverable in CONDITIONAL_CHECKS.items():
        wanted = {m for m, p in profiles.items()
                  if deliverable in p.get("requires", [])}
        judged = {m for m, p in profiles.items()
                  if check in required_checks(p)}
        assert wanted == judged, (
            f"{check} is owed by {sorted(wanted)} but counted for "
            f"{sorted(judged)}"
        )
        assert wanted, f"no market requires {deliverable}; the check is dead"


def test_every_market_owes_the_full_set_of_checks():
    """A profile missing a threshold quietly lowers that market's bar, because
    a check it cannot be judged on is not counted against it. That may be
    legitimate one day, but it must be a deliberate, visible choice -- so
    assert the current profiles demand all of them."""
    from media.qc.profiles import load_profiles
    from telemetry.exporters.thresholds import REQUIRED_CHECKS, required_checks

    for market, profile in load_profiles().items():
        owed = set(required_checks(profile))
        assert set(REQUIRED_CHECKS) <= owed, (
            f"{market} cannot be judged on "
            f"{sorted(set(REQUIRED_CHECKS) - owed)}"
        )


def test_a_release_is_judged_on_more_than_how_it_sounds():
    """The scope check. "Can we release this here" is not a localisation
    question with some extras bolted on -- rights, technical conformance and
    package completeness block real releases far more often than a dub does,
    and a market that measured only its audio would ship into a territory it
    has no licence for."""
    from media.qc.profiles import load_profiles
    from telemetry.exporters.thresholds import MARKET_CHECKS, required_checks

    dimensions = {
        "localisation": {"dub_sync", "line_overrun", "speech_rate",
                         "semantic_fidelity"},
        "audio_delivery": {"loudness", "true_peak"},
        "timed_text": {"subtitle_rate"},
        "technical": {"tech_video_height", "tech_frame_rate",
                      "tech_audio_channels", "tech_video_codec"},
        "rights_and_packaging": set(MARKET_CHECKS),
    }
    for market, profile in load_profiles().items():
        owed = set(required_checks(profile))
        for dimension, checks in dimensions.items():
            assert checks <= owed, (
                f"{market} is not judged on {dimension}: missing "
                f"{sorted(checks - owed)}"
            )
