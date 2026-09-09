# Devpost submission — every field, ready to paste

Track: **Grafana Labs**. Everything below is copy-paste. Image URLs point at
the public repo, so they render on Devpost without uploading anything twice.

---

## 1 · Project name  *(60 char limit)*

```
Continuity — Global Release Autopilot
```

## 2 · Elevator pitch  *(200 char limit)*

```
Grafana decides whether a film can ship in each territory. When it says no it wakes a crew of Gemini agents — and marks them on whether their prediction held, not on whether they got away with it.
```

Alternate, if you prefer leading with the outcome:

```
One master in, a market-ready release out for every territory. Grafana computes the verdict, wakes a crew of Gemini agents when a market fails, and grades every repair against its own prediction.
```

## 3 · Thumbnail

Upload `docs/img/thumbnail.png` — 1500×1000, exactly 3:2.

## 4 · Video demo link

```
https://youtu.be/J3jybSyzpRE
```

## 5 · "Try it out" links

| Label | URL |
|---|---|
| Live control room | `https://continuity-control-z6txmgck2a-el.a.run.app` |
| Source (Apache-2.0) | `https://github.com/rajj28/continuity` |
| Architecture, one page | `https://continuity-control-z6txmgck2a-el.a.run.app/architecture` |
| Grafana calling the agents | `https://continuity-control-z6txmgck2a-el.a.run.app/wakelog` |

## 6 · Built with  *(24 tags)*

```
google-cloud, vertex-ai, gemini, gemini-2.5-flash, gemini-tts,
agent-development-kit, cloud-run, cloud-build, artifact-registry,
grafana, grafana-cloud, mimir, prometheus, promql,
model-context-protocol, mcp-grafana, opentelemetry, python, ffmpeg,
server-sent-events, docker, pytest, javascript, apache-2.0
```

## 7 · Image gallery  *(15 images, `docs/gallery/`)*

All 1800×1200 (3:2), captured from the running control room at 2x — not frames
pulled out of the video. Upload in this order; captions are in
`docs/gallery/README.md`.

```
01-control-room     02-new-release      03-readiness        04-verdict
05-outputs          06-compliance       07-lineage          08-blast-radius
09-proposal         10-ledger           11-autonomy         12-architecture
13-alert-received   14-grafana-verdict  15-grafana-agents
```

## 8 · The four short answers

**Please provide a URL to your open source code repository. Must include OSI license**

```
https://github.com/rajj28/continuity
```

Apache-2.0, detected by GitHub and visible in the About panel.

**Provide a URL to the hosted Project for judging and testing**

```
https://continuity-control-z6txmgck2a-el.a.run.app
```

**What Google Cloud products did you use in this project?**

```
Vertex AI (Gemini 2.5 Flash for dialogue adaptation and agent reasoning;
Gemini TTS for the dub and audio description) · Agent Development Kit (ADK)
· Cloud Run (two services from one image at one digest: the control room and
the alert receiver) · Cloud Build · Artifact Registry · Cloud Logging ·
Cloud Storage · Pub/Sub (enabled) · IAM service accounts with Application
Default Credentials — no consumer API key anywhere in the deployed path
```

**Please list all other tools or products you used in your project**

```
Grafana Cloud — Mimir (metrics, thresholds and the market_release_ready
recording rule, promtool-tested), Grafana-managed Alerting, Grafana
dashboards · mcp-grafana, run with --disable-write as the agents' only
interface to Grafana · Model Context Protocol · Prometheus remote write ·
PromQL · OpenTelemetry, GenAI semantic conventions · ffmpeg and ffprobe —
every number that can block a release · Python 3.11 · pytest (339 tests) ·
Docker · Chrome DevTools Protocol (the demo was shot headlessly by
scripts/record.py) · ElevenLabs (voiceover for the demo video only — no part
of the product) · Sintel, © Blender Foundation, CC BY 3.0
```

---

# 9 · Project story — paste everything below this line

![Continuity system diagram](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/system-diagram.png)

**A film does not ship to the world once. It ships fifty times.**

Germany needs a German dub, German subtitles, an FSK certificate, an audio
description track, a German storefront page, and a package built to a specific
technical spec. Japan needs all of that again, differently — including a much
tighter subtitle reading rate, because Japanese characters take longer to read.
Brazil needs a DEJUS classification that nobody can hurry.

