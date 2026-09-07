# The wake receiver and the workers it starts ship as one image.
#
# They are one image because they are one codebase and the repair path needs
# the same QC probes the ingest path does -- a verifier that measures with
# different code than the builder used is a verifier that can disagree with
# itself for reasons no one can debug.

FROM python:3.11-slim

# ffmpeg is not optional. Every number this system blocks a release on comes
# out of it, so a probe that cannot find it must fail at build time here rather
# than at 2am in a Cloud Run log.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version > /dev/null && ffprobe -version > /dev/null

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/ agents/
COPY media/ media/
COPY telemetry/ telemetry/
COPY grafana/rules/ grafana/rules/
COPY assets/market_profiles.json assets/

# Test fixtures and the Sintel master are deliberately absent. The master is
# 224 MB and lives in GCS; the fixtures are for the test suite and have no
# business in a production image where they could be mistaken for real assets.

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

# Cloud Run overrides PORT; wake.py reads it.
EXPOSE 8080

CMD ["python", "-m", "agents.wake"]
