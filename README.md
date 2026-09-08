# Continuity

**A global release autopilot for media.** One question, answered continuously
for every market, with evidence: *can we release it?*

A film does not ship to the world once. It ships fifty times — a dub per
language, subtitles, captions, an audio description track, a storefront record,
a certificate from each ratings body, a package built to each platform's
technical spec. Every one of those is a separate artefact that can be wrong,
and every one of them can be made wrong again by a change upstream. The work of
a global release is not making those artefacts. It is knowing, at any moment,
which of them are currently good.

Continuity treats each derivative asset as a running service and the release
itself as a system with an SLO. Lip-sync drift, subtitle reading rate,
programme loudness, audio-description collisions, technical conformance, rights
clearance and asset staleness all become live Prometheus series. And the
release verdict for each market is a **Grafana recording rule**:

```promql
market_release_ready =
      market_requirements_all_met        # everything measured, passed
    * market_coverage_complete           # everything owed, was measured
    * (1 - market_has_stale_assets)      # nothing built from a stale parent
```

That expression is the whole architecture. **Grafana computes the verdict, and
no agent can talk it green** — the agents reach Grafana through an MCP server
started with `--disable-write`, which registers zero create/update/delete
tools. The read-only guarantee is enforced by the process, not by a prompt.

When a market goes red, **Grafana calls the agents**, not the other way round.
An alert fires, POSTs to a Cloud Run receiver, and the Conductor investigates
through the same MCP boundary — correlating evidence, forming a falsifiable
prediction, taking the smallest corrective action against real media, then
re-running the query that condemned the asset. If the metric did not move, the
repair failed and the plan is revised.

**Models propose. Measurements dispose.**

---

## Live

| | |
|---|---|
| **Control room** | https://continuity-control-z6txmgck2a-el.a.run.app |
| Wake receiver | `https://continuity-wake-z6txmgck2a-el.a.run.app/alert` |
| Grafana | 3 provisioned dashboards, 5 markets, 87 checks |

Both services run from **one image at one digest with a different argument**,
so a repair started by an alert and a repair approved by an operator are the
same code. The control room also runs the state exporter, which republishes
the on-disk store to Prometheus every 30 seconds — series go stale after five
minutes, so without it the board would correctly but uselessly report that
nothing has been measured.

### Drive it yourself

Reads are open — the board, the verdict provenance, the timeline and the
lineage need nothing. To watch the agents actually work, sign in on the
control room with:

```
demo-vd18Z5yDsCiBzkVIv9ThJQOz
```

Pick a blocked market and press **Investigate now**. One specialist is woken
per failing dimension and they work in parallel; every tool call streams as it
happens. Watch the compliance specialist in particular — it is never handed a
repair tool, so it can only escalate.

That token runs investigations and **cannot approve a repair**, and the wake
receiver does not accept it at all. It is published deliberately; the operator
token is not. An investigation costs model quota and changes no asset, which is
why this one can be public.

`docs/DEMO.md` is the three-minute walkthrough.

---

## The loop

```
        ffmpeg / Gemini                  Mimir recording rules
   measurement ──────────► metrics ──────────► verdict ──────► alert
        ▲                                                        │
        │                                                        ▼
   real media file                                    Cloud Run wake receiver
        ▲                                                        │
        │                                                        ▼
   repair executed ◄──── operator approves ◄──── proposal ◄── Conductor (ADK)
        │                     or autonomy tier                   │
        └──────────────► re-measure, verify prediction ◄─────────┘
                                    │
                                    ▼
                          ledger: succeeded / lucky / failed
```

Three things in that diagram are the point:

**The verdict is not the agent's.** Thresholds are published as metrics
(`market_threshold`) alongside measurements, so the verdict is a join between
two independently published facts. An agent that wanted to pass a check would
have to write to Mimir, and it holds no credential that can.

**A missing measurement blocks exactly as a failing one does.**
`market_coverage_complete` is why. Most systems of this shape are green when
they are broken, because absent data reads as absent problems.

**Being right is not the same as being right for the right reason.** Every
repair carries a falsifiable prediction — which series, which direction, past
which value. Afterwards the outcome is recorded `succeeded`, `lucky` (it passed
but the prediction did not hold) or `failed`. `lucky` raises the denominator
and not the numerator, so a strategy cannot earn autonomy by coincidence.

Autonomy is earned per strategy per market, read from `continuity_repairs_total`
in Prometheus at the moment of the decision. Three verified successes and a
repair stops asking.

---

## Run it

### Requirements