Every one of those is a separate artefact that can be wrong, and every one can
be made wrong again by a change upstream. Someone re-cuts a scene, and now the
German dub is out of sync with the picture, the subtitles no longer match what
is spoken, and the audio description talks over a line that moved.

The hard part of a global release is not making those artefacts. It is knowing,
at any moment, **which of them are currently good**. Today that lives in
spreadsheets and email, checked by hand, one file at a time — and the checking
takes longer than the making.

**Continuity turns that question into a Grafana recording rule, and gives a
crew of Gemini agents the job of turning it green.**

### 🎬 Watch this (5:30)

| | |
|---|---|
| **0:37** | A master and its dialogue list go in; seven real pipeline stages run, streaming their own output |
| **1:12** | **Listen** — the original, the German dub and the described track, playing. Not a description of a dub |
| **1:37** | Not a dubbing tool: 87 checks across 8 dimensions, named one column at a time |
| **2:09** | Grafana computes the verdict, and nothing in the system can talk it green |
| **2:59** | The alert answering — the deployed receiver's own log, three markets woken in one second |
| **3:32** | **A repair, approved on camera, that improves the number and is recorded as a failure** |
| **4:11** | The dimensions no agent may touch, and why each one is red |
| **4:47** | The wiring |

---

## What it does

**You give it a video and a list of what is said in it.**

Drop a master and its dialogue list on the control room, choose your markets,
and seven stages run — ingest, dub, subtitles, audio description, storefront
record, release check, package. They stream their own output as they go,
because they *are* the scripts in the repository, started as processes. Nothing
was reimplemented to make a screen move.

![A build running](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/02-new-release.png)

**And you can play what came out.** Every produced file is served by asset id
out of a content-addressed store: the scene as delivered, the dub, the
described track, and the packaged deliverable with all of it muxed together.

![What the agents made](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/05-outputs.png)

A dub you cannot hear is indistinguishable from one that was never made. That
is why the demo video plays them at 1:12 rather than describing them.

## It is not a dubbing tool

87 checks, in 8 dimensions, for every market:

| Dimension | What it checks |
|---|---|
| **Localisation** | dub sync against picture, lines overrunning their slots, delivery pace, meaning preserved |
| **Audio** | integrated loudness against the territory's own target, true-peak ceiling |
| **Timed text** | subtitle reading rate |
| **Accessibility** | description colliding with dialogue, description coverage |
| **Technical** | resolution, frame rate, video codec, pixel format, channel configuration, sample rate |
| **Rights** | territory grants and their windows |
| **Certification** | ratings body, state, and whether the certificate was granted against *this* cut |
| **Packaging** | required deliverables complete, storefront record localised, forced narratives present |

![The control room](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/03-readiness.png)

Those are the ones it runs today. The same shape extends to the rest of what a
real delivery is checked against — scan type and active picture, colour
primaries and HDR metadata, photosensitive-epilepsy compliance, channel mapping
and M&E stems, subtitle line length and cue gaps, SDH and CEA-608/708 captions,
music cue sync and stock footage rights, holdbacks and format rights, content
descriptors and certificate expiry, artwork variants carrying localised text,
IMF conformance and per-platform delivery manifests, territory-specific edits.
Each of those is a threshold published as a metric and a probe that reads a
file. None of them changes the architecture.

**And the dimensions that cannot be repaired say so, and say why.**

![Compliance](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/06-compliance.png)

Japan fails on a right that was never granted, a right whose window does not
open until the 15th, and a certificate nobody has submitted. Germany's
certificate is granted, in date, and against this exact cut — re-cut the film
and it stops counting, by hash. No agent here is given a tool that could
pretend otherwise.

## Grafana is the control loop, not the dashboard

This is the part I would ask a judge to check first.

**1 · Grafana computes the verdict.** Whether a market may ship is a Mimir
recording rule with promtool unit tests. No agent computes it, and no code in
this repository computes it:

```promql
market_release_ready =
      market_requirements_all_met      # everything measured, passed
    * market_coverage_complete         # everything owed, was measured
    * (1 - market_has_stale_assets)    # nothing built on a stale parent
```

