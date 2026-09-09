"""The shot list: what each clip shows, and how the camera moves through it.

Kept apart from the recorder so the mechanics and the direction can be read
separately. `scripts/record.py` knows how to drive a browser and turn frames
into video; this knows what is worth pointing it at.

Every clip is framed rather than merely captured. A 1080p frame of the control
room holds far more than a viewer takes in during the seconds they get, so the
camera pushes into whatever is being talked about and pulls back between beats.
Shown whole it is a screenshot of a dashboard; shown one panel at a time it is
an argument.

## Shots are cut long

Each is a few seconds longer than the narration it sits under. Trimming a shot
in the edit costs nothing; discovering the picture runs out before the sentence
does means going back and shooting it again. The first pass was cut to what
looked right on its own and came to 125 seconds against 174 seconds of voice,
which is the same mistake in nine places.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL = "https://continuity-control-z6txmgck2a-el.a.run.app"
# The build shot, and only the build shot, is taken against a local instance.
# Not because the deployed one cannot run a build -- it is the same image and
# the same code -- but because it has nothing to build FROM: a feature master
# is a quarter of a gigabyte, a container is the wrong place for a film, and
# Cloud Run will not accept a request body that size either. Same service, same
# screen, on the machine that has the film on it.
LOCAL = "http://127.0.0.1:8090"
GRAFANA = "https://cordialharbor49.grafana.net"
# MarketNotReleaseReady -- the rule that wakes the agents.
ALERT_UID = "cfxmy5p8nqtq8d"
# A copy, because Chrome locks a profile it is using and the original is the
# one a person logs into.
PROFILE = ROOT / "out" / "chrome-profile-copy"


async def _open_board(take, hash_: str = "") -> None:
    """The board, from the instance that has a film on it.

    Local rather than deployed, for one visible reason: every shot that holds
    the whole page holds the New Release panel with it, and on the deployed
    service that panel correctly says it has nothing to build from. Cutting
    between an empty picker and a full one inside the same minute reads as two
    different systems. The numbers are identical either way -- both read the
    same Grafana -- so the only thing this choice changes is that the film is
    one session instead of two.
    """
    await take.goto(LOCAL + hash_, settle=2)
    await take.wait_for("document.querySelectorAll('#rows .row').length > 0")
    await asyncio.sleep(1.2)


async def _open_dashboard(take, path: str) -> None:
    """A dashboard in kiosk mode, with the onboarding noise dismissed.

    `kiosk` strips Grafana's navigation: the point of these shots is the
    panels, and the menu is not what anyone is being asked to look at.
    """
    await take.goto(GRAFANA + path, settle=11)
    await take.js(
        "(() => { for (const b of document.querySelectorAll('button')) {"
        "  const a = (b.getAttribute('aria-label') || '').toLowerCase();"
        "  if (a.includes('close') || a.includes('dismiss')) b.click();"
        "} })()"
    )
    await asyncio.sleep(1.2)


# ---------------------------------------------------------------------------
# The control room
# ---------------------------------------------------------------------------


async def board(take, token: str):
    """The whole answer, then the way in, then the detail.

    The opening shot, and the only one that has to work for a viewer who has
    been looking at this screen for four seconds. Counts first, because two
    and three is the answer; then the panel a master goes into, because that
    is the sentence the narration is on by then; then the matrix, then one
    row of it.
    """
    await _open_board(take)
    await take.hold(3.0)
    await take.focus(".counts", 1.4, pad=14)
    await take.hold(3.5)
    await take.push(await take.rect_of("#build", 12), 1.6)
    await take.hold(5.0)
    await take.push(await take.rect_of("table.matrix", 8), 1.6)
    await take.hold(5.0)
    await take.focus('#rows .row[data-m="de-DE"]', 1.4, pad=10)
    await take.hold(5.5)
    await take.wide(1.5)
    await take.hold(6.5)


async def release(take, token: str):
    """A master goes in and the pipeline runs, at the pace it actually runs.

    Nothing here is dressed. The rail is the seven stages of the README, the
    lines underneath are what those scripts print, and the seconds against
    each stage are how long it took. `watch` records in real time rather than
    holding a still, because the elapsed time is part of the claim: this is a
    build, not a transition.
    """
    await take.goto(LOCAL, settle=2)
    await take.js("sessionStorage.setItem('op', %r)" % token)
    await take.goto(LOCAL, settle=2)
    await take.wait_for(
        "document.querySelectorAll('#pick-markets input').length > 0")
    await take.js(
        "(() => { for (const i of document.querySelectorAll('#pick-markets input'))"
        "  i.checked = (i.value === 'pt-BR');"
        "  document.getElementById('pick-markets').onchange(); })()")
    await take.push(await take.rect_of("#build", 14), 1.2)
    await take.hold(2.5)
    await take.js("document.getElementById('build-go').click()")
    # Nine seconds of a build that takes minutes. Cut for the film rather than
    # sped up: the stage timings on the rail are the real ones, and a shot
    # that ran the clock faster than the pipeline would be the one dishonest
    # frame in the whole thing.
    await take.watch(9.0)
    await take.hold(1.5)


async def outputs(take, token: str):
    """The files themselves, with transports under them.

    Everything else on this screen is a representation of the work -- a pip, a
    number, a hash. This is the work. A dub you cannot hear is indistinguish-
    able from one that was never made, and the whole project is an argument
    against being asked to take that on trust.
    """
    # Local, like the build shot before it, and for a related reason: the
    # deployed image carries the dub, the described track and the package but
    # not the scene's own audio, which lives in the store's objects/ directory
    # with a quarter of a gigabyte of other media. Playing the original
    # against the dub is the comparison that makes a dub mean anything, so
    # this is taken where all four files exist.
    await take.goto(LOCAL + "/#de-DE", settle=2)
    # Waits on the outputs themselves, and waits a long time. The detail pane
    # is one round trip per figure to Grafana Cloud, and from here that is
    # forty seconds of wide-area latency -- fast from the deployed service,
    # which sits in the same region. Waiting for the button would pass while
    # the panel this shot is about was still a skeleton.
    await take.wait_for(
        "document.querySelectorAll('#detail .out-row').length > 0", timeout=120)
    await take.js(
        "(() => { const h = [...document.querySelectorAll('#detail h2')]"
        "  .find(e => e.textContent.includes('agents made'));"
        "  if (h) h.scrollIntoView({block: 'start'}); })()")
    # Then back up past the sticky header. `scrollIntoView` puts the heading at
    # the top of the viewport and the header sits on top of it, so the first
    # row's label ended up behind the counts -- a transport moving with nothing
    # to say which file it belongs to.
    await take.js("window.scrollBy(0, -72)")
    await asyncio.sleep(1.0)
    await take.push(await take.rect_of_all("#detail .out-row", 22), 1.3)
    await take.hold(10.5)


async def listen(take, token: str):
    """Press play on each transport, at the moment that file is heard.

    The picture for the one block whose soundtrack is the product rather than
    a voice. `scripts/listen.py` builds that soundtrack and writes the offsets
    it used to `out/voice/vo2b.cues.json`; the numbers below are the same
    offsets, so the row that is moving is the row you can hear.

    Recorded with `watch`, at the pace it actually happens, because the whole
    point of the block is that twenty seconds of audio takes twenty seconds.
    """
    import json

    cues = json.loads(
        (ROOT / "out" / "voice" / "vo2b.cues.json").read_text("utf-8"))
    # Where each file starts and how long it runs, derived from the gaps
    # between cues rather than written down twice.
    plays = []
    for index, (_start, ends, _name) in enumerate(cues):
        after = cues[index + 1][0] if index + 1 < len(cues) else None
        plays.append((ends, (after - ends) if after else 6.0))

    await take.goto(LOCAL + "/#de-DE", settle=2)
    await take.wait_for(
        "document.querySelectorAll('#detail .out-row').length > 0", timeout=120)
    await take.js(
        "(() => { const h = [...document.querySelectorAll('#detail h2')]"
        "  .find(e => e.textContent.includes('agents made'));"
        "  if (h) h.scrollIntoView({block: 'start'}); })()")
    # Then back up past the sticky header. `scrollIntoView` puts the heading at
    # the top of the viewport and the header sits on top of it, so the first
    # row's label ended up behind the counts -- a transport moving with nothing
    # to say which file it belongs to.
    await take.js("window.scrollBy(0, -72)")
    await asyncio.sleep(1.0)
    # The three audio rows and not the whole panel. Framing the panel put its
    # top edge between the first row's label and its transport, so the shot
    # showed a control moving with nothing to say which file it belonged to --
    # and the packaged video underneath made the region tall enough that
    # fitting it to 16:9 zoomed the labels down to nothing.
    await take.push(
        await take.rect_of_all('#detail .out-row[data-kind="audio"]', 26), 0.1)
    await take.hold(0.8)

    at = 0.0
    for index, (begins, runs) in enumerate(plays):
        if begins > at:
            await take.watch(begins - at)
            at = begins
        # `currentTime` set to the same second the excerpt was cut from, so
        # the elapsed counter under the transport reads what is being heard.
        # Seek, then play, then play again half a second later. The third
        # transport seeked and stopped: `play()` issued in the same tick as a
        # `currentTime` that has not resolved does nothing, silently, and the
        # first two only worked because their media was already buffered. The
        # second call is a no-op when the first one took.
        seek = ("(() => { const a = document.querySelectorAll("
                "'#detail .out-row audio, #detail .out-row video')[%d];"
                " if (a) { a.currentTime = %.2f; a.play(); } })()")
        nudge = ("(() => { const a = document.querySelectorAll("
                 "'#detail .out-row audio, #detail .out-row video')[%d];"
                 " if (a && a.paused) a.play(); })()")
        await take.js(seek % (index, [12.9, 12.9, 2.9][index]))
        await take.watch(0.5)
        await take.js(nudge % index)
        await take.watch(runs - 0.5)
        at += runs
        await take.js(
            "(() => { const a = document.querySelectorAll("
            "'#detail .out-row audio, #detail .out-row video')[%d];"
            " if (a) a.pause(); })()" % index)
    await take.hold(1.5)


async def compliance(take, token: str):
    """The two dimensions nothing here can repair, and why each is red.

    Japan first, because it fails in three different ways at once -- a right
    that was never granted, a right whose window has not opened, and a
    certificate nobody has submitted -- and then Germany, where all of it is
    green. Same panel, same colours, entirely different reasons.
    """
    await take.goto(LOCAL + "/#ja-JP", settle=2)
    await take.wait_for(
        "document.querySelectorAll('#detail .comp .card').length > 0",
        timeout=120)
    await take.js(
        "(() => { const h = [...document.querySelectorAll('#detail h2')]"
        "  .find(e => e.textContent.includes('Compliance'));"
        "  if (h) h.scrollIntoView({block: 'start'}); })()")
    # Then back up past the sticky header. `scrollIntoView` puts the heading at
    # the top of the viewport and the header sits on top of it, so the first
    # row's label ended up behind the counts -- a transport moving with nothing
    # to say which file it belongs to.
    await take.js("window.scrollBy(0, -72)")
    await asyncio.sleep(1.0)
    await take.push(await take.rect_of_all("#detail .comp .card", 24), 1.2)
    await take.hold(7.5)
    await take.js("location.hash = '#de-DE'")
    await take.wait_for(
        "document.querySelectorAll('#detail .comp .card.ok').length > 0",
        timeout=120)
    await take.js(
        "(() => { const h = [...document.querySelectorAll('#detail h2')]"
        "  .find(e => e.textContent.includes('Compliance'));"
        "  if (h) h.scrollIntoView({block: 'start'}); })()")
    await asyncio.sleep(0.8)
    await take.push(await take.rect_of_all("#detail .comp .card", 24), 1.0)
    await take.hold(4.0)


async def dimensions(take, token: str):
    """Across the matrix a column at a time: this is not a dubbing tool.

    The film has just spent twenty-four seconds on audio, which is the moment
    a viewer decides what kind of system this is. Every column is a different
    trade with a different failure mode -- a frame rate is not a right, a
    right is not a certificate, and a certificate is not a storefront record
    -- and the only way to say that without a list is to move across them.

    Addressed by `data-dim` rather than by column index, so inserting a
    dimension does not silently reframe the shot onto its neighbour.
    """
    await _open_board(take)
    await take.push(await take.rect_of("table.matrix", 8), 1.2)
    await take.hold(2.5)
    for group in (["Localisation", "Audio"],
                  ["Timed text", "Accessibility"],
                  ["Technical"],
                  ["Rights", "Certification"],
                  ["Packaging"]):
        selector = ", ".join('[data-dim="%s"]' % d for d in group)
        await take.push(await take.rect_of_all(selector, 22), 1.0)
        await take.hold(3.2)
    await take.push(await take.rect_of("table.matrix", 8), 1.3)
    await take.hold(4.5)


async def repair_ask(take, token: str):
    """A proposal, with the number it promises to reach, and the press.

    The film says elsewhere that an agent must predict its own result before
    it is allowed to act. This is the only place that promise is on screen:
    the strategy, the parameters, the series and the value it will land past,
    and the authority tier -- RECOMMEND, because REMIX has no measured history
    in this market and therefore proposes rather than acts. Approving is a
    person supplying authority the agent has not yet earned.
    """
    await take.goto(LOCAL + "/#pt-BR", settle=2)
    await take.js("sessionStorage.setItem('op', %r)" % token)
    await take.goto(LOCAL + "/#pt-BR", settle=2)
    await take.wait_for("!!document.querySelector('#detail .prop')", timeout=150)
    await asyncio.sleep(1.0)
    await take.push(await take.rect_of("#detail .prop", 20), 1.3)
    await take.hold(9.0)
    pressed = await take.js(
        "(() => { const b = document.getElementById('approve');"
        " if (!b || b.disabled) return false; b.click(); return true; })()")
    if not pressed:
        raise RuntimeError("no enabled Approve button -- is a proposal pending "
                           "and is the operator token set?")
    # Long enough to see the button change and the run log open. The repair
    # itself takes about a minute: it re-investigates, acts, re-probes, and
    # writes the outcome. Sitting on a spinner for that minute is not a shot,
    # so the film cuts to the ledger, which is where the answer is kept.
    await take.watch(9.0)


async def repair_done(take, token: str):
    """Where the answer is kept: the ledger, and the measurement it moved.

    Deliberately not a continuation of the previous shot's clock. What the
    approval produced is written down -- outcome, baseline, observed, and the
    prediction it is judged against -- and that record outlives the run log
    on somebody's screen. The line in view is the one the previous shot
    started.
    """
    await _open_board(take)
    # Wait for the detail pane to finish before scrolling past it. It is one
    # round trip per figure to Grafana Cloud, and until it lands it is a short
    # skeleton -- so anything below it is measured at the wrong height, and the
    # camera frames the panel that was there a second ago.
    await take.wait_for("!!document.getElementById('measured')", timeout=150)
    await take.js("document.getElementById('activity')"
                  ".scrollIntoView({block: 'center'})")
    await asyncio.sleep(1.0)
    await take.push(await take.rect_of("#activity", 10), 1.2)
    await take.hold(17.5)


async def wakelog(take, token: str):
    """Grafana calling the agents, in the receiver's own words.

    Everything else about the alert path is shown from Grafana's side, where
    a rule can be firing and nothing on earth be listening. This is the other
    end of the webhook.
    """
    await take.goto(LOCAL + "/wakelog", settle=2)
    await take.hold(13.0)


async def architecture(take, token: str):
    """The shape, once, after every part of it has been seen working.

    Deliberately last before the close. Shown at the start it would be a
    diagram of claims; shown here it is a recap of things the viewer has
    already watched happen.
    """
    await take.goto(LOCAL + "/architecture", settle=2)
    await take.hold(4.0)
    await take.push(await take.rect_of_all(".band .step", 26), 1.4)
    await take.hold(7.5)
    await take.push(await take.rect_of(".back", 30), 1.2)
    await take.hold(4.5)
    await take.wide(1.4)
    await take.hold(4.0)


async def endcard(take, token: str):
    """The last frame, and the only one that is not the product.

    Served over http rather than opened as a file:// URL: Chrome treats a
    local file as an opaque origin and the fonts and layout that make it
    match the rest of the film are the first things to go.
    """
    await take.goto(LOCAL + "/endcard", settle=2)
    await take.hold(9.0)


async def verdict(take, token: str):
    """The three factors that multiply into the verdict, and their PromQL."""
    await take.goto(CONTROL + "/#de-DE", settle=2)
    await take.wait_for("!!document.getElementById('investigate')")
    await asyncio.sleep(1.5)
    await take.push(await take.rect_of("#detail .pad", 12), 1.3)
    await take.hold(6.0)
    # In tight on a query string. Being able to check a number is the whole
    # argument, and at full frame nobody can read one.
    await take.focus("#detail .q", 1.5, pad=70)
    await take.hold(5.0)


async def coverage(take, token: str):
    """The hollow pips: checks nobody has run, blocking just as hard."""
    await _open_board(take)
    await take.push(await take.rect_of("table.matrix", 8), 0.1)
    await take.hold(2.0)
    # hi-IN and not pt-BR. pt-BR used to be the market with six checks nobody
    # had run; it was then built through the pipeline from the control room,
    # which is what filled them in. Being able to see that is the point of the
    # build panel -- but it means the shot about never-measured checks has to
    # point at a market that has not been built yet.
    await take.focus('#rows .row[data-m="hi-IN"]', 1.5, pad=12)
    await take.hold(5.0)
    await take.push(await take.rect_of(".legend", 30), 1.3)
    await take.hold(3.5)


async def lineage(take, token: str):
    """What the agents built, and what each thing was built from.

    The pipeline's output made visible without a terminal: adapted lines, the
    dub stem, the subtitle, the described track, and the package assembled
    from all of them -- each with the hash of the version it was built
    against, which is what makes staleness a fact rather than a guess.
    """
    await take.goto(CONTROL + "/#de-DE", settle=2)
    await take.wait_for("!!document.getElementById('investigate')")
    await asyncio.sleep(1.5)
    await take.js(
        "(() => { const h = [...document.querySelectorAll('#detail h2')]"
        "  .find(e => e.textContent.includes('Lineage'));"
        "  if (h) h.scrollIntoView({block: 'center'}); })()")
    await asyncio.sleep(1.0)
    await take.push(await take.rect_of_all("#detail .asset", 26), 1.3)
    # Shorter than it was. The blast radius used to follow here and is the
    # better idea, but this block of narration now has to carry the build as
    # well and something had to give. It is on the screen for anyone who
    # opens the control room; the film cannot hold everything.
    await take.hold(8.5)


async def swarm(take, token: str):
    """One specialist per failing dimension, working at once."""
    await take.goto(CONTROL + "/#ja-JP", settle=2)
    await take.wait_for("!!document.getElementById('investigate')")
    await take.js("sessionStorage.setItem('op', %r)" % token)
    await asyncio.sleep(1.2)
    await take.js("document.getElementById('investigate').click()")
    await take.push(await take.rect_of("#trace", 10), 1.2)
    # At the pace the agents actually work. The elapsed time is the evidence.
    await take.watch(50.0)
    await take.hold(2.5)


async def autonomy(take, token: str):
    """What each strategy has earned, and what the contracts have refused."""
    await _open_board(take)
    # Wait for the detail pane to finish before scrolling past it. It is one
    # round trip per figure to Grafana Cloud, and until it lands it is a short
    # skeleton -- so anything below it is measured at the wrong height, and the
    # camera frames the panel that was there a second ago.
    await take.wait_for("!!document.getElementById('measured')", timeout=150)
    await take.js("document.getElementById('autonomy')"
                  ".scrollIntoView({block: 'center'})")
    await asyncio.sleep(0.9)
    await take.push(await take.rect_of("#autonomy", 10), 1.1)
    await take.hold(9.0)
    await take.js("document.getElementById('guardrails')"
                  ".scrollIntoView({block: 'center'})")
    await asyncio.sleep(0.9)
    await take.push(await take.rect_of("#guardrails", 10), 1.1)
    await take.hold(5.5)


async def repairs(take, token: str):
    """The ledger: lucky and failed alongside the successes."""
    await _open_board(take)
    # Wait for the detail pane to finish before scrolling past it. It is one
    # round trip per figure to Grafana Cloud, and until it lands it is a short
    # skeleton -- so anything below it is measured at the wrong height, and the
    # camera frames the panel that was there a second ago.
    await take.wait_for("!!document.getElementById('measured')", timeout=150)
    await take.js("document.getElementById('activity')"
                  ".scrollIntoView({block: 'center'})")
    await asyncio.sleep(1.0)
    await take.push(await take.rect_of("#activity", 10), 1.1)
    await take.hold(12.0)


async def unrepairable(take, token: str):
    """The hatched checks: failing, and nothing here can fix them."""
    await _open_board(take)
    await take.push(await take.rect_of("table.matrix", 8), 0.1)
    await take.hold(2.5)
    await take.focus('#rows .row[data-m="ja-JP"] .pip.hard', 1.7, pad=200)
    await take.hold(8.0)


async def close(take, token: str):
    """Rest on the answer: two can ship, three cannot."""
    await _open_board(take)
    await take.push(await take.rect_of("table.matrix", 8), 0.1)
    await take.hold(4.0)
    await take.wide(1.6)
    await take.hold(5.0)


# ---------------------------------------------------------------------------
# Grafana
# ---------------------------------------------------------------------------


async def grafana_rule(take, token: str):
    """The alert that wakes the agents, in the state it is actually in.

    Straight to the rule's own page rather than the list. The list shows a
    folded group and a lot of Grafana's filtering UI; the rule page shows the
    thing that matters -- the rule is FIRING, and which markets it is firing
    for, by name.
    """
    await take.goto(GRAFANA + f"/alerting/grafana/{ALERT_UID}/view", settle=9)
    await take.js(
        "(() => { for (const b of document.querySelectorAll('button')) {"
        "  const a = (b.getAttribute('aria-label') || '').toLowerCase();"
        "  if (a.includes('close') || a.includes('dismiss')) b.click();"
        "} })()"
    )
    await take.js(
        "(() => { const b = document.querySelector("
        "'[aria-label*=\"navigation\" i],[aria-label*=\"mega menu\" i]');"
        " if (b) b.click(); })()"
    )
    await asyncio.sleep(1.4)
    await take.hold(6.0)
    # Down to the per-market state: two Normal, three Firing.
    await take.glide(380, 2.6)
    await take.hold(7.0)


async def grafana_verdict(take, token: str):
    """Grafana's own view of the verdict, on Grafana's own dashboard.

    The point of this shot is that none of it is our interface. The readiness
    counts, the verdict over time and the table of what is blocking each market
    are Grafana panels over Grafana data -- the control room is a reading of
    this, not a replacement for it.
    """
    await _open_dashboard(take, "/d/continuity-control-room/?kiosk")
    await take.hold(5.0)
    await take.glide(300, 2.6)
    await take.hold(6.0)
    await take.glide(640, 2.4)
    await take.hold(4.0)


async def grafana_agent(take, token: str):
    """Grafana watching the agents themselves.

    Guardrail refusals, tool calls, how each run ended, tokens per call. The
    agents are not merely monitored BY this stack -- they are monitored IN the
    same stack they read their instructions from.
    """
    await _open_dashboard(take, "/d/continuity-agent/?kiosk")
    await take.hold(5.0)
    await take.glide(320, 2.6)
    await take.hold(6.0)


async def grafana_dash(take, token: str):
    """The measurements, against the bar each market sets."""
    await _open_dashboard(
        take, "/d/continuity-media-qc/?var-market=de-DE&var-market=fr-FR&kiosk")
    await take.hold(4.5)
    await take.glide(430, 3.2)
    await take.hold(6.0)


SHOTS = {
    "board": (board, None),
    "release": (release, None),
    "outputs": (outputs, None),
    "listen": (listen, None),
    "compliance": (compliance, None),
    "dimensions": (dimensions, None),
    "repair_ask": (repair_ask, None),
    "repair_done": (repair_done, None),
    "wakelog": (wakelog, None),
    "architecture": (architecture, None),
    "endcard": (endcard, None),
    "verdict": (verdict, None),
    "coverage": (coverage, None),
    "lineage": (lineage, None),
    "swarm": (swarm, None),
    "autonomy": (autonomy, None),
    "repairs": (repairs, None),
    "unrepairable": (unrepairable, None),
    "close": (close, None),
    "grafana_rule": (grafana_rule, PROFILE),
    "grafana_verdict": (grafana_verdict, PROFILE),
    "grafana_agent": (grafana_agent, PROFILE),
    "grafana_dash": (grafana_dash, PROFILE),
}

# Which shots carry which block of narration. The edit cuts each group to the
# length of its voiceover, so a group must be longer than the words it holds.
# One film through the pipeline, in order: what goes in, what gets made, how
# it is measured, who decides, what happens when it breaks, how the agents are
# marked, and where it ends up.
BLOCKS = {
    "vo1":  ["board"],
    "vo2":  ["release", "lineage", "outputs"],
    # The one block whose soundtrack is the product. See scripts/listen.py.
    "vo2b": ["listen"],
    "vo2c": ["dimensions"],
    "vo3":  ["verdict", "grafana_dash", "grafana_verdict"],
    "vo4":  ["coverage", "grafana_rule"],
    "vo4b": ["wakelog"],
    "vo5":  ["swarm"],
    "vo5c": ["repair_ask", "repair_done"],
    "vo5b": ["compliance"],
    "vo6":  ["repairs", "autonomy", "grafana_agent"],
    "vo6b": ["architecture"],
    # Not `unrepairable` any more. Three clips came to thirty-two seconds
    # against twenty of narration, and since a group is trimmed from the end,
    # the end card -- the only frame carrying the links -- was cut off
    # entirely. What that shot said is now said better by the compliance
    # panel a minute earlier, with the actual reasons on screen.
    "vo7":  ["close", "endcard"],
}

ORDER = [name for block in BLOCKS.values() for name in block
         if name in SHOTS]

# Shots that cannot be driven with the published demo token. An investigation
# costs model quota and changes no asset, which is why the demo token can run
# one; a build rewrites this title's assets, which is why it cannot. The
# recorder reads the operator token for these and only these. Neither token is
# ever on screen -- both are set into sessionStorage, and the header shows the
# word "signed in" rather than the string.
NEEDS_OPERATOR = frozenset({"release"})
