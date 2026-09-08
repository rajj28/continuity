# Limitations

Stated plainly, because an undocumented limitation a reviewer discovers costs
more trust than a documented one costs credibility.

The same rule cuts the other way, so this file is maintained rather than
appended to: a limitation that has since been fixed is deleted, not left
standing as false modesty. What follows is true of the system as it runs today.

## Measurement

- **`dub_sync_offset_ms` measures isochrony, not viseme-level lip-sync.** It
  compares utterance boundaries in the dubbed stem against the corresponding
  boundaries in the source master. True lip-sync would require a phoneme
  aligner mapping visemes to mouth shapes. We do not have one and do not
  claim one.
- **Sync tolerance is a simplification.** We use a single symmetric per-market
  tolerance (default 120 ms). Broadcast practice uses asymmetric audio-lead vs
  audio-lag tolerances, because viewers detect early audio far sooner than late
  audio. Implementing the asymmetric form is straightforward future work.
- **`silencedetect` needs a sane noise floor.** The default -35 dBFS suits
  dialogue stems. Material with a high noise floor (production sound, heavy
  room tone) needs the threshold tuned per title, or the voiced-interval
  detection degrades.
- **Utterance alignment is heuristic.** Spans are assigned to reference slots by
  overlap, falling back to the last-started cue and then to the first
  (`media/qc/sync.py:_slot_for`). This replaced alignment by index, which was
  wrong in a specific and instructive way: a French dub that merged two
  utterances shifted every subsequent pairing and reported a 6137 ms offset
  that did not exist. Overlap is right far more often, but it is still a
  heuristic, and a scene whose utterance count disagrees with the reference is
  reported as an anomaly rather than smoothed over.

## Lineage

- **Lineage is authoritative on disk, not in a cloud database.** The
  content-addressed store (`out/store/index`) is the record: every asset
  carries its parents' hashes, and staleness is
  `recorded_parent_hash != parent.current_hash`. That is deliberate — the
  lineage of a build should be a property of the build, not of a database that
  can be unavailable. The cost is that it is per-machine: two pipelines running
  in parallel do not share a view of what exists.
- **Tempo is an investigation surface, not the record.** Traces are subject to
  sampling and to retention (14 days on the Grafana Cloud free tier; TraceQL
  metrics queries are additionally capped at a 24-hour window). When a TraceQL
  cross-check disagrees with the store, that disagreement is surfaced as a
  finding — it usually means a code path is uninstrumented.

## Grafana dependencies in preview

- **Grafana Agent Observability** and its `agento11y_*` MCP tools are in public
  preview and ship disabled by default. The earned-autonomy tier is therefore
  computed from our own `continuity_repairs_total` series rather than read back
  from Grafana's agent telemetry.

## Scope

- Rules for each market are a small versioned ruleset covering delivery and a
  narrow editorial set. A production system would need far broader territory
  coverage, and would source it from a rights system rather than a JSON file.
- **Meaning preservation is not checked at all.** This is the one dimension of
  localisation the system does not judge, and the omission is deliberate.

  It was not always. Every market profile declared a `semantic_fidelity_floor`
  and nothing in the pipeline produced the score, so every market permanently
  owed a check that could never be measured. The coverage gate did exactly what
  it is built to do -- an unmeasured requirement blocks -- which meant
  `market_release_ready` was unsatisfiable for every market. Repairs ran,
  measurements improved, no check was failing, and the board stayed red with
  nothing to point at. The gate was right; the requirement was fiction.

  The fix was not to write the probe. Whether a dubbed line still MEANS what
  the original meant is a judgement, and the only machine version is a model
  scoring its own back-translation. A model-derived number that blocks a
  release would break the guarantee the whole design rests on. So meaning
  belongs to a linguist, and a back-translation score -- if one is ever added
  -- belongs with the diagnostics, which inform a human and gate nothing.

  `tests/test_verdict_rules.py` now asserts that every check a market owes maps
  to a series something actually publishes, so a requirement cannot be declared
  into existence again.
- **Five markets, one title, one scene.** The pipeline is not shaped around
  those numbers — nothing in it knows how many markets there are — but a real
  release has hundreds of the first and thousands of the last, and rate limits
  and cost, not architecture, are what stand between here and there.

## Infrastructure

- **Pub/Sub is enabled but not yet in the path.** `agents/wake.Dispatcher` is
  in-process: real dedup semantics, but not durable ones. If the receiver dies
  mid-repair, that incident is lost until the alert re-delivers — which it
  will, in at most one `repeat_interval`. The interface is the one Pub/Sub fits
  behind, so this is a substitution and not a rewrite. It is listed here
  because "the queue is in memory" is exactly the kind of thing a diagram
  quietly implies is not.
- **Both Cloud Run services run at `max-instances=1`,** because the dedup set
  and the pending-proposal store are per-instance. Making them horizontal is
  the same piece of work as putting Pub/Sub in the path.
- **The hosted control room cannot execute a repair.** It carries the asset
  index, the QC reports and the repair ledger — a few hundred kilobytes — and
  not the 240 MB of content-addressed media, because a container is the wrong
  place for a film. So it reads, it investigates, and it says so plainly when
  asked to approve. Repairs run where the media is.

## Audio description

- **The word budget is calibrated to the synthesiser, not to a human narrator.**
  Broadcast practice puts AD narration near 160 words per minute. Budgeting at
  that rate produced "Sintel runzelt." -- two words -- as 1920 ms of audio in a
  1000 ms gap. Gemini TTS reads narration at roughly 85 wpm once its ~290 ms of
  leading silence is counted, so the budget uses the measured figure. It is
  still an estimate, which is why stage 4 re-writes shorter when the synthesised
  audio overruns rather than trusting the arithmetic.
- **Description quality is not measured, only its fit.** `ad_collision_ms` is a
  fact about audio: does the narration talk over dialogue. Whether it describes
  the *right* thing is an editorial judgement this system does not attempt, and
  a track that scores perfectly on collision may still be a poor description.
  Human AD review is a step, not an optional one.
- **Gaps shorter than 700 ms are skipped entirely.** Sintel S03 has a 350 ms gap
  between two lines that will hold nothing; describing into it would be worse
  than silence.
- **A cue that still will not fit after every rewrite attempt is dropped.** Same
  rule as above, reached from the other end: narration that cannot be said in
  the time available is not narration, and keeping it lays description over the
  dialogue it was written to avoid. Dropping it is visible rather than silent --
  `ad_coverage_ratio` falls, which is a measured statement that this scene is
  less described than it should be, and the right place for a human to decide
  whether to re-cut the gap or accept it.

## Model backend

- **Vertex and the Gemini API do not serve the same model catalogue,** so
  `media/model.py` resolves a *role* ("the speech model") per backend rather
  than letting callers name a model. The deployed path is Vertex under a
  service account. The consumer-API path still exists and is still exercised
  locally, which means a model name can drift on the backend nobody ran today.
