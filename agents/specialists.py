"""Who investigates what, and what each of them is allowed to hold.

One agent reasoning about lip-sync, EBU R128 loudness, subtitle reading rates,
territory rights windows and package completeness needs a thirty-line briefing
and is mediocre at all five. Split by dimension and each gets a short, sharp
instruction and a small tool surface -- which is both better reasoning and, more
importantly, a place to put authority.

## Authority is the tool list, not the prompt

The Compliance specialist cannot propose a repair. Not because it is instructed
not to: because `propose_repair` is not among the tools it is given, so the
function is absent from the protocol it speaks. A model cannot call a tool it
was never handed, and no amount of clever prompting reaches one.

That is the same guarantee as `mcp-grafana --disable-write`, applied one level
up. A right that was never cleared is not a defect in a file, and the agent
looking at it has no mechanism for pretending otherwise. It can read, it can
correlate, and it can escalate with the facts a human needs to act -- which is
the entire correct response.

The same idea narrows the others: the Audio specialist may propose REMIX and
nothing else, so a model that decides the loudness problem is really a timing
problem cannot quietly reach for RETIME. It has to say so and hand back.

## Truthfulness of the strategy lists

Each specialist's `strategies` is what the executor can actually carry out for
it, not what would be nice. REWRITE is deliberately absent everywhere: it
re-synthesises through the dub pipeline rather than transforming an existing
asset, and `agents.repair.apply` refuses it. Offering it would produce a
proposal that validates, gets approved by a human, and then dies at the moment
of acting -- which is the worst place to discover a capability does not exist.
Progressive drift therefore escalates, and says why.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.contracts import Strategy

# Every tool the Conductor knows how to execute. A specialist is a subset.
ALL_TOOLS = (
    "list_failing_checks",
    "query_metric",
    "get_threshold",
    "repair_history",
    "metric_history",
    "blast_radius",
    "compare_markets",
    "propose_repair",
    "escalate",
)

# The tools any investigator needs to establish what is true. None of them
# changes anything, which is why every specialist gets all of them.
READ_TOOLS = (
    "list_failing_checks",
    "query_metric",
    "get_threshold",
    "repair_history",
    # The three that make an agent good at pulling information rather than
    # merely able to. `metric_history` separates a fresh regression from a
    # standing condition; `blast_radius` says what a fix would break;
    # `compare_markets` is how a human specialist actually reasons -- "we saw
    # this in France last week". None of them changes anything, so every
    # specialist gets all three, including the ones that cannot repair.
    "metric_history",
    "blast_radius",
    "compare_markets",
)


@dataclass(frozen=True)
class Specialist:
    """One agent's remit, tool surface and authority."""

    name: str
    owns: frozenset[str]
    tools: tuple[str, ...]
    strategies: frozenset[Strategy]
    brief: str

    @property
    def may_repair(self) -> bool:
        return "propose_repair" in self.tools

    def handles(self, check: str) -> bool:
        return check in self.owns


LOCALISATION = Specialist(
    name="localisation",
    owns=frozenset({"dub_sync", "line_overrun", "speech_rate"}),
    tools=ALL_TOOLS,
    strategies=frozenset({Strategy.RETIME}),
    brief=(
        "You are the localisation specialist. You judge whether the dubbed "
        "dialogue lands where the picture says it should.\n"
        "\n"
        "The distinction that decides everything: drift that is UNIFORM across "
        "every line means the whole stem is offset, and shifting it fixes all "
        "of it. Drift that GROWS down the scene means lines are overrunning "
        "their slots and pushing the next one late -- shifting the stem moves "
        "the whole staircase and fixes nothing. `dub_drift_systematic` tells "
        "you which you are looking at.\n"
        "\n"
        "`dub_sync_offset_ms` is a MAGNITUDE. It says how far out the dub is "
        "and never which way. `dub_sync_signed_ms` is the direction: positive "
        "means the dub arrives late. A RETIME shift is the negation of the "
        "signed value. Getting this backwards makes the fault twice as bad, "
        "and has.\n"
        "\n"
        "You may propose RETIME. You may not re-write and re-synthesise "
        "dialogue -- that capability does not exist here -- so if the lines "
        "are simply too long, escalate and say so."
    ),
)

AUDIO = Specialist(
    name="audio",
    owns=frozenset({"loudness", "true_peak"}),
    tools=ALL_TOOLS,
    strategies=frozenset({Strategy.REMIX}),
    brief=(
        "You are the audio delivery specialist. You judge whether the stem "
        "meets the market's loudness specification.\n"
        "\n"
        "Loudness is a two-sided BAND, not a ceiling: too quiet fails delivery "
        "exactly as surely as too loud, so read both "
        "`loudness_target_lufs` and `loudness_tolerance_lu` before deciding "
        "anything. True peak is a hard ceiling.\n"
        "\n"
        "You may propose REMIX, which normalises to the target with a "
        "true-peak limit and does not touch timing. If the problem is timing, "
        "it is not yours -- say so and hand it back."
    ),
)

