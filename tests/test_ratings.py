"""Certification, which is a permission rather than a quality bar.

Every case here is a state a real title passes through, and the reason each
gets its own test is that they demand different responses from an operator: one
needs a submission started weeks ago, one needs patience, one needs a
resubmission because the cut moved, and one is simply a no.
"""

from __future__ import annotations

from datetime import date

import pytest

from media.ratings import (
    Certificate,
    certification,
)

CUT = "a" * 64
OTHER_CUT = "b" * 64
WHEN = date(2026, 9, 9)


def register(*certificates: Certificate) -> dict[str, Certificate]:
    return {c.market: c for c in certificates}


def granted(**kw) -> Certificate:
    base = dict(market="de-DE", body="FSK", state="granted", rating="FSK 12",
                granted_on="2026-06-18", master_sha256=CUT)
    base.update(kw)
    return Certificate(**base)


def check(certificate: Certificate | None, market="de-DE", body="FSK"):
    return certification(
        market, body, CUT, on=WHEN,
        register=register(certificate) if certificate else {},
    )


# ---------------------------------------------------------------------------
# The states, and what each one tells an operator to do
# ---------------------------------------------------------------------------


def test_a_current_certificate_for_this_cut_lets_the_market_ship():
    result = check(granted())
    assert result.valid
    assert result.rating == "FSK 12"


def test_nothing_submitted_says_a_human_has_to_start_it():
    """The one worth alerting on early: certification lead times run to weeks,
    so discovering it at delivery is discovering it too late."""
    result = check(None, market="ja-JP", body="EIRIN")
    assert not result.valid
    assert "no submission to EIRIN" in result.reason
    assert "weeks" in result.reason


def test_a_pending_submission_carries_the_date_to_plan_around():
    result = check(granted(state="pending", rating="", master_sha256="",
                           expected_by="2026-09-20"))
    assert not result.valid
    assert "2026-09-20" in result.reason
    assert result.detail["expected_by"] == "2026-09-20"


def test_a_refusal_is_reported_as_a_refusal():
    """Distinct from every other state: no amount of waiting resolves it."""
    result = check(granted(state="refused", rating="",
                           note="cut 14 requires trims"))
    assert not result.valid
    assert "refused" in result.reason
    assert "trims" in result.reason


def test_an_expired_certificate_is_not_a_certificate():
    result = check(granted(expires_on="2026-08-31"))
    assert not result.valid
    assert "expired 2026-08-31" in result.reason


def test_an_exemption_is_valid_and_not_a_gap():
    """Some territories do not require certification for some channels.
    Treating that as missing would block a release never obliged to have one."""
    result = check(granted(state="exempt", rating="", master_sha256=""))
    assert result.valid
    assert "exempt" in result.reason


# ---------------------------------------------------------------------------
# The hash, which is why this lives next to a content-addressed store
# ---------------------------------------------------------------------------


def test_a_certificate_for_a_different_cut_does_not_cover_this_one():
    """A certificate describes a specific runtime and content. Re-cutting
    invalidates it, and shipping the new cut under the old certificate is a
    regulatory matter rather than a QC failure."""
    result = certification("de-DE", "FSK", OTHER_CUT, on=WHEN,
                           register=register(granted()))
    assert not result.valid
    assert "certified a different cut" in result.reason
    assert result.detail["certified_sha256"] == CUT
    assert result.detail["current_sha256"] == OTHER_CUT


def test_a_certificate_with_no_recorded_cut_is_not_second_guessed():
    """Older records may predate hashing. Refusing them would block markets
    over bookkeeping rather than over content."""
    result = certification("de-DE", "FSK", OTHER_CUT, on=WHEN,
                           register=register(granted(master_sha256="")))
    assert result.valid


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_certification_is_never_repairable_by_an_agent():
    """A certificate is a permission granted by a body outside this system.
    An agent that 'repaired' a missing one would be forging it."""
    for state in ("granted", "pending", "refused", "not_submitted"):
        result = check(granted(state=state, rating="", master_sha256=""))
        assert result.repairable is False


def test_the_real_register_is_loadable_and_covers_every_market():
    """A market with no entry blocks as unsubmitted, which is correct but
    should be a deliberate choice rather than an oversight."""
    from pathlib import Path

    from media.qc.profiles import load_profiles
    from media.ratings import load_register

    register = load_register(
        Path(__file__).resolve().parents[1] / "assets" / "certificates.json"
    )
    for market, profile in load_profiles().items():
        assert market in register, f"{market} has no certification entry"
        assert register[market].body == profile["ratings_body"], (
            f"{market} is certified by {register[market].body} but its profile "
            f"names {profile['ratings_body']}"
        )


@pytest.mark.parametrize("market,valid", [
    ("fr-FR", True), ("de-DE", True),
    ("pt-BR", False), ("hi-IN", False), ("ja-JP", False),
])
def test_the_real_register_produces_the_states_it_documents(market, valid):
    from pathlib import Path

    from media.qc.profiles import profile
    from media.ratings import load_register
    from media.store import Store

    store = Store(Path(__file__).resolve().parents[1] / "out" / "store")
    try:
        master = store.load("SINTEL:master").sha256
    except FileNotFoundError:
        pytest.skip("no ingested master; run scripts/stage1.py")

    register = load_register(
        Path(__file__).resolve().parents[1] / "assets" / "certificates.json"
    )
    result = certification(market, profile(market)["ratings_body"], master,
                           on=WHEN, register=register)
    assert result.valid is valid, result.reason
