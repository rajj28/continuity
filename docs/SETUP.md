# Setup

Four things must exist before the cloud layer can be built. Steps 1 and 4 are
the Day-1 blockers.

## 1. Google Cloud application-default credentials

The Python client libraries need ADC, which `gcloud auth login` does not
provide. Run this yourself -- it opens a browser:

    gcloud auth application-default login
    gcloud config set project <YOUR_PROJECT_ID>

Confirm billing is enabled on the project, then enable the APIs:

    gcloud services enable \
      aiplatform.googleapis.com texttospeech.googleapis.com \
      speech.googleapis.com translate.googleapis.com \
      run.googleapis.com pubsub.googleapis.com \
      storage.googleapis.com firestore.googleapis.com \
      secretmanager.googleapis.com

## 2. Grafana Cloud stack

Free tier is sufficient: 10k active series, 50 GB logs, 50 GB traces,
14-day retention. Sign up at grafana.com/products/cloud, then collect:

- the stack URL (`https://<stack>.grafana.net`)
- two service accounts and tokens:
  - **investigator** -- Viewer role
  - **scribe** -- custom role: annotations write, incidents write, silences
    write, and dashboards write scoped to a `Continuity/Evidence` folder only
- Tempo user id + token (Traces MCP uses HTTP Basic)
- the OTLP endpoint and Prometheus remote-write endpoint with their credentials

Put all of it in `.env.local`. That file is gitignored; keep it that way.

## 3. Local toolchain

Verified present on this machine: Python 3.11.9, Node 25, ffmpeg 8.0.1 (full
build), gcloud 571, Docker 29, git.

    python -m pip install -r requirements-dev.txt
    python -m pytest tests/ -q

## 4. Source media and its rights

**Blocking, and non-negotiable.** Rights provenance is a theme of this product;
shipping it on footage we cannot document would be indefensible.

Pick one:
- a Blender Foundation open movie (Sintel, Tears of Steel, Big Buck Bunny) --
  CC-BY, well documented, and they have real dialogue to dub
- footage shot for this project

Record the source, licence, licence URL and retrieval date for every asset in
`assets/SOURCES.md` before any of it enters the pipeline.