ACCESSIBILITY = Specialist(
    name="accessibility",
    owns=frozenset({"ad_collision"}),
    tools=READ_TOOLS + ("escalate",),
    strategies=frozenset(),
    brief=(
        "You are the accessibility specialist. You judge whether the audio "
        "description stays out of the dialogue.\n"
        "\n"
        "`ad_collision_ms` is narration overlapping speech and its tolerance "
        "is zero, because there is no amount of talking over the dialogue that "
        "is acceptable. `ad_coverage_ratio` is how much of the describable "
        "silence was actually used -- a track can collide with nothing by "
        "describing almost nothing, and that is a different failure.\n"
        "\n"
        "Rebuilding a description means re-writing narration and re-"
        "synthesising it, which is a pipeline stage and not a repair you can "
        "propose. Establish precisely which cue collides and by how much, then "
        "escalate with that."
    ),
)

PACKAGING = Specialist(
    name="packaging",
    # `coverage` belongs here too. It is not a dimension -- it is the report
    # that some check the market owes was never measured at all -- and absence
    # is precisely this specialist's subject. Left unowned it fell to the
    # roster's catch-all, which was correct behaviour that read like a bug.
    owns=frozenset({"deliverables_complete", "metadata_localised",
                    "forced_narrative", "subtitle_rate", "coverage"}),
    tools=READ_TOOLS + ("escalate",),
    strategies=frozenset(),
    brief=(
        "You are the packaging specialist. You judge whether what the market "
        "is owed actually exists.\n"
        "\n"
        "This is the dimension where absence, not failure, is the usual "
        "problem: a deliverable nobody authored produces no measurement to "
        "look at. A missing deliverable IS buildable -- unlike a missing "
        "right -- so the correct response is to name exactly what is absent "
        "and which stage produces it, then escalate so it gets built. Do not "
        "report a market as unfixable when the answer is that nobody has run "
        "the stage yet.\n"
        "\n"
        "A failing `coverage` check is the same thing said differently: the "
        "market owes more checks than have been measured. Say which ones are "
        "absent, not merely that some are."
    ),
)

COMPLIANCE = Specialist(
    name="compliance",
    owns=frozenset({"rights_cleared", "certified", "tech_audio_channels"}),
    # No `propose_repair`. This is the whole point: a right that was never
    # cleared is not a defect in a file, and this agent has no mechanism for
    # pretending it is. It reads, it correlates, it escalates.
    tools=READ_TOOLS + ("escalate",),
    strategies=frozenset(),
    brief=(
        "You are the compliance specialist. You judge whether this title may "
        "legally ship to this territory.\n"
        "\n"
        "Nothing here is a defect in a file and nothing here is repairable by "
        "processing. A music cue that was never licensed for Japan is not "
        "fixed by re-encoding; a certificate that expired is not fixed by "
        "remixing; a stereo master where the platform demands 5.1 is a "
        "different master, not a filter.\n"
        "\n"
        "You have no repair tool, deliberately. Your job is to make the "
        "escalation actionable: which grant or certificate is missing, which "
        "body issues it, what the dates are, and what a human has to do next. "
        "A vague escalation wastes the one thing you are for."
    ),
)

ROSTER: tuple[Specialist, ...] = (
    LOCALISATION, AUDIO, ACCESSIBILITY, PACKAGING, COMPLIANCE,
)

BY_NAME = {s.name: s for s in ROSTER}


def for_check(check: str) -> Specialist | None:
    """Whose dimension is this failure in?"""
    for specialist in ROSTER:
        if specialist.handles(check):
            return specialist
    return None


def dispatch_for(failing: list[str]) -> list[tuple[Specialist, list[str]]]:
    """Which specialists to wake, and what each of them is being asked about.

    Only the ones whose dimension is actually failing. A market with clean
    rights does not wake the compliance specialist, which is both the obvious
    saving and the reason a five-agent roster does not cost five agent runs
    per incident.

    Roster order is preserved so a run is reproducible, and unowned checks are
    reported rather than dropped -- a failing requirement nobody owns is a hole
    in the roster and should look like one.
    """
    grouped: dict[str, list[str]] = {}
    unowned: list[str] = []
    for check in failing:
        specialist = for_check(check)
        if specialist is None:
            unowned.append(check)
            continue
        grouped.setdefault(specialist.name, []).append(check)

    work = [(s, sorted(grouped[s.name])) for s in ROSTER if s.name in grouped]
    if unowned:
        # Surfaced through the roster rather than silently ignored. A check
        # with no specialist cannot be investigated by anyone, and a system
        # that quietly skipped it would report a market as fully investigated
        # while nobody had looked at one of its failures.
        work.append((_UNOWNED, sorted(unowned)))
    return work


# A placeholder specialist for checks nobody owns, so the caller gets a name
# to report rather than a silent omission. It can do nothing but escalate,
# which is the truthful capability for a failure with no owner.
_UNOWNED = Specialist(
    name="unassigned",
    owns=frozenset(),
    tools=READ_TOOLS + ("escalate",),
    strategies=frozenset(),
    brief=(
        "You are looking at a failing requirement that no specialist owns. "
        "That is a gap in this system's roster, not a fact about the title. "
        "Establish what is failing and escalate saying which dimension is "
        "unassigned."
    ),
)
