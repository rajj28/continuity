# Demo runbook — three minutes

Three minutes is enough for exactly one story, so this tells one:

> **A film goes in and market-ready packages come out. Grafana decides whether
> any of them can ship. When it says no, it wakes an agent — and the agent has
> to predict what its repair will do, then gets marked on whether the
> prediction held, not on whether it got away with it.**

Everything below happens against the live stack. Nothing is staged. The numbers
in Act IV are the ones in `out/store/repairs.jsonl`, including the failures.

## Before recording

```bash
python -m pytest -q                      # 320 passing; have this on screen once
```

Check the control room is serving live numbers, not an empty board:

```bash
curl -s $CONTROL/api/board | python -m json.tool | head -20
```

If every market shows `0` present, the state exporter is not publishing and
Prometheus has aged the series out. It runs inside the `continuity-control`
service; check its logs before recording anything.

Run the control room **locally** for Act I. The deployed one is the same image
and the same code, but it has nothing to build from: a feature master is a
quarter of a gigabyte, a container is the wrong place for a film, and Cloud Run
will not accept a request body that size either. Acts II onward are better on
the deployed service, which sits in the same region as Grafana and answers in
a fraction of the time.

```bash
python ui/server.py                      # :8090, with assets/sintel/ present
```

Have open, in this order: the control room, Grafana (Continuity — Media QC),
and a terminal.

---

## Act I — a master goes in (0:00–0:55)

**Start at the top of the control room.** Drop `assets/sintel/master_v1.mp4`
and `assets/sintel/sintel_en.srt` on the panel — or leave them selected, they
are the ones shipped with the project. Tick a market that has not been built.
Press **Build the release**.

> A film does not ship once. It ships fifty times — every territory wants its
> own dub, its own subtitles, its own described track, its own certificate, its
> own package. This is a video and a list of what is said in it. That is the
> whole input.

Seven stages appear and start moving. Underneath them, the stages' own output:
the hash of the ingested master, each line as it is adapted, the loudness as it
is measured.

> Nothing here is a progress bar. Those are the seven scripts in the README,
> started as processes, printing what they print. Gemini rewrites every line to
> fit the gap it has to land in and speaks it. The subtitles come from those
> same lines. Another agent watches the picture and describes it for people who
> cannot see it. Another writes the storefront copy. Then it is assembled into
> the file a platform actually receives.

**Select a built market and scroll to "What the agents made".** Play the
original, then the dub, then the described track on its own. Then press play on
the deliverable.

> This is not a report about the work. It is the work. Everything else on this
> screen is a representation of it — a pip, a number, a hash — and a dub you
> cannot hear is indistinguishable from one that was never made.

The described track is the one to dwell on: narration written to land in the
gaps *between* lines, which is what `ad_collision_ms` is measuring when it
reads zero.

## Act II — the board, and who decides (0:55–1:30)

**Scroll to the readiness matrix.** Five markets down the side, eight
dimensions across. One pip per check: solid passed, hatched failed, hollow
never measured.

> Eighty-seven checks across five markets. Not one of these numbers came from a
> model — every one is ffmpeg output, parsed deterministically. And the verdict
> on the right is not this screen's opinion. It's a Grafana recording rule.

**Click a green market.** The Verdict panel names the three factors that
multiply into `market_release_ready`, each with the PromQL that produced it.
Click **Open in Grafana** and show the same numbers on the dashboard.

> The agents read Grafana through an MCP server started with `--disable-write`.
> It registers zero write tools — not refused at runtime, absent from the
> protocol. Nothing here can talk itself green; it would have to write to
> Mimir, and it holds no credential that can.

Point at the **readiness-over-time** strip: hatched means no verdict at all.

> A gap is not a pass. A check nobody ran blocks exactly as hard as one that
> failed — which is why a market nobody has built yet is red for things nobody
> has measured, rather than quietly green.

That is also the cleanest way to show what Act I did: the market you built went
from six hollow columns to seventeen of seventeen measured, and picked up three
real failures in the process. Filling the gaps is not the same as passing.

