"""Investigation: turning an alert into a diagnosis, with no model involved.

This is the step most agent demos skip. An alert says a market cannot ship; it
does not say why, and "why" is not one question but four:

    Is it still true?          the alert may have fired minutes ago
    What is failing?           which requirements, against which thresholds
    What is unmeasured?        a market can fail with nothing failing
    What moved underneath it?  a parent hash that no longer matches

Every one of those is answerable by measurement, so none of them uses a model.
That is the point of the file. A language model asked "why is de-DE blocked?"
will produce a fluent answer whether or not it has looked, and the answer will
be indistinguishable from one that did. Arithmetic over `Evidence` cannot do
that: if the query returns nothing, the finding does not exist.

The model earns its place later, in adaptation -- rewriting a line so it means
the same thing in fewer syllables is a judgement no probe can make. Measurement
first, judgement second, and never the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.contracts import Evidence, Finding, ImpactSet, UnsupportedClaim
from agents.mcp import McpError
from agents.signal import Signal
from agents.wake import Incident


@dataclass
class Investigation:
    """What was established, and what could not be."""

    incident: Incident
    still_failing: bool
    findings: list[Finding] = field(default_factory=list)
    impact: ImpactSet | None = None
    # Things we tried to establish and could not, kept rather than dropped: an
    # investigation that silently skipped a step looks identical to one where
    # the step came back clean, and the Conductor must be able to tell those
    # apart before it decides how much to trust its own diagnosis.
    gaps: list[str] = field(default_factory=list)

    @property
    def resolved_before_we_arrived(self) -> bool:
        """The market recovered between the alert firing and us looking.

        Not an edge case -- with `for: 2m` on the rule and a repair already in
        flight, it is a normal outcome. Repairing a market that is already
        green is how an autonomous system does damage.
        """
        return not self.still_failing

    @property
    def subjects(self) -> list[str]:
        return sorted({f.subject for f in self.findings if f.subject})

    def evidence(self) -> list[Evidence]:
        trail = [e for f in self.findings for e in f.evidence]
        if self.impact:
            trail += self.impact.evidence
        return trail

    def summary(self) -> str:
        if self.resolved_before_we_arrived:
            return (
                f"{self.incident.market} recovered before investigation; "
                f"nothing to repair"
            )
        parts = [f"{self.incident.market} blocked"]
        if self.subjects:
            parts.append("failing: " + ", ".join(self.subjects))
        if self.impact and self.impact.affected:
            parts.append(
                f"{len(self.impact.affected)} of {self.impact.considered} "
                f"assets affected"
            )
        if self.gaps:
            parts.append(f"{len(self.gaps)} gap(s)")
        return "; ".join(parts)


def investigate(
    signal: Signal, incident: Incident, *, considered: int = 0
) -> Investigation:
    """Establish why a market is blocked, using only measurements.

    `considered` is the size of the asset population, for the blast radius
    denominator. It comes from the caller's store because Tempo cannot know it.
    """
    title, market = incident.title_id, incident.market

    # 1. Is it still true? Ask Grafana again rather than trusting the payload.
    #    The alert is a wake-up, not a fact with a shelf life.
    verdict = signal.verdict(title, market)
    if verdict is None:
        # No verdict at all is not recovery -- it is a market we cannot
        # currently judge, which is strictly worse than one we judged as failing.
        return Investigation(
            incident=incident, still_failing=True,
            gaps=[f"no market_release_ready series for {title}/{market}; "
                  f"the verdict cannot currently be computed"],
        )
    if float(verdict.value) == 1.0:
        return Investigation(incident=incident, still_failing=False)

    investigation = Investigation(incident=incident, still_failing=True)

    # 2. What is failing, and against what bar?
    try:
        investigation.findings.extend(
            signal.failing_requirements(title, market)
        )
    except (McpError, UnsupportedClaim) as exc:
        investigation.gaps.append(f"could not enumerate failing checks: {exc}")

    # 3. What was never measured? A market can be blocked with nothing failing,
    #    and that case reads as healthy on every dashboard unless asked.
    try:
        if gap := signal.missing_coverage(title, market):
            investigation.findings.append(gap)
    except (McpError, UnsupportedClaim) as exc:
        investigation.gaps.append(f"could not check coverage: {exc}")

    # 4. Did something move underneath it? Only worth asking when staleness is
    #    actually reported -- a blast radius query on a healthy lineage is a
    #    Tempo search that returns everything and explains nothing.
    stale = signal.observe(
        f'sum(asset_stale{{title="{title}",market="{market}"}})'
    )
    if stale is not None and float(stale.value) > 0:
        investigation.findings.append(Finding(
            claim=f"{market} has {stale.value:g} asset(s) whose parent moved",
            evidence=[stale],
            subject="staleness",
        ))
        parent = signal.observe(
            f'asset_stale{{title="{title}",market="{market}"}} == 1'
        )
        if parent is None:
            investigation.gaps.append(
                "staleness is reported in aggregate but no individual asset "
                "resolved; blast radius not computed"
            )
    elif stale is None:
        investigation.gaps.append(
            f"no asset_stale series for {title}/{market}; staleness unknown"
        )

    return investigation


def blast_radius_for(
    signal: Signal, parent_sha: str, *, considered: int
) -> tuple[ImpactSet | None, str | None]:
    """Blast radius, with the failure returned rather than raised.

    An investigation that dies because Tempo is in preview and changed a field
    name is worse than one that reports a gap and carries on with the metric
    evidence it already has.
    """
    try:
        return signal.blast_radius(parent_sha, considered=considered), None
    except (McpError, ValueError) as exc:
        return None, f"blast radius unavailable for {parent_sha[:12]}: {exc}"
