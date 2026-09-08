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


# Environment variables Google sets on its own compute. Their presence means
# Application Default Credentials will resolve from the metadata server.
_GOOGLE_COMPUTE = ("K_SERVICE", "K_REVISION", "FUNCTION_TARGET", "GAE_ENV")


def _credentials_available(env: dict[str, str]) -> bool:
    """Whether Vertex will authenticate without us handing it a key file.

    A key file is one way. It is not the good way, and on Cloud Run it is not
    the way at all: the service runs AS a service account and ADC comes from
    the metadata server, which is the entire reason no key is baked into the
    image.

    Requiring GOOGLE_APPLICATION_CREDENTIALS therefore answered "no Vertex
    backend" on exactly the deployment where Vertex works best. The hosted
    control room's Investigate button failed with "no model backend
    configured" while the identical code reasoned happily on a laptop -- and
    it failed only on the one path nobody had exercised over HTTP, because
    every other hosted endpoint reads Grafana rather than a model.
    """
    return bool(
        env.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or env.get("GCP_USE_ADC")
        or any(os.environ.get(name) for name in _GOOGLE_COMPUTE)
    )


def using_vertex(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else _env()
    return bool(env.get("GCP_PROJECT_ID")) and _credentials_available(env)


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


def configure_environment(env: dict[str, str] | None = None) -> None:
    """Export the variables OTHER Google libraries read to find the backend.

    Our own code calls `client()`, but the Agent Development Kit builds its own
    genai client and discovers the backend from the process environment. So the
    choice made here has to be visible there too, or ADK falls back to looking
    for a consumer API key and fails with "No API key was provided" while a
    perfectly good service account sits in the same process.

    Idempotent, and it never overrides something already set: an operator who
    exported GOOGLE_CLOUD_PROJECT meant it.
    """
    env = env if env is not None else _env()
    if not using_vertex(env):
        if env.get("GEMINI_API_KEY"):
            os.environ.setdefault("GOOGLE_API_KEY", env["GEMINI_API_KEY"])
        return
    credentials = (env.get("GOOGLE_APPLICATION_CREDENTIALS")
                   or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
    # Absent on Cloud Run, and correctly so -- ADC comes from the metadata
    # server there. Only resolve a path when one was actually given.
    if credentials:
        path = Path(credentials)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
            path if path.is_absolute() else ROOT / path
        )
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", env["GCP_PROJECT_ID"])
    os.environ.setdefault(
        "GOOGLE_CLOUD_LOCATION",
        env.get("GCP_VERTEX_LOCATION", DEFAULT_LOCATION),
    )


def client(env: dict[str, str] | None = None):
    """The configured Gemini client. Cached: the SDK object is reusable and
    building one per call adds a credential refresh to every request."""
    env = env if env is not None else _env()
    configure_environment(env)
    if using_vertex(env):
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