- Python 3.11+
- ffmpeg 7+ on PATH (needs `silencedetect`, `loudnorm`, `ebur128`)
- A Grafana Cloud stack (free tier is enough) and a Google Cloud project with
  Vertex AI enabled

`docs/SETUP.md` has the full credential list. Copy `.env.example` to
`.env.local` and fill it in; that file is gitignored and must stay that way.

```bash
python -m pip install -r requirements.txt
python -m pytest -q                    # 310 passing
```

### The pipeline, end to end

```bash
python scripts/stage1.py                          # master -> scenes, content-addressed
python scripts/stage2.py --market de-DE           # adapt + speak a dub, measure it
python scripts/stage3.py --market de-DE           # subtitles from the adapted lines
python scripts/stage4.py --market de-DE           # audio description
python scripts/stage5.py --market de-DE           # storefront metadata, forced narratives
python scripts/release_check.py                   # technical, rights, deliverables
python scripts/stage6.py --market de-DE           # assemble the deliverable
```

Every number those produce comes out of ffmpeg and is parsed deterministically.
No model produces a value that can block a release.

Stage 6 is what the market actually receives: one MP4 carrying the picture, the
dubbed audio, the audio description as its own selectable track with the
accessibility flag set, and language-tagged subtitles — plus a manifest listing
every ingredient by hash. The video is copied, never re-encoded. Because the
package records its ingredients as parents, repairing any of them stales it and
blocks the market until it is rebuilt.

### The agentic loop

```bash
python grafana/provision.py                       # rules, dashboards, alerts
python scripts/run_adk.py --market de-DE          # investigate, propose (no action)
python scripts/run_swarm.py --market ja-JP        # wake every failing dimension at once
python scripts/repair.py --market de-DE --approve # investigate, act, verify, learn
python ui/server.py                               # control room at :8090
```

### Deploy

```bash
python deploy/cloudrun.py --check                 # preflight only
python deploy/cloudrun.py                         # build once, deploy both services
python grafana/alerting.py --url <wake-url>/alert # point Grafana at it
```

---

## What each piece is doing there

**Grafana Cloud** — Mimir holds the measurements and computes the verdict as a
recording rule; Grafana-managed alerts fire on it and call the agents; the
agents read it back through `mcp-grafana`. It is the control loop, not the
picture of one.

**Vertex AI (Gemini)** — adapts each line of real dialogue to the slot it has
to fit, speaks it, writes the localised storefront record, watches the picture
for on-screen text, and reasons as the Conductor. Authenticated as a service
account; no consumer API key anywhere in the deployed path.

**Agent Development Kit** — runs the Conductor's tool loop and is the path to
Agent Engine. The runtime is ADK's; the authority is `agents/contracts.py`.

**A roster of specialists, not one generalist.** A blocked market is usually
blocked several ways at once and those ways are independent, so the failing
dimensions are investigated concurrently by agents that each know one of them.
Authority is the tool list rather than the prompt: the compliance specialist
is never handed `propose_repair`, so it cannot invent a fix for an uncleared
music cue — the same guarantee as `--disable-write`, one level up. Only the
specialists whose dimension is actually failing are woken, and their proposals
are applied one at a time in an order taken from the asset lineage, so a repair
runs before anything built from it.

**Cloud Run / Cloud Build / Artifact Registry** — the wake receiver and the
control room, from one digest-pinned image.

**Cloud Storage, Pub/Sub** — the asset store's remote backing and the work
queue between alert and repair.

**OpenTelemetry** — spans follow the OTel GenAI semantic conventions
(`gen_ai.*`), so token usage and tool calls are queryable alongside the media
measurements they justified. Guardrail refusals are a time series
(`continuity_agent_rejections_total{reason}`), which means "how often does the
agent try to claim something it did not measure" is a question with an answer.

That series is republished from an append-only log rather than incremented in
process, for the same reason the autonomy ledger is: an agent run is a
short-lived job, and a counter that ages out five minutes after it exits
reports "nothing was ever refused" — indistinguishable from a guardrail that
has never fired, and the one thing this metric must never say untruthfully.

---

## Honesty

- `docs/LIMITATIONS.md` — what is not real yet, stated plainly
- `docs/MCP_BOUNDARIES.md` — exactly which tools the agents can reach, and the
  verified count

There is no synthetic data in this project and no mocked service. Every failure
it repairs is a real failure in a real file, measured by ffmpeg, and the
recovery arc where the first repair proves insufficient is a real one that was
observed and not staged.

Source media is *Sintel* © Blender Foundation, CC-BY 3.0. Provenance for every
asset is recorded in `assets/SOURCES.md`.

## License

Apache-2.0. See `LICENSE`.
