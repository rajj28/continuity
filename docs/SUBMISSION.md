# Devpost submission text

Copy-paste ready. Written against the four stated criteria, for the **Grafana
Labs** track.

---

## Tagline

**Grafana decides whether a film can ship. When it says no, it wakes a crew of
agents — and marks them on whether their prediction held, not on whether they
got away with it.**

---

## Inspiration

A film does not ship to the world once. It ships fifty times.

Germany needs a German dub, German subtitles, an FSK certificate, an audio
description track, a German storefront page, and a package built to a specific
technical spec. Japan needs all of that again, differently — including a much
tighter subtitle reading rate, because Japanese characters take longer to read.

Every one of those is a separate artefact that can be wrong, and every one can
be made wrong again by a change upstream. Someone re-cuts a scene and now the
German dub is out of sync with the picture, the subtitles no longer match what
is spoken, and the audio description talks over a line that moved.

The hard part of a global release is not making those artefacts. It is knowing,
at any moment, **which of them are currently good**. Today that lives in
spreadsheets and email, and territories get held back because nobody can prove
they are ready in time.

## What it does

Continuity treats every derivative asset as a running service and the release
itself as a system with an SLO. Lip-sync drift, subtitle reading rate, programme
loudness, audio-description collisions, technical conformance, rights clearance
and asset staleness all become live Prometheus series in Grafana Cloud.

The release verdict for each market is a **Grafana recording rule**:

```promql
market_release_ready =
      market_requirements_all_met      # everything measured, passed
    * market_coverage_complete         # everything owed, was measured
    * (1 - market_has_stale_assets)    # nothing built from a stale parent
```

When a market goes red, **Grafana calls the agents** — not the other way round.
An alert fires, POSTs to a Cloud Run receiver, and a crew of specialists wakes:
one per failing dimension, working in parallel. They investigate through the
Grafana MCP server, form a falsifiable prediction, take the smallest corrective
action against real media, and re-run the query that condemned the asset.

Then it assembles the thing a market actually receives: one MP4 with the
picture, the dubbed audio, the audio description as a selectable track with the
accessibility flag set, and language-tagged subtitles — plus a manifest naming
every ingredient by hash.

## How Grafana is used

Not as a dashboard. As the control loop.

1. **Grafana computes the verdict.** `market_release_ready` is a Mimir
   recording rule with promtool unit tests. No agent computes it.
2. **Grafana initiates the work.** A Grafana-managed alert fires on
   `market_release_ready < 1` and POSTs to Cloud Run. The agents do not poll.
3. **Grafana is the agents' only interface to truth.** They read it through
   `mcp-grafana` started with `--disable-write`, which registers zero
   create/update/delete tools. The read-only guarantee is enforced by the
   process arguments, not by a prompt.
4. **Thresholds are published as metrics**, so the verdict is a join between
   two independently published facts. An agent that wanted to pass a check
   would have to write to Mimir, and it holds no credential that can.
5. **Grafana monitors the agents themselves** under the OTel GenAI semantic
   conventions — token usage, tool calls, and guardrail refusals as
   `continuity_agent_rejections_total{reason}`, republished from an append-only
   log so the series never ages out.

Three dashboards are generated from the metric registry and provisioned in one
command.

## What makes it non-obvious

**A check nobody ran blocks exactly as hard as one that failed.**
`market_coverage_complete` demands that the number of requirements measured
equals the number the market owes. Most systems of this shape go quiet when
they break, because absent data reads as absent problems. This one goes red.

That gate caught something during the build that no test did: every market
profile declared a `semantic_fidelity_floor`, and nothing in the pipeline
produced that score — so every market permanently owed a check that could never
be measured, and the verdict was **unsatisfiable for every market on earth**.
Repairs ran, measurements improved, no check was failing, and the board stayed
red with nothing to point at. The gate was right. The requirement was fiction.

**Being right is not the same as being right for the right reason.** Every
repair carries a falsifiable prediction — which series, which direction, past
which value. Afterwards the outcome is recorded `succeeded`, `lucky` (it passed
but the prediction did not hold) or `failed`. `lucky` raises the denominator
and not the numerator, so a strategy cannot earn autonomy by coincidence.

**Authority is the tool list, not the prompt.** The compliance specialist
cannot propose a repair — not because it is instructed not to, but because
`propose_repair` is absent from the tools it is given. A right that was never
cleared is not a defect in a file, and this agent has no mechanism for
pretending otherwise. It is the same guarantee as `--disable-write`, one level
up.

**Lineage is the scheduler.** The package records the dub stem as a parent, so
repairing the stem invalidates the package. The order in which repairs are
applied is not a rule someone wrote; it is read out of the asset graph.

## How we built it

- **Google Cloud** — Vertex AI (Gemini 2.5 Flash for reasoning, adaptation and
  TTS) under a service account with no consumer API key in the deployed path;
  Cloud Run for both services; Cloud Build and Artifact Registry; Cloud Storage;
  Pub/Sub enabled.
- **Agent Development Kit** runs the Conductor's tool loop. The runtime is
  ADK's; the authority is `agents/contracts.py`.
- **Grafana Cloud** — Mimir for metrics and the ruler, Grafana-managed alerting,
  `mcp-grafana` bundled into the image so the read-only boundary survives into
  production.
- **ffmpeg** produces every number that can block a release. No model produces
  a value that gates the verdict.
- Two Cloud Run services from **one image at one digest** with a different
  argument, so a repair started by an alert and one approved by an operator are
  provably the same code.

## Challenges

The honest ones, all still visible in the repository history:

**The measurement was blind to direction.** `dub_sync_offset_ms` is a
magnitude, deliberately — a signed value under a `<= 120 ms` check would let a
dub running 400 ms *early* pass. But `abs()` on the first line threw the sign
away, so RETIME was choosing a shift direction by coin flip. On real audio it
took a stem 187 ms early and shifted it a further 188 ms earlier, landing at
375 ms. Three consecutive repairs recorded themselves `failed`, verification
caught every one, and nothing shipped. That is the safety net working — and not
a substitute for the loop converging. The fix publishes a signed diagnostic
alongside the magnitude.

**A rebuild silently reverted a repair.** Two repairs took the German stem to
v3; a later pipeline re-run passed `version=1`, as a build stage naturally
does, and `record` wrote it. The index pointed back at unrepaired audio, the
board reported a fault the system had already fixed, and the only evidence it
ever happened was an orphaned QC report nothing referenced. Nothing errored.
Versions are the store's to assign now.

**Two assets writing the same series.** The audio description track and the dub
stem both carried `audio_loudness_lufs` under identical labels, so the loudness
check evaluated whichever was written last and the verdict flapped with nothing
changing.

## What's next

Pub/Sub is enabled but not yet in the path — the dispatcher is in-process, with
real dedup semantics but not durable ones. Meaning-preservation is deliberately
unjudged: the only machine version is a model scoring its own back-translation,
and a model-derived number that blocks a release would break the guarantee the
whole design rests on. `docs/LIMITATIONS.md` states both, and everything else
we know to be true.

## Try it

Open the control room, pick a blocked market, sign in with the demo token in
the README, and press **Investigate now**. One specialist wakes per failing
dimension; every tool call streams as it happens. The token runs investigations
and cannot approve a repair.

Two of five markets currently ship. The other three tell you exactly what they
are waiting for.