## Act III — Grafana wakes the agent (1:30–2:05)

Do not trigger this from the UI. The point is that **Grafana initiates**.

Show the alert rule firing on `market_release_ready < 1`, its webhook pointed
at the Cloud Run receiver, then the receiver's log:

```
accepted MarketNotReleaseReady[firing] SINTEL/de-DE -> investigate
```

Cut to the control room's live trace panel. The roster forms first — one
specialist per failing dimension, working at once — and then tool calls appear
as they happen: `list_failing_checks`, `query_metric`, `get_threshold`,
`repair_history`.

> That's the Agent Development Kit running the loop. Every number a specialist
> states came back from a tool. A proposal citing a query it never ran is
> rejected as `uncited_evidence`, and those rejections are a time series you
> can graph.

Point at the compliance specialist in the roster, marked read-only.

> It was never handed a repair tool. An expired certificate is not a fault in a
> file, so it can only gather the facts and pass them to a person. Its limits
> are in what it can call, not in what it was told.

## Act IV — predicted, measured, marked (2:05–2:40)

Show the proposal: strategy, parameters, **prediction**, authority, evidence.

> It has to predict. Which series, which direction, past which value. That
> prediction is checked against the measurement afterwards.

Approve it. RETIME shifts the stem and sync goes **375.2 → 17.4 ms** — inside
the 120 ms tolerance. Then show the ledger line:

```
RETIME  de-DE  lucky   375.2 -> 17.4   predicted ... -> <= 0
```

> It worked. It is not recorded as a success. The agent predicted the drift
> would go past zero and it landed at 17.4, so the market improved but not for
> the reason given. That's `lucky`, and lucky raises the denominator without
> raising the numerator — getting away with it costs a strategy authority
> rather than earning it.

Second pass: REMIX takes loudness **−20.17 → −22.96 LUFS** against a −23 target.
Also `lucky`, by four hundredths.

Then scroll the **Earned autonomy** panel and show RETIME's real record:
successes, failures and luck together.

> Those failures are real. Three of them came from one bug: the sync
> measurement is a magnitude, so it says how far off the dub is and not which
> way — and RETIME was shifting the wrong direction, turning 187 ms early into
> 375 ms early. Every one of those was caught by verification and recorded
> failed. Nothing shipped. That's the difference between a system that measures
> its own work and one that reports what it intended to do.

## Act V — the ones it must not touch (2:40–3:00)

Back to the board. Point at a hatched pip — `rights_cleared`, `certified`.

> A right that was never cleared is not a defect in a file, and no amount of
> processing fixes it. Brazil is waiting on DEJUS. No agent here can make a
> regulator decide, and the system will not pretend otherwise.

Close on the board.

> Two markets can ship. Three cannot, and every one of them can tell you
> exactly what it's waiting for. Nobody checked any of this by hand.

---

## If something goes wrong on camera

- **Board empty** — the exporter died; series aged out after five minutes.
  Restart the `continuity-control` revision.
- **The build panel offers nothing to build from** — that is the deployed
  service, which ships no master. Run the control room locally, where
  `assets/sintel/` is on disk.
- **A stage fails mid-build** — let it. The rail marks that stage red, the
  stages after it do not run, and the last lines of its own output are on
  screen. Stopping at the first failure is deliberate: everything downstream
  reads what it was meant to write.
- **Investigation hangs** — Vertex quota. The escalation path is real; let it
  escalate and narrate that as the designed behaviour, because it is.
- **Approve is disabled** — that is the hosted control room. It carries the
  produced media so the outputs play, but not the content-addressed store
  behind them, and a repair reads its target's parents. Run the repair from the
  terminal instead: `python scripts/repair.py --market de-DE --approve`.
- **A repair refuses with "Prometheus has not yet ingested the last repair"** —
  working as intended. Wait one publish cycle (30 s) and re-run; the guard
  exists because acting on a stale reading is how the loop over-corrected.
