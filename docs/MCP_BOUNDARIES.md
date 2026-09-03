# MCP boundaries

Verified against a live Grafana Cloud stack on 3 September 2026 with
`mcp-grafana v1.3.0`. Re-run `scripts/mcp_probe.py` after any upgrade; these
notes are observations, not assumptions.

## Servers actually in play

| Surface | Transport / auth | Verified |
|---|---|---|
| `grafana/mcp-grafana` v1.3.0, investigator | streamable-http, caller bearer via `-server-auth-token`; Grafana creds held server-side | yes -- 57 tools |
| `grafana/mcp-grafana` v1.3.0, scribe | same, write enabled, narrow role | pending |
| Grafana Cloud Tempo MCP | `https://<tempo-host>/tempo/api/mcp`, HTTP Basic | yes -- 9 tools |
| Hosted `mcp.grafana.com` | OAuth 2.1, browser | operator path only, not critical path |

## Finding: mcp-grafana proxies the Tempo MCP server automatically

v1.3.0 discovers Tempo from the Grafana datasource list and re-exports its
tools under a `tempo_` prefix:

    tempo_traceql-search          tempo_get-trace
    tempo_traceql-metrics-instant tempo_get-attribute-names
    tempo_traceql-metrics-range   tempo_get-attribute-values
    tempo_trace-diff              tempo_docs-traceql / tempo_docs-config

Consequence: **one endpoint serves both the Grafana tools and the TraceQL
lineage tools.** The Signal Agent needs one connection, not two. We keep the
direct Tempo endpoint configured as a fallback in case proxying is disabled or
the datasource is renamed.

`tempo_trace-diff` was not in the published tool list we designed against. It
compares two complete traces and returns span-level differences -- which is
exactly the artifact we want for a failed-repair-versus-successful-repair
comparison. Adopted into the evidence trail.

## Finding: proxied tool registration is asynchronous

`initialize` returns before proxied discovery completes; tool registration
took ~6 s against this stack and arrives as a `notifications/tools/list_changed`
frame. A client that calls `tools/list` immediately gets **zero tools**.

Two consequences for our agents:

1. Allow the connection to settle before listing tools, and treat an empty
   tool list as "not ready", never as "no tools".
2. `tools/list` responses arrive as `text/event-stream` with **several frames**
   -- the notification first, then the response. Parse by matching the JSON-RPC
   `id`; taking the first `data:` frame yields the notification and looks like
   an empty server. This cost us a debugging cycle; `scripts/mcp_probe.py`
   documents the correct handling.

## Finding: `--disable-write` is enforced by the process

With `--disable-write --disable-admin --disable-provisioning --disable-oncall`,
zero `create_*` / `update_*` / `patch_*` / `delete_*` tools are registered --
verified by counting the tool list, not by reading the docs. The read-only
boundary for the Signal Agent is therefore structural: it is not a prompt
instruction the model could be argued out of.

## Tools we deliberately do not call

ClickHouse, Athena, Snowflake, CloudWatch, Elasticsearch, InfluxDB, Graphite,
Quickwit -- we operate no such datasource. OnCall -- we run no rota (disabled).
Admin and Provisioning -- not ours to touch (disabled). `grafana_api_request`
is available on the investigator but unused: it is an arbitrary-API escape
hatch, and routing decisions through it would defeat the point of auditing
which typed tool produced which piece of evidence.

## Useful flags found on the binary

- `-server-auth-token` -- static caller bearer. Removes the need to mint
  short-lived Google ID tokens for ADK's header block on Cloud Run.
- `-include-args-in-spans` -- the server emits its own OTel spans including
  tool arguments. Our `mcp.tool/*` evidence spans come from the server itself.
  Non-production only, per the flag's own warning.
- `-metrics` -- Prometheus endpoint on the server, so MCP call counts are
  scrapeable without instrumenting the client.
- `-loki-guardrail-mode` -- query cost guardrail; set to `enforce` before the
  demo so a runaway LogQL query cannot burn the free-tier budget.

## Verified argument conventions (3 Sep 2026)

Tool argument names are not guessable and are not uniform. Use
`python scripts/mcp_probe.py grafana schema <tool>` rather than assuming:

- Grafana-native tools use **camelCase**: `datasourceUid`, `queryType`,
  `endTime`, `startTime`, `stepSeconds`.
- Proxied Tempo tools keep their **snake_case** originals: `trace_id`, not
  `traceId` -- while still requiring the camelCase `datasourceUid` that
  mcp-grafana injects. Both conventions appear in one call.
- `query_prometheus` **requires `endTime`** even for an instant query; omitting
  it fails with a time-parse error rather than defaulting to now.

## Verified: OTel to Prometheus metric naming

The conversion appends a full-word unit suffix, which silently renames series:

| instrument | unit | series in Mimir |
|---|---|---|
| `nt_offset` | `ms` | `nt_offset_milliseconds` |
| `nt_ready` | `"1"` | `nt_ready_ratio` |
| `nt_stale` | `""` | `nt_stale` |

Continuity therefore bakes units into metric names and sets `unit=""`, so the
names in `telemetry/metrics.py` are exactly the names the recording rules,
dashboards and the agent's PromQL reference. See that module for the rationale.
