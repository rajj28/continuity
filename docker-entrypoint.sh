#!/bin/sh
# Start the read-only MCP server, then the requested service.
#
# mcp-grafana runs alongside rather than being replaced by direct HTTP calls,
# because --disable-write is what makes the read-only boundary real: it
# registers zero create/update/patch/delete tools, so a write is absent from
# the protocol rather than refused at runtime. A deployment that dropped it
# would keep the claim and lose the guarantee.
set -e

if [ -n "$GRAFANA_URL" ] && [ -n "$GRAFANA_SERVICE_ACCOUNT_TOKEN" ]; then
  echo "starting mcp-grafana (read-only) on :8000"
  mcp-grafana -t streamable-http --disable-write --log-level warn &
  # The proxied Tempo tools are discovered after the handshake returns, so a
  # client that connects instantly can legitimately see an empty tool list.
  sleep 3
else
  echo "GRAFANA_URL or token unset -- MCP not started; reads will fail" >&2
fi

# Keep Grafana's picture of the world current.
#
# Prometheus marks a series stale five minutes after its last sample, so the
# measurements published by a pipeline run evaporate shortly after it ends and
# every market reads as never-measured -- correctly, by the coverage gate, and
# uselessly, for anyone who opens the control room an hour later. The exporter
# republishes what is already on disk in the content-addressed store. It
# invents nothing: no QC report, no series.
#
# EXACTLY ONE process may do this. Two publishers put two samples under two
# `instance` ids into the same series, which makes `market_threshold`
# ambiguous on the `on (market)` joins, which fails every scene rule and takes
# the verdict dark -- a failure that looks like a Grafana outage and is not.
# So it is opt-in per service, and deploy/cloudrun.py sets it on one.
if [ "$RUN_STATE_EXPORTER" = "1" ]; then
  echo "starting state exporter (republishing the store to Grafana)"
  python -m telemetry.exporters.state --store out/store &
fi

case "$1" in
  wake)    exec python -m agents.wake ;;
  control) exec python ui/server.py --host 0.0.0.0 ;;
  *)       exec "$@" ;;
esac
