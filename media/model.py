"""One place that decides how this system talks to Gemini.

Two ways in, and the difference matters more than it looks:

    Vertex AI      authenticated as a service account through Application
                   Default Credentials, billed to a project, no consumer API
                   key anywhere. This is the enterprise product and the one a
                   release system would actually run on.

    Gemini API     a consumer API key. Free tier, hard per-day caps, and a
                   credential that is a bearer token in a text file.

The whole pipeline was built against the second because every Google Cloud
billing account available to this project was closed. It now runs on the first,
and the switch is one environment variable rather than an edit in nine modules
-- which is the reason this file exists at all.

## Model names are not portable

Vertex and the Gemini API do not serve the same catalogue. `gemini-3.5-flash`
answers on the consumer API and 404s on Vertex; TTS is `gemini-2.5-flash-tts`
on Vertex and `gemini-2.5-flash-preview-tts` on the API. Callers therefore ask
for a ROLE -- "the text model", "the speech model" -- and this module resolves
it for whichever backend is live. A caller that hard-coded a model name would
work on one backend and 404 on the other, which is exactly the kind of failure
that surfaces during a demo.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("continuity.model")

ROOT = Path(__file__).resolve().parents[1]

# Roles the pipeline asks for, resolved per backend.
#
#   text     translation, adaptation, storefront copy, the Conductor's reasoning
#   speech   dubbing and audio description
#   vision   watching the picture -- audio description, forced narratives
VERTEX_MODELS = {
    "text": "gemini-2.5-flash",
    "speech": "gemini-2.5-flash-tts",
    "vision": "gemini-2.5-flash",
}
API_MODELS = {
    "text": "gemini-3.5-flash",
    "speech": "gemini-3.1-flash-tts-preview",
    "vision": "gemini-3.5-flash",
}

# Vertex regions differ in what they serve. us-central1 carries the whole set
# this pipeline needs; asia-south1 is closer but does not.
DEFAULT_LOCATION = "us-central1"


class ModelError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    from telemetry.otel import load_env
    return load_env()


def using_vertex(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else _env()
    return bool(env.get("GCP_PROJECT_ID")) and bool(
        env.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or env.get("GCP_USE_ADC")
    )


def model_for(role: str, env: dict[str, str] | None = None) -> str:
    """The model name for a role, on whichever backend is configured."""
    env = env if env is not None else _env()
    table = VERTEX_MODELS if using_vertex(env) else API_MODELS
    if role not in table:
        raise ModelError(f"no model for role {role!r}; known: {sorted(table)}")
    return table[role]


@lru_cache(maxsize=2)
def _client(vertex: bool, project: str, location: str, key: str):
    from google import genai
    if vertex:
        return genai.Client(vertexai=True, project=project, location=location)
    return genai.Client(api_key=key)


def client(env: dict[str, str] | None = None):
    """The configured Gemini client. Cached: the SDK object is reusable and
    building one per call adds a credential refresh to every request."""
    env = env if env is not None else _env()
    if using_vertex(env):
        credentials = (
            env.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        )
        if credentials:
            # Set for the SDK's own ADC lookup, which reads the process
            # environment rather than our .env.local.
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
                Path(credentials) if Path(credentials).is_absolute()
                else ROOT / credentials
            )
        return _client(True, env["GCP_PROJECT_ID"],
                       env.get("GCP_VERTEX_LOCATION", DEFAULT_LOCATION), "")

    key = env.get("GEMINI_API_KEY", "")
    if not key:
        raise ModelError(
            "no model backend configured. Either set GCP_PROJECT_ID and "
            "GOOGLE_APPLICATION_CREDENTIALS for Vertex AI, or GEMINI_API_KEY "
            "for the Gemini API."
        )
    return _client(False, "", "", key)


def describe(env: dict[str, str] | None = None) -> str:
    """One line naming the backend, for logs and for the demo runbook."""
    env = env if env is not None else _env()
    if using_vertex(env):
        return (f"Vertex AI · project {env['GCP_PROJECT_ID']} · "
                f"{env.get('GCP_VERTEX_LOCATION', DEFAULT_LOCATION)} · "
                f"service-account credentials")
    return "Gemini API · consumer key · free-tier daily caps apply"