![Grafana owns the verdict](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/14-grafana-verdict.png)

**2 · Grafana initiates the work.** A Grafana-managed alert fires on
`market_release_ready < 1` and POSTs to a Cloud Run receiver. The agents do not
poll and have no scheduler. This is the deployed receiver's own log:

![The alert answering](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/13-alert-received.png)

Three markets woken in the same second, and a second rule underneath watching
whether the last repair actually held. Rendered by `scripts/wakelog.py`, which
reads Cloud Logging and refuses to write a page when nothing has fired.

**3 · Grafana is the agents' only interface to truth, and it is read-only by
construction.** They reach it through `mcp-grafana` started with
`--disable-write`, which registers zero create/update/delete tools. The
guarantee is enforced by the process arguments, not by a prompt. The MCP server
is bundled into the container image so the boundary survives into production.

**4 · Thresholds are published as metrics**, so the verdict is a join between
two independently published facts. An agent that wanted to pass a check would
have to write to Mimir — and it holds no credential that can.

**5 · Grafana monitors the agents themselves**, under the OpenTelemetry GenAI
semantic conventions: tool calls, how each run ended, model call duration,
control-loop steps, and guardrail refusals as
`continuity_agent_rejections_total{reason}`.

![Grafana watching the agents](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/15-grafana-agents.png)

The agents are not merely watched *by* this stack. They are watched *in the
same stack they read their instructions from.* Three dashboards are generated
from the metric registry and provisioned in one command.

## Google Cloud, at runtime

Every one of these is imported and called in code, not named in a README:

| Service | Where it is called | What it does |
|---|---|---|
| **Vertex AI — Gemini 2.5 Flash** | `media/model.py`, `agents/conductor.py` | Adapts every line of dialogue to the gap it must land in; runs the specialists' reasoning and tool loop |
| **Vertex AI — Gemini TTS** | `media/dub/`, `media/ad.py` | Speaks the dub and the audio description |
| **Agent Development Kit** | `agents/adk_conductor.py` | Runs the tool loop. The runtime is ADK's; the authority is `agents/contracts.py` |
| **Cloud Run** | `deploy/cloudrun.py` | Two services — the control room and the alert receiver |
| **Cloud Build + Artifact Registry** | `deploy/cloudrun.py` | Build once, resolve the digest, deploy that exact digest twice |

Authentication is a service account with Application Default Credentials; there
is no consumer API key anywhere in the deployed path.

The two Cloud Run services run **one image at one digest with a different
argument**. That is not packaging convenience — it is the guarantee that a
repair started by an alert and a repair approved by an operator are provably
the same code.

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
which value — and afterwards the outcome is recorded `succeeded`, `lucky` (it
passed, but the prediction did not hold) or `failed`. `lucky` raises the
denominator and not the numerator, so a strategy cannot earn autonomy by
coincidence.

![A proposal awaiting a human](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/09-proposal.png)

**The demo shows one of these going wrong, live.** At 3:32 the audio specialist
proposes a REMIX on pt-BR, predicting
`audio_loudness_lufs: -18.78 -> <= -24`. Approved on camera, it runs on the
real audio and lands at **−23.32** — four and a half decibels better than it
started, and short of what it promised. The ledger records it as **failed**.

