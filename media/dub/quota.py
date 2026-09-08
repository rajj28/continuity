"""Living inside the free tier's rate limits.

Two limits, measured against the live API on 7 September 2026, and they demand
completely different responses:

    3 requests per minute per model    worth sleeping through
    10 requests per DAY per project    not worth sleeping through

The second is the one that shapes the design. A twelve-line scene needs twelve
TTS calls and the day allows ten, so a run cannot assume it will finish, and
the only thing that makes progress compound is caching every successful call
(see cache.py). Treating a daily cap as something to back off from would turn
a clear "resume tomorrow, or on a billed project" into an hour of silence that
looks exactly like a hang.

Three mechanisms, and the distinctions between them matter:

`Throttle` is proactive. It spaces requests so the limit is not hit in the
first place. Without it every run burns its first three calls instantly and
then spends the rest of its life in backoff, which is slower overall and much
harder to reason about.

`with_retry` is reactive, for when the estimate is wrong anyway -- a shared
project, a concurrent run, a quota that resets on a different boundary than we
assumed. It obeys the server's own `retryDelay` rather than inventing an
exponential curve, because the server knows when the window opens and we do
not.

`is_daily_cap` is the stop condition. It raises rather than waits, because
sleeping cannot reach tomorrow.

A 429 or a dropped connection is retried. A 400 or a 403 is not: those mean the
request was wrong or the credential was, and repeating them changes nothing
except how long it takes to find out.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

log = logging.getLogger("continuity.quota")

T = TypeVar("T")


class DailyQuotaExhausted(RuntimeError):
    """The free tier's per-day allowance is spent.

    Its own type because the correct response is different from every other
    failure: stop, keep what was cached, and say plainly that the run resumes
    tomorrow or on a billed project. Retrying is not it.
    """

# Free-tier requests per minute, per model, measured against the live API on
# 7 September 2026. The TTS number came from the 429 itself, which names its
# own quotaValue.
FREE_TIER_RPM: dict[str, int] = {
    "gemini-2.5-flash-preview-tts": 3,
    "gemini-3.1-flash-tts-preview": 3,
    "gemini-2.5-flash": 10,
    "gemini-3.5-flash": 10,
}
DEFAULT_RPM = 10

_RETRY_DELAY = re.compile(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'")


def rpm_for(model: str) -> int:
    return FREE_TIER_RPM.get(model, DEFAULT_RPM)


@dataclass
class Throttle:
    """Spaces calls to stay under a requests-per-minute budget.

    Deliberately simple: one timestamp, one sleep. A token bucket would let a
    burst through and then stall, which for a job that is going to take four
    minutes anyway buys nothing and makes the log harder to read.
    """

    rpm: int
    _last: float = field(default=0.0, repr=False)

    @property
    def interval_s(self) -> float:
        return 60.0 / max(self.rpm, 1)

    def wait(self) -> float:
        """Sleep until the next call is allowed. Returns how long it waited."""
        now = time.monotonic()
        earliest = self._last + self.interval_s
        slept = 0.0
        if self._last and now < earliest:
            slept = earliest - now
            time.sleep(slept)
        self._last = time.monotonic()
        return slept


def _retry_after(exc: Exception, default: float) -> float:
    """The server's own retryDelay, when it gave one."""
    match = _RETRY_DELAY.search(str(exc))
    if match:
        # A small cushion: retrying at exactly the boundary races the window.
        return float(match.group(1)) + 0.5
    return default


def is_rate_limit(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def is_daily_cap(exc: Exception) -> bool:
    """A per-DAY quota, which no amount of waiting inside one run will clear.

    Worth separating from a per-minute limit because the responses differ
    completely: a minute is worth sleeping through, a day is not, and a job
    that backs off for an hour against a daily cap looks like a hang.
    """
    return "PerDay" in str(exc)


def is_transient(exc: Exception) -> bool:
    """A network failure rather than a refusal.

    Long jobs on a metered API sit on one TLS connection for minutes at a
    time, and connections get reset. Failing the whole scene on one dropped
    socket wastes the quota already spent on the lines that succeeded.
    """
    text = str(exc)
    return any(marker in text for marker in (
        "ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError",
        "forcibly closed", "10054", "Connection reset", "ServerError",
        "500 ", "502 ", "503 ", "504 ",
    ))


def with_retry(
    call: Callable[[], T],
    *,
    attempts: int = 5,
    base_delay: float = 20.0,
    label: str = "request",
) -> T:
    """Run `call`, waiting out rate limits and transient network failures.

    A malformed request or a bad credential is raised immediately, because
    repeating it changes nothing except how long it takes to discover. A daily
    cap is raised immediately too: sleeping cannot reach tomorrow, and a job
    that backs off for an hour against one looks like a hang.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # the SDK raises a wide family of API errors
            if is_daily_cap(exc):
                raise DailyQuotaExhausted(
                    f"{label}: the free tier's per-day quota is spent. "
                    f"Cached work is kept, so a re-run resumes rather than "
                    f"restarting. {str(exc)[:200]}"
                ) from exc
            if not (is_rate_limit(exc) or is_transient(exc)):
                raise
            last = exc
            if attempt == attempts:
                break
            if is_transient(exc):
                delay = min(2.0 * attempt, 10.0)
                log.info("transport failure on %s (attempt %d/%d); retrying "
                         "in %.1fs", label, attempt, attempts, delay)
            else:
                delay = _retry_after(exc, base_delay)
                log.info("rate limited on %s (attempt %d/%d); waiting %.1fs",
                         label, attempt, attempts, delay)
            time.sleep(delay)
    raise RuntimeError(
        f"{label} still failing after {attempts} attempts: {last}"
    ) from last


class Budget:
    """One throttle per model, shared across a run."""

    def __init__(self) -> None:
        self._throttles: dict[str, Throttle] = {}

    def throttle(self, model: str) -> Throttle:
        if model not in self._throttles:
            self._throttles[model] = Throttle(rpm_for(model))
        return self._throttles[model]

    def call(self, model: str, fn: Callable[[], T], *, label: str = "") -> T:
        throttle = self.throttle(model)
        waited = throttle.wait()
        if waited > 0.5:
            log.debug("throttled %s for %.1fs", model, waited)
        return with_retry(fn, label=label or model)


# One budget for the process. The limits are per project, not per object, so a
# module-level instance is the honest representation of what is being shared.
BUDGET = Budget()


def call(model: str, fn: Callable[[], Any], *, label: str = "") -> Any:
    return BUDGET.call(model, fn, label=label)
