"""The contracts the agents pass between each other.

These types are the reason this is a multi-agent system rather than five
prompts in a trench coat. Each one is a hard boundary that a model's output has
to survive, and each one rejects a specific way an LLM pipeline quietly goes
wrong:

  Evidence          a claim with no re-runnable query is not a claim
  Finding           a diagnosis must cite evidence, and cite it non-emptily
  ImpactSet         blast radius must name the span that proves each link
  RepairIntent      an action must predict its own effect before it acts
  VerificationResult  succeeding and being right are scored separately

The last two carry the most weight, so they are worth stating plainly.

**A repair must be falsifiable before it runs.** `RepairIntent` cannot be
constructed without a `Prediction`: which series, for which scene and market,
moving in which direction, past which value. An agent that cannot say what
should happen does not get to make it happen. Verification then checks that
exact statement rather than asking a model whether things look better.

**Being right and getting lucky are different.** `VerificationResult` reports
`passed` (the market's requirement is now met) and `prediction_held` (the
number moved where the agent said it would) as separate facts. A repair can
pass by accident -- a concurrent re-render, a threshold change, a probe that
was measuring the wrong file. Only `prediction_held` feeds the autonomy ledger,
so an agent earns trust for understanding the system, not for being present
when it happened to recover.

Nothing here talks to a model, a datasource, or the network. That is deliberate:
these are the shapes, and they are unit-testable without any of it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

EvidenceKind = Literal["metric", "trace", "log", "probe", "profile", "asset"]

# Where a piece of evidence can come from, and what a reader needs in order to
# reproduce it themselves. The point of `query` is that a judge, a reviewer or
# an on-call engineer can paste it into Grafana and get the same number back.
_REPRODUCIBLE: dict[str, str] = {
    "metric": "PromQL, runnable against the Prometheus datasource",
    "trace": "TraceQL, runnable against the Tempo datasource",
    "log": "LogQL, runnable against the Loki datasource",
    "probe": "the exact probe invocation, e.g. sync_offset(ref, dub)",
    "profile": "dotted path into assets/market_profiles.json",
    "asset": "content-addressed asset id and sha256",
}


class UnsupportedClaim(ValueError):
    """Raised when something asserts a fact without evidence for it.

    This is deliberately an exception rather than a validation warning. An
    unsupported claim that flows onward becomes a repair decision, and by then
    nobody can tell which numbers were measured and which were narrated.
    """


@dataclass(frozen=True)
class Evidence:
    """One re-runnable observation.

    Frozen because evidence is a record of what was true at a moment. If a
    later step wants a fresher number it re-runs the query and produces new
    evidence; it does not edit the old one.
    """

    kind: EvidenceKind
    query: str
    value: Any
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = ""          # datasource uid, or the probe's method string
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _REPRODUCIBLE:
            raise UnsupportedClaim(
                f"unknown evidence kind {self.kind!r}; "
                f"known: {sorted(_REPRODUCIBLE)}"
            )
        if not self.query or not self.query.strip():
            raise UnsupportedClaim(
                f"{self.kind} evidence needs a query -- "
                f"{_REPRODUCIBLE[self.kind]}. A number nobody can re-fetch is "
                f"an assertion, not evidence."
            )

    def cite(self) -> str:
        """One line a human can read in the UI or a trace attribute."""
        return f"[{self.kind}] {self.query} = {self.value}"


def _require_evidence(evidence: list[Evidence], what: str) -> None:
    if not evidence:
        raise UnsupportedClaim(
            f"{what} carries no evidence. Every claim this system makes has to "
            f"be traceable to something re-runnable in Grafana."
        )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A diagnosis: what is wrong, and why we believe it."""

    claim: str
    evidence: list[Evidence]
    confidence: float = 1.0
    # Free-form, but by convention the metric or asset the finding is about, so
    # the UI can group findings against the thing they concern.
    subject: str = ""

    def __post_init__(self) -> None:
        _require_evidence(self.evidence, f"finding {self.claim!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AffectedAsset:
    """One asset inside a blast radius, with the span that proves it is in.

    `via_span_id` is not decoration. Blast radius is computed by searching
    Tempo for spans carrying the changed parent's hash, so every membership
    claim has a span behind it, and the UI can deep-link straight to it.
    """

    asset_id: str
    sha256: str
    kind: str
    market: str | None
    via_span_id: str
    trace_id: str


@dataclass
class ImpactSet:
    """Everything downstream of a change, and how it was determined."""

    root_asset_id: str
    root_sha256: str
    affected: list[AffectedAsset]
    evidence: list[Evidence]
    # Assets we determined were NOT affected. Recorded because "we regenerated
    # 4 of 27 assets" is the claim that makes the system valuable, and it is
    # only credible if the denominator is stated.
    considered: int = 0

    def __post_init__(self) -> None:
        _require_evidence(self.evidence, "impact set")
        if self.considered < len(self.affected):
            raise ValueError(
                "considered must include the affected assets; "
                f"got considered={self.considered} < affected="
                f"{len(self.affected)}"
            )

    @property
    def preserved(self) -> int:
        return self.considered - len(self.affected)

    def markets(self) -> list[str]:
        return sorted({a.market for a in self.affected if a.market})


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


class Strategy(str, Enum):
    """Repair strategies, ordered by how much of the asset they disturb.

    The Conductor prefers the least invasive strategy that its evidence
    supports, and escalates only when verification refutes the prediction.
    """

    RETIME = "RETIME"                  # shift/stretch an existing stem
    REMIX = "REMIX"                    # re-balance levels, no new synthesis
    RESYNTHESISE = "RESYNTHESISE"      # new TTS from the same adapted text
    REWRITE = "REWRITE"                # new adapted text, then new TTS
    RECONFORM_SUBS = "RECONFORM_SUBS"  # re-time or re-split subtitle cues


class Direction(str, Enum):
    DECREASE = "decrease"
    INCREASE = "increase"


class AutonomyTier(int, Enum):
    """How far the system may act on its own for a given repair.

    The tier is not chosen by a model. It is derived from measured historical
    outcomes for that strategy in that market -- see `earned_tier` -- so
    autonomy is something a strategy accumulates by being right, and loses by
    being wrong.
    """

    OBSERVE = 0            # record only
    AUTO_FIX = 1           # act, no verification gate
    AUTO_FIX_VERIFY = 2    # act, then must verify
    RECOMMEND = 3          # propose, human clicks
    REQUIRE_APPROVAL = 4   # propose, human must approve before any write
    BLOCK = 5              # do not attempt


@dataclass(frozen=True)
class Prediction:
    """What the agent says will happen, stated so it can be proven wrong.

    Deliberately mechanical -- a series, a direction, a threshold to cross.
    "The sync should look better" is not a prediction, and this type cannot
    express it.
    """

    series: str
    market: str
    scene: str
    direction: Direction
    target_value: float
    # The value at the moment of prediction, so `prediction_held` can be
    # evaluated without re-deriving what "before" meant.
    baseline: float

    def __post_init__(self) -> None:
        if self.direction is Direction.DECREASE and self.target_value >= self.baseline:
            raise ValueError(
                f"predicting a decrease to {self.target_value} from a baseline "
                f"of {self.baseline} is not a decrease"
            )
        if self.direction is Direction.INCREASE and self.target_value <= self.baseline:
            raise ValueError(
                f"predicting an increase to {self.target_value} from a baseline "
                f"of {self.baseline} is not an increase"
            )

    def holds_for(self, observed: float) -> bool:
        if self.direction is Direction.DECREASE:
            return observed <= self.target_value
        return observed >= self.target_value

    def describe(self) -> str:
        op = "<=" if self.direction is Direction.DECREASE else ">="
        return (
            f"{self.series}{{market={self.market},scene={self.scene}}}: "
            f"{self.baseline:g} -> {op} {self.target_value:g}"
        )


@dataclass
class RepairIntent:
    """A proposed action, its justification, and its falsifiable prediction."""

    strategy: Strategy
    target_asset_id: str
    params: dict[str, Any]
    prediction: Prediction
    justification: list[Evidence]
    tier: AutonomyTier
    # Set when this intent replaces one that was tried and refuted, so the
    # trace shows a chain of reasoning rather than an unexplained second go.
    supersedes: str | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        _require_evidence(
            self.justification, f"repair intent {self.strategy.value}"
        )

    @property
    def autonomous(self) -> bool:
        return self.tier in (AutonomyTier.AUTO_FIX, AutonomyTier.AUTO_FIX_VERIFY)

    @property
    def needs_human(self) -> bool:
        return self.tier in (AutonomyTier.RECOMMEND, AutonomyTier.REQUIRE_APPROVAL)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """Did it work, and were we right about why?

    `passed` and `prediction_held` are separate on purpose. A repair can pass
    for reasons the agent did not anticipate -- a concurrent re-render, an
    edited threshold, a probe that was reading a stale file. Counting that as
    earned competence is how an agent talks itself into autonomy it has not
    demonstrated.
    """

    intent: RepairIntent
    observed: float
    passed: bool
    evidence: list[Evidence]

    def __post_init__(self) -> None:
        _require_evidence(self.evidence, "verification result")

    @property
    def prediction_held(self) -> bool:
        return self.intent.prediction.holds_for(self.observed)

    @property
    def outcome(self) -> str:
        """The `outcome` label on `continuity_repairs_total`.

        `lucky` is its own bucket rather than being folded into `succeeded`:
        the market shipped, but the agent did not understand why, and the
        autonomy ledger has to be able to tell the difference.
        """
        if self.passed and self.prediction_held:
            return "succeeded"
        if self.passed:
            return "lucky"
        return "failed"

    @property
    def delta(self) -> float:
        return self.observed - self.intent.prediction.baseline

    def summary(self) -> str:
        return (
            f"{self.intent.strategy.value} -> {self.outcome}: "
            f"{self.intent.prediction.series} "
            f"{self.intent.prediction.baseline:g} -> {self.observed:g} "
            f"(predicted past {self.intent.prediction.target_value:g})"
        )


# ---------------------------------------------------------------------------
# Earned autonomy
# ---------------------------------------------------------------------------

# How many prior attempts before a strategy's record means anything. Below
# this, a strategy that succeeded once would read as 100% reliable.
MIN_HISTORY = 3


def earned_tier(
    *,
    succeeded: int,
    lucky: int,
    failed: int,
    ceiling: AutonomyTier = AutonomyTier.AUTO_FIX_VERIFY,
) -> AutonomyTier:
    """Derive an autonomy tier from measured history.

    The inputs come from `continuity_repairs_total` in Prometheus -- read out
    of Grafana at decision time, never held in agent memory. That matters: the
    ledger is external, append-only and visible, so autonomy cannot be argued
    upward inside a prompt, and a judge can watch a strategy lose privileges
    live by making it fail twice.

    `lucky` outcomes count toward experience but not toward competence: they
    raise the denominator and not the numerator, so a strategy that keeps
    getting away with it drifts down the ladder rather than up.
    """
    attempts = succeeded + lucky + failed
    if attempts < MIN_HISTORY:
        # Not enough history to trust it alone, but not a reason to refuse:
        # propose and let a human decide.
        tier = AutonomyTier.RECOMMEND
    else:
        rate = succeeded / attempts
        # AUTO_FIX_VERIFY is the most this system ever grants. AUTO_FIX exists
        # in the ladder for completeness but is deliberately unreachable by
        # earning: a repair that skips verification cannot produce the outcome
        # that would justify skipping verification, so a strategy could reach
        # a perfect record having proven nothing.
        if rate >= 0.6:
            tier = AutonomyTier.AUTO_FIX_VERIFY
        elif rate >= 0.3:
            tier = AutonomyTier.RECOMMEND
        else:
            tier = AutonomyTier.REQUIRE_APPROVAL
    # Tiers ascend as autonomy shrinks, so the more restrictive of (earned,
    # ceiling) is the larger value. A market with rights or legal sensitivity
    # can therefore cap autonomy regardless of how good the record is, and no
    # amount of history argues past it.
    return AutonomyTier(max(tier.value, ceiling.value))


def confidence_from(values: list[float], target: float) -> float:
    """A crude, honest confidence for a Finding built from repeated samples.

    Spread-aware rather than model-asserted: tight agreement far from the
    threshold is confident, scattered samples straddling it are not. Returned
    so a model never has to invent a number it cannot justify.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        # One sample says nothing about spread. Report the side it falls on,
        # at the confidence a single observation actually warrants, rather
        # than the 1.0 a model would happily assert.
        return 0.5
    mean = statistics.fmean(values)
    spread = statistics.pstdev(values)
    if spread == 0:
        return 1.0 if mean > target else 0.0
    z = abs(mean - target) / spread
    return round(min(1.0, z / 3.0), 3)
