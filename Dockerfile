# One image, two services: the wake receiver and the control room.
#
# They are one image because they are one system. Both read Grafana through the
# same Signal, both load the same contracts, and a repair started by an alert
# has to behave identically to one approved by an operator. Two images would be
# two chances for that to stop being true.
#
# CMD picks which one runs; deploy/cloudrun.py passes it.

FROM python:3.11-slim

# ffmpeg is not optional. Every number this system blocks a release on comes
# out of it, so a probe that cannot find it must fail at build time here rather
# than at 2am in a Cloud Run log.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version > /dev/null && ffprobe -version > /dev/null

# The MCP boundary has to survive into production. The agents reach Grafana
# only through mcp-grafana started with --disable-write, which registers ZERO
# create/update/patch/delete tools -- the read-only guarantee is enforced by
# the process arguments rather than by a prompt. Running it as a sidecar in
# this container keeps that true when deployed, instead of quietly falling back
# to direct HTTP and leaving the claim in a README.
ARG MCP_GRAFANA_VERSION=v1.3.0
RUN curl -fsSL -o /tmp/mcp.tar.gz \
      "https://github.com/grafana/mcp-grafana/releases/download/${MCP_GRAFANA_VERSION}/mcp-grafana_Linux_x86_64.tar.gz" \
 && tar -xzf /tmp/mcp.tar.gz -C /usr/local/bin mcp-grafana \
 && rm /tmp/mcp.tar.gz \
 && chmod +x /usr/local/bin/mcp-grafana \
 && /usr/local/bin/mcp-grafana --help > /dev/null 2>&1 || true

WORKDIR /app

COPY requirements.txt .
# Installed in one command so pip resolves them together: google-adk pulls an
# older opentelemetry-sdk than the OTLP exporter needs, and installing in two
# passes silently leaves the wrong one in place.
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/ agents/
COPY media/ media/
COPY telemetry/ telemetry/
COPY grafana/ grafana/
COPY scripts/ scripts/
COPY ui/ ui/
COPY assets/market_profiles.json assets/rights_ledger.json assets/certificates.json assets/
COPY docker-entrypoint.sh /usr/local/bin/

# The small half of the store: the asset index, QC reports, the autonomy ledger
# and the localised metadata. Together a few hundred kilobytes, and enough for
# the control room to show real lineage and a real repair history.
#
# NOT the 240 MB of content-addressed media. A container is the wrong place for
# a film, and the hosted control room is a read-and-investigate surface: it can
# watch the agent reason against live Grafana, and it says plainly that
# approving a repair needs the media, which lives where the pipeline runs.
COPY out/store/ out/store/

# The one scene the technical checks probe, at 3.7 MB. Everything else the
# state exporter republishes is already written down in a QC report; the
# technical dimension is the exception, because "is this the right frame rate"
# is a question you answer by looking at the file. Without it those four checks
# would read as never-measured, and the coverage gate would block every market
# for a reason that was an artefact of how the image was packed.
COPY out/scenes/S03.mp4 out/scenes/
COPY out/scenes/manifest.json out/scenes/

# What the agents made. About 30 MB of dubs, described tracks, subtitles and
# packaged deliverables, so the deployed control room can play them rather than
# list four files it cannot open. Everything else on that screen is a
# representation of the work -- a pip, a number, a hash; this is the work.
COPY out/dub/ out/dub/
COPY out/ad/ out/ad/
COPY out/subs/ out/subs/
COPY out/deliverables/ out/deliverables/

# The screenshots, served at /img/<name>.png from this service's own origin.
COPY docs/img/ docs/img/
COPY docs/gallery/ docs/gallery/

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080 \
    MCP_GRAFANA_URL=http://127.0.0.1:8000/mcp

EXPOSE 8080
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["wake"]
