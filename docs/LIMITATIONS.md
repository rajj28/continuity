# Limitations

Stated plainly, because an undocumented limitation a reviewer discovers costs
more trust than a documented one costs credibility.

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
- **Utterance alignment is by index.** Legitimate because we generate the dub
  one utterance at a time from the reference, so the counts should match. A
  mismatch is reported as an anomaly rather than being smoothed over, but the
  measurement for that scene should be treated as low-confidence.
- **`silencedetect` needs a sane noise floor.** The default -35 dBFS suits
  dialogue stems. Material with a high noise floor (production sound, heavy
  room tone) needs the threshold tuned per title, or the voiced-interval
  detection degrades.

## Lineage

- **Tempo is the investigation surface, not the record.** Traces are subject to
  sampling and to retention (14 days on the Grafana Cloud free tier; TraceQL
  metrics queries are additionally capped at a 24-hour window). Firestore holds
  the authoritative lineage. When the TraceQL cross-check disagrees with
  Firestore, that disagreement is surfaced as a finding -- it usually means a
  code path is uninstrumented.

## Grafana dependencies in preview

- **Grafana Cloud Traces MCP server** is in public preview; breaking changes
  are possible. Blast radius stays correct without it (Firestore), but the
  TraceQL corroboration and the lineage demo degrade.
- **Grafana Agent Observability** and its `agento11y_*` MCP tools are in public
  preview and ship disabled by default. The earned-autonomy read-back is P2 for
  this reason; the fallback computes success rates from our own
  `continuity_repairs_total` series.

## Scope

- Rules for each market are a small versioned ruleset covering delivery and a
  narrow editorial set. A production system would need far broader territory
  coverage, and would source it from a rights system rather than a JSON file.
- Semantic fidelity is a back-translation score, which detects meaning drift
  but not register or tone drift reliably. It is used to *trigger* human review,
  never to approve a change on its own.

## Infrastructure not yet real

Recorded here rather than worked around, because the alternative is a mock and
a mock in this position would invalidate the one claim the project rests on.

- **Google Cloud billing is closed on every account available to this
  project.** Seven billing accounts across two Google identities, all
  `open: false`; a real Chirp 3 request returns `BILLING_DISABLED`. This blocks
  dub synthesis, Gemini, Pub/Sub and Cloud Run. Nothing is stubbed in their
  place: there are no dub stems, so `dub_sync_offset_ms` is absent, so the
  affected markets have incomplete coverage, so they are **BLOCKED rather than
  green** -- which is exactly what the verdict is supposed to do with a
  measurement it does not have. The limitation is visible in the product
  instead of hidden behind fixtures.
- **The wake-up webhook has no public address yet.** Grafana Cloud can only
  POST to something reachable from the internet. The receiver, its dedup, its
  auth and its handling of Grafana's exact payload are all tested over a real
  socket (`tests/test_wake.py`); what is untested is the hop from Grafana's
  alertmanager to it. That closes with either a tunnel or a Cloud Run URL, and
  `grafana/alerting.py` provisions the routing in one command once there is one.
- **Pub/Sub sits behind the same billing blocker.** `agents/wake.Dispatcher`
  is in-process today: a real queue with real dedup semantics, but not a
  durable one. If the receiver dies mid-repair, that incident is lost until
  the alert re-delivers -- which it will, in at most one `repeat_interval`.
  The interface is the one Pub/Sub fits behind, so this is a substitution and
  not a rewrite.
