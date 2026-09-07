"""Living inside a ten-calls-a-day quota.

These are not tests about politeness to an API. The free tier allows ten TTS
requests per project per day and a twelve-line scene needs twelve, so the
difference between "retry this" and "stop, you cannot reach tomorrow" is the
difference between a run that makes permanent progress and one that spends the
day's allowance discovering it has none left.
"""

from __future__ import annotations

import pytest

from media.dub.cache import Cache
from media.dub.quota import (
    DailyQuotaExhausted,
    Throttle,
    is_daily_cap,
    is_rate_limit,
    is_transient,
    with_retry,
)

PER_MINUTE = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota "
    "exceeded', 'details': [{'violations': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}], "
    "'retryDelay': '2.7s'}}"
)
PER_DAY = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': "
    "[{'violations': [{'quotaId': 'GenerateRequestsPerDayPerProject"
    "PerModel-FreeTier'}]}]}}"
)
RESET = "httpx.ConnectError: [WinError 10054] An existing connection was forcibly closed"
BAD_REQUEST = "400 INVALID_ARGUMENT. Model tried to generate text"


class Boom(Exception):
    pass


def raiser(*messages: str):
    """A callable that raises the given messages in turn, then succeeds."""
    calls = {"n": 0}

    def call():
        i = calls["n"]
        calls["n"] += 1
        if i < len(messages):
            raise Boom(messages[i])
        return f"ok after {i} failure(s)"

    call.count = calls  # type: ignore[attr-defined]
    return call


# ---------------------------------------------------------------------------
# Telling the failures apart
# ---------------------------------------------------------------------------


def test_a_per_minute_limit_is_not_a_per_day_limit():
    assert is_rate_limit(Boom(PER_MINUTE))
    assert not is_daily_cap(Boom(PER_MINUTE))


def test_a_per_day_limit_is_recognised_as_one():
    assert is_rate_limit(Boom(PER_DAY))
    assert is_daily_cap(Boom(PER_DAY))


def test_a_dropped_connection_is_transient_not_a_refusal():
    assert is_transient(Boom(RESET))
    assert not is_rate_limit(Boom(RESET))


def test_a_bad_request_is_neither():
    assert not is_rate_limit(Boom(BAD_REQUEST))
    assert not is_transient(Boom(BAD_REQUEST))
    assert not is_daily_cap(Boom(BAD_REQUEST))


# ---------------------------------------------------------------------------
# What each failure earns
# ---------------------------------------------------------------------------


def test_a_per_minute_limit_is_waited_out(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("media.dub.quota.time.sleep", slept.append)
    assert with_retry(raiser(PER_MINUTE, PER_MINUTE)) == "ok after 2 failure(s)"
    # obeys the server's own retryDelay rather than an invented curve
    assert slept == pytest.approx([3.2, 3.2])


def test_a_dropped_connection_is_retried_quickly(monkeypatch):
    """Failing a whole scene on one reset socket wastes the quota already
    spent on the lines that succeeded."""
    slept: list[float] = []
    monkeypatch.setattr("media.dub.quota.time.sleep", slept.append)
    assert with_retry(raiser(RESET)) == "ok after 1 failure(s)"
    assert slept == [2.0]


def test_a_daily_cap_stops_immediately(monkeypatch):
    """Sleeping cannot reach tomorrow. A job that backs off for an hour
    against a daily cap looks like a hang."""
    slept: list[float] = []
    monkeypatch.setattr("media.dub.quota.time.sleep", slept.append)
    call = raiser(PER_DAY)
    with pytest.raises(DailyQuotaExhausted, match="per-day quota is spent"):
        with_retry(call)
    assert slept == []
    assert call.count["n"] == 1


def test_a_bad_request_is_not_retried():
    """Repeating it changes nothing except how long it takes to find out."""
    call = raiser(BAD_REQUEST)
    with pytest.raises(Boom):
        with_retry(call)
    assert call.count["n"] == 1


def test_giving_up_says_what_it_gave_up_on(monkeypatch):
    monkeypatch.setattr("media.dub.quota.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="tts.*still failing after 3"):
        with_retry(raiser(*[PER_MINUTE] * 5), attempts=3, label="tts")


# ---------------------------------------------------------------------------
# Spacing calls rather than crashing into the limit
# ---------------------------------------------------------------------------


def test_the_first_call_is_not_delayed(monkeypatch):
    monkeypatch.setattr("media.dub.quota.time.sleep", lambda _: None)
    assert Throttle(rpm=3).wait() == 0.0


def test_three_per_minute_means_twenty_seconds_apart():
    assert Throttle(rpm=3).interval_s == pytest.approx(20.0)
    assert Throttle(rpm=10).interval_s == pytest.approx(6.0)


def test_a_zero_budget_does_not_divide_by_zero():
    assert Throttle(rpm=0).interval_s == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# The cache, which is what makes ten calls a day survivable
# ---------------------------------------------------------------------------


def test_identical_requests_hit(tmp_path):
    c = Cache(tmp_path)
    c.put_bytes(b"audio", "tts-model", "Kore", "Danke.")
    assert c.get_bytes("tts-model", "Kore", "Danke.") == b"audio"
    assert c.hits == 1


def test_a_line_that_changed_by_one_word_is_a_different_line(tmp_path):
    c = Cache(tmp_path)
    c.put_bytes(b"audio", "m", "Kore", "Diese Klinge hat eine dunkle Geschichte.")
    assert c.get_bytes("m", "Kore", "Diese Klinge hat eine dunkle Vergangenheit.") is None


def test_the_voice_is_part_of_the_key(tmp_path):
    """A repair must not change the performer mid-scene, and a cache that
    ignored the voice would let it."""
    c = Cache(tmp_path)
    c.put_bytes(b"kore", "m", "Kore", "Danke.")
    assert c.get_bytes("m", "Aoede", "Danke.") is None


def test_key_parts_cannot_collide_by_moving_the_boundary(tmp_path):
    """('ab', 'c') and ('a', 'bc') must not be the same key."""
    c = Cache(tmp_path)
    assert c.key("ab", "c") != c.key("a", "bc")


def test_round_trips_unicode(tmp_path):
    c = Cache(tmp_path, suffix=".json")
    c.put_json({"text": "Du hast Glück, dass dein Blut noch fließt."}, "k")
    assert c.get_json("k")["text"].endswith("fließt.")


def test_a_corrupt_entry_is_a_miss_not_a_crash(tmp_path):
    """It cost one call to make and costs one to replace. Crashing instead
    would cost the rest of the day's quota."""
    c = Cache(tmp_path, suffix=".json")
    (tmp_path / f"{Cache.key('k')}.json").write_bytes(b"{not json")
    assert c.get_json("k") is None
