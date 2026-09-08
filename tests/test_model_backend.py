"""Vertex must be reachable where it actually runs.

This file exists because the hosted control room's Investigate button had never
worked, and could not have. `using_vertex` required a key file to conclude that
a Vertex backend was configured -- but on Cloud Run there is no key file, and
there deliberately should not be one: the service runs AS a service account and
Application Default Credentials come from the metadata server. That is the
whole reason no credential is baked into the image.

So the check said "no model backend configured" on precisely the deployment
where Vertex works best. It went unnoticed because every other hosted endpoint
reads Grafana rather than a model, and the one that does not was behind an
operator token nobody had exercised over HTTP.
"""

from __future__ import annotations

import pytest

from media.model import API_MODELS, VERTEX_MODELS, model_for, using_vertex

PROJECT = {"GCP_PROJECT_ID": "grafana-508011"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Nothing here may depend on the developer's own shell."""
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE", "K_REVISION",
                 "FUNCTION_TARGET", "GAE_ENV", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_cloud_run_needs_no_key_file(monkeypatch):
    """The regression. K_SERVICE is set on every Cloud Run revision."""
    monkeypatch.setenv("K_SERVICE", "continuity-control")
    assert using_vertex(PROJECT) is True


@pytest.mark.parametrize("marker", ["K_SERVICE", "K_REVISION",
                                    "FUNCTION_TARGET", "GAE_ENV"])
def test_any_google_compute_marker_implies_adc(monkeypatch, marker):
    monkeypatch.setenv(marker, "x")
    assert using_vertex(PROJECT) is True


def test_a_key_file_still_works(monkeypatch):
    """The laptop path, which was the only one that used to work."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/key.json")
    assert using_vertex(PROJECT) is True


def test_an_explicit_adc_flag_still_works():
    assert using_vertex({**PROJECT, "GCP_USE_ADC": "1"}) is True


def test_no_project_means_no_vertex(monkeypatch):
    """A project id is the one thing Vertex cannot be inferred without."""
    monkeypatch.setenv("K_SERVICE", "continuity-control")
    assert using_vertex({}) is False


def test_a_bare_laptop_falls_back_to_the_consumer_api():
    """No credentials anywhere: the API-key path, and the API catalogue.

    Vertex and the Gemini API do not serve the same models, so getting this
    wrong is a 404 at the moment of use rather than a configuration error.
    """
    assert using_vertex(PROJECT) is False
    assert model_for("text", PROJECT) == API_MODELS["text"]


def test_the_catalogue_follows_the_backend(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "continuity-control")
    for role in ("text", "speech", "vision"):
        assert model_for(role, PROJECT) == VERTEX_MODELS[role]