![The ledger](https://raw.githubusercontent.com/rajj28/continuity/main/docs/img/10-ledger.png)

In the same investigation, the localisation specialist looked at the sync
failure and **refused to touch it**, because `dub_drift_systematic = 0` meant a
retime would not fix it. It escalated instead of trying. Neither of those
outcomes is scripted, and neither is flattering — which is the point.

**Authority is the tool list, not the prompt.** The compliance specialist
cannot propose a repair — not because it is instructed not to, but because
`propose_repair` is absent from the tools it is handed. A right that was never
cleared is not a defect in a file, and this agent has no mechanism for
pretending otherwise. Same guarantee as `--disable-write`, one level up.

**Lineage is the scheduler.** The package records the dub stem as a parent, so
repairing the stem invalidates the package. The order repairs are applied in is
not a rule someone wrote; it is read out of the asset graph.

## How we built it

- **Google Cloud** — Vertex AI (Gemini 2.5 Flash and Gemini TTS) behind a
  service account; Cloud Run for both services; Cloud Build and Artifact
  Registry; Pub/Sub enabled.
- **Agent Development Kit** runs the Conductor's tool loop and the five
  specialists, dispatched in parallel — one per failing dimension.
- **Grafana Cloud** — Mimir for metrics and the ruler, Grafana-managed
  alerting, and `mcp-grafana` bundled into the image so the read-only boundary
  survives deployment.
- **ffmpeg** produces every number that can block a release. No model produces
  a value that gates the verdict.
- **339 tests**, and a content-addressed asset store where staleness is a hash
  comparison rather than a guess.

## Challenges we ran into

The honest ones, all still visible in the repository history.

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
does, and the store wrote it. The index pointed back at unrepaired audio, the
board reported a fault the system had already fixed, and the only evidence it
had ever happened was an orphaned QC report nothing referenced. Nothing
errored. Versions are the store's to assign now, never the caller's to assert.

**Two assets writing the same series.** The audio description track and the dub
stem both carried `audio_loudness_lufs` under identical labels, so the check
evaluated whichever was written last and the verdict flapped with nothing
changing.

**The store was written on one operating system and read on another.** Asset
URIs carried backslashes, and on Linux `out\dub\de-DE\stem.wav` is not three
directories and a file — it is one filename that exists nowhere. The deployed
control room listed no outputs for any market while every byte sat in the
image. Nothing failed; the screen simply said the agents had produced nothing,
which is the one claim this project cannot afford to make wrongly.

## Accomplishments that we're proud of

Two markets currently ship, and they got there through repairs that were
proposed, predicted, executed against real audio, and verified — not through a
fixture. The ledger keeps the failures next to the successes because a system
that hides its misses cannot be trusted about its hits.

And every number in the demo video was measured from a real file. No mock data,
no staged runs, no numbers written by a model.

## What we learned

That the interesting failure mode of an agentic system is not a wrong action —
it is a *confident* one. Almost everything in this design is an answer to that:
predictions that can be falsified, an outcome taxonomy where getting away with
it costs you, authority expressed as a tool list, a verdict computed by
something the agents cannot write to, and a coverage gate so that silence
cannot be mistaken for success.

## What's next

**A series ships fifty times an episode, every week.** The gate is per episode,
but the interesting problem is across them — a glossary and a character's voice
have to hold for a season, and when a shared asset changes, everything built
from it is stale. That is already the model: an asset records its parents'
hashes and is stale when they stop matching. Extending it from scenes to
episodes is data, not architecture.

**A live event ships while it is still going out.** The same gate inside a
latency budget rather than before publication — loudness, caption reading rate
and caption latency on a rolling window, the verdict as a live recording rule,
and the repair as a mixer action rather than a re-render. Grafana is *more*
natural there, not less.

The same shape covers theatrical DCP conformance and ad campaigns, where the
asset count is higher and the dimensions are identical.

Known limits, stated rather than hidden: Pub/Sub is enabled but not yet in the
path — the dispatcher is in-process, with real dedup semantics but not durable
ones. Meaning-preservation is deliberately left unjudged, because the only
machine version available is a model scoring its own back-translation, and a
model-derived number that blocks a release would break the guarantee the whole
design rests on. `docs/LIMITATIONS.md` states both, and everything else we know
to be true.

## Try it yourself

Open the **[control room](https://continuity-control-z6txmgck2a-el.a.run.app)**,
press *Operator sign-in*, and paste:

```
demo-vd18Z5yDsCiBzkVIv9ThJQOz
```

Pick a blocked market and press **Investigate now**. One specialist wakes per
failing dimension and they work in parallel; every tool call streams as it
happens, with the PromQL behind each finding. Watch the compliance specialist
in particular — it is never handed a repair tool, so it can only escalate.

That token runs investigations and **cannot approve a repair**, and the alert
receiver does not accept it at all. It is published deliberately: an
investigation costs model quota and changes no asset.

Then scroll a market's detail for **What the agents made** and press play on
the dub, then the described track, then the packaged deliverable. Below it,
**Compliance** names who has to say yes and whether they have.

Two of five markets currently ship. The other three tell you exactly what they
are waiting for — and one of them is waiting on a regulator, which nothing in
this repository can do anything about.
