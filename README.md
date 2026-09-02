# Continuity

**A global release autopilot for media.** One question, answered continuously
for every market, with evidence: *can we release it?*

Continuity treats every derivative media asset -- dubbed stem, subtitle file,
caption, package -- as a running service, and the release itself as a system
with an SLO. Lip-sync drift, subtitle reading rate, programme loudness and
asset staleness become live Prometheus series. The dependency graph is emitted
as OpenTelemetry traces. And the release verdict for each market is a Grafana
recording rule with an alert on top -- which means **Grafana computes the
verdict, and no model can talk it green.**

When a market goes red, Grafana calls the agents, not the other way round. They
investigate through the Grafana MCP server, correlate the evidence, compute the
blast radius, take the smallest safe corrective action against real media, then
re-run the query that condemned the asset in the first place. If the metric did
not move, the repair failed and the plan is revised.

> Status: in active development for the Agentic Cinema hackathon (Grafana Labs
> track). See `docs/LIMITATIONS.md` for what is and is not real yet.

## What works today

The deterministic QC layer -- the measurements that block a release:

```bash
python -m pytest tests/ -q          # 12 passing
./scripts/make_fixtures.sh          # regenerate fixtures from ffmpeg
```

Every number is produced by ffmpeg and parsed deterministically. No model
produces a value that can block a release.

**Models propose. Measurements dispose.**

## Requirements

- Python 3.11+
- ffmpeg 7+ on PATH (needs `silencedetect`, `loudnorm`)

## License

Apache-2.0. See `LICENSE`.
