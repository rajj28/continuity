# Project gallery

Fifteen images, in the order to upload them to Devpost. All 1800x1200 (3:2),
captured from the running control room at 2x rather than pulled out of the
demo video.

Regenerate the first thirteen with the control room running locally:

    python ui/server.py --port 8090
    python scripts/gallery.py

| # | File | Caption to paste |
|---|---|---|
| 1 | `01-control-room.png` | The control room: five markets, eight dimensions, 87 checks, one screen |
| 2 | `02-new-release.png` | Drop a master and its dialogue list, pick your markets, and seven real pipeline stages run |
| 3 | `03-readiness.png` | One pip per check — solid passed, hatched failed, hollow never measured. A gap is not a pass |
| 4 | `04-verdict.png` | The verdict is a Grafana recording rule, not this screen's opinion — with the PromQL behind every factor |
| 5 | `05-outputs.png` | What the agents made, playable in the browser: the original, the dub, the described track, the deliverable |
| 6 | `06-compliance.png` | The dimensions no agent may touch: a right never granted, a window that opens on the 15th, a certificate nobody submitted |
| 7 | `07-lineage.png` | Every asset with the hash of the version it was built against — which is what makes staleness a fact |
| 8 | `08-blast-radius.png` | What a change here would invalidate, read out of the asset graph rather than out of traces |
| 9 | `09-proposal.png` | A proposal awaiting a human: the prediction it must land, the authority it has not yet earned, the metrics it cited |
| 10 | `10-ledger.png` | The ledger keeps the misses next to the hits. The top line improved the audio by 4.5 dB and is recorded as failed |
| 11 | `11-autonomy.png` | Authority is earned per strategy per market, read from Prometheus at decision time — and guardrail refusals are a time series |
| 12 | `12-architecture.png` | How it works: ingest, build, measure, judge, repair — and the return path that makes everything downstream stale |
| 13 | `13-alert-received.png` | Grafana calling the agents: the deployed receiver's own log, three markets woken in the same second |
| 14 | `14-grafana-verdict.png` | Grafana's own view of the verdict — the control room is a reading of this, not a replacement for it |
| 15 | `15-grafana-agents.png` | Grafana watching the agents themselves: tool calls, how each run ended, guardrail refusals, tokens per call |
