# Demo runbook — three minutes

Three minutes is enough for exactly one story, so this tells one:

> **Grafana decides whether a film can ship. When it says no, it wakes an agent.
> The agent has to predict what its repair will do, and gets marked on whether
> the prediction held — not on whether it got away with it.**

Everything below happens against the live stack. Nothing is staged. The numbers
in Act III are the ones in `out/store/repairs.jsonl`, including the failures.

## Before recording

```bash
python -m pytest -q                      # 272 passing; have this on screen once
```

Check the control room is serving live numbers, not an empty board:

```bash
curl -s $CONTROL/api/board | python -m json.tool | head -20
```

If every market shows `0` present, the state exporter is not publishing and
Prometheus has aged the series out. It runs inside the `continuity-control`
service; check its logs before recording anything.

Have open, in this order: the control room, Grafana (Continuity — Media QC),
and a terminal.

---

## Act I — the board (0:00–0:45)

**Show the control room.** Five markets down the side, eight dimensions across.
One pip per check: solid passed, hatched failed, hollow never measured. Two
markets green, three red.

> Eighty-seven checks across five markets. Not one of these numbers came from a
> model — every one is ffmpeg output, parsed deterministically. And the verdict
> on the right is not this screen's opinion. It's a Grafana recording rule.

**Click de-DE**, the green one. The Verdict panel names the three factors that
multiply into `market_release_ready`, each with the PromQL that produced it.
Click **Open in Grafana** and show the same numbers on the dashboard.

> The agents read Grafana through an MCP server started with `--disable-write`.
> It registers zero write tools — not refused at runtime, absent from the
> protocol. Nothing here can talk itself green; it would have to write to
> Mimir, and it holds no credential that can.

Point at the **readiness-over-time** strip: hatched means no verdict at all.

> A gap is not a pass. A check nobody ran blocks exactly as hard as one that
> failed, which is why three of these markets are red for things nobody has
> measured yet rather than quietly green.

## Act II — Grafana wakes the agent (0:45–1:30)

Do not trigger this from the UI. The point is that **Grafana initiates**.

Show the alert rule firing on `market_release_ready < 1`, its webhook pointed
at the Cloud Run receiver, then the receiver's log:

```
accepted MarketNotReleaseReady[firing] SINTEL/de-DE -> investigate
```

Cut to the control room's live trace panel. Tool calls appear as they happen —
`list_failing_checks`, `query_metric`, `get_threshold`, `repair_history`.

> That's the Agent Development Kit running the loop. Every number it states came
> back from a tool. A proposal citing a query it never ran is rejected as
> `uncited_evidence`, and those rejections are a time series you can graph.

## Act III — predicted, measured, marked (1:30–2:30)

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

## Act IV — the ones it must not touch (2:30–3:00)

Back to the board. Point at a hatched pip — `rights_cleared`, `certified`.

> A right that was never cleared is not a defect in a file, and no amount of
> processing fixes it. The agent is told which blockers are repairable and
> escalates the rest. A system that tried to fix everything would eventually
> ship something it had no right to.

Scroll to **Blast radius** on a green market.

> Before you approve anything: repairing the stem gives it a new hash, and
> everything built from the old one is stale until it's rebuilt. That's read
> from the asset index, not from traces — traces are sampled and expire, and an
> empty blast radius for an asset that has children is the most dangerous wrong
> answer this panel could give.

Close on the board.

> Two markets can ship. Three cannot, and every one of them can tell you
> exactly what it's waiting for.

---

## If something goes wrong on camera

- **Board empty** — the exporter died; series aged out after five minutes.
  Restart the `continuity-control` revision.
- **Investigation hangs** — Vertex quota. The escalation path is real; let it
  escalate and narrate that as the designed behaviour, because it is.
- **Approve returns a message about missing media** — that is the hosted
  control room, which carries the index and not the 240 MB of media. Run the
  repair from the terminal instead: `python scripts/repair.py --market de-DE
  --approve`.
- **A repair refuses with "Prometheus has not yet ingested the last repair"** —
  working as intended. Wait one publish cycle (30 s) and re-run; the guard
  exists because acting on a stale reading is how the loop over-corrected.
