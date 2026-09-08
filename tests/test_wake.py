"""The wake-up path, tested against a real Grafana payload.

The fixture in `grafana_payload()` is the body Grafana Cloud actually POSTs for
`MarketNotReleaseReady` -- same field names, same nesting, same fingerprint
semantics. Testing against an invented shape would prove nothing about whether
the loop starts.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agents.wake import Dispatcher, Incident, parse, serve


def grafana_payload(
    *, status: str = "firing", market: str = "de-DE",
    action: str = "investigate", fingerprint: str = "f1de3e",
) -> dict:
    return {
        "receiver": "continuity-conductor",
        "status": status,
        "orgId": 1,
        "externalURL": "https://cordialharbor49.grafana.net/",
        "groupLabels": {"alertname": "MarketNotReleaseReady"},
        "commonLabels": {"alertname": "MarketNotReleaseReady",
                         "severity": "critical"},
        "alerts": [{
            "status": status,
            "fingerprint": fingerprint,
            "startsAt": "2026-09-07T14:31:00Z",
            "generatorURL": "https://cordialharbor49.grafana.net/alerting/grafana/...",
            "valueString": "[ var='A' labels={market=de-DE, title=SINTEL} value=0 ]",
            "labels": {
                "alertname": "MarketNotReleaseReady",
                "market": market,
                "title": "SINTEL",
                "severity": "critical",
                "team": "release_ops",
                "continuity_action": action,
            },
            "annotations": {
                "summary": f"{market} cannot ship SINTEL",
                "description": "Release readiness for SINTEL is 0.",
                "runbook": "https://github.com/rajj28/continuity/blob/main/docs/DEMO_RUNBOOK.md",
            },
        }],
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_an_alert_carries_everything_the_conductor_needs():
    incident, = parse(grafana_payload())
    assert incident.alertname == "MarketNotReleaseReady"
    assert incident.market == "de-DE"
    assert incident.title_id == "SINTEL"
    assert incident.action == "investigate"
    assert incident.firing
    assert incident.known_action
    # the deep link back into Grafana survives, so the UI can show the judge
    # the exact rule that started this
    assert incident.generator_url.startswith("https://")
    assert "value=0" in incident.value_string


def test_a_grouped_batch_becomes_one_incident_per_market():
    """Alertmanager groups alerts; the unit of work is a market, not a group."""
    payload = grafana_payload()
    payload["alerts"].append(
        grafana_payload(market="ja-JP", fingerprint="a91c02")["alerts"][0]
    )
    incidents = parse(payload)
    assert [i.market for i in incidents] == ["de-DE", "ja-JP"]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_a_repeat_delivery_does_not_start_a_second_run():
    """Alertmanager re-sends every repeat_interval until the alert resolves,
    and a repair takes minutes. Without this, the Conductor is restarted on
    work already in flight and the second run investigates the first run's
    half-written asset."""
    started: list[Incident] = []
    d = Dispatcher(started.append)
    incident, = parse(grafana_payload())

    assert d.submit(incident) is True
    assert d.submit(incident) is False
    assert d.submit(incident) is False
    assert len(started) == 1
    assert d.duplicates == 2


def test_resolution_is_new_information_not_a_duplicate():
    """Same fingerprint, different status. The system has to hear that the
    market recovered, or it never closes the incident."""
    started: list[Incident] = []
    d = Dispatcher(started.append)
    firing, = parse(grafana_payload(status="firing"))
    resolved, = parse(grafana_payload(status="resolved"))
    assert firing.fingerprint == resolved.fingerprint

    assert d.submit(firing) is True
    assert d.submit(resolved) is True
    assert [i.status for i in started] == ["firing", "resolved"]


def test_different_markets_are_independent_incidents():
    d = Dispatcher()
    de, = parse(grafana_payload(market="de-DE", fingerprint="f1de3e"))
    jp, = parse(grafana_payload(market="ja-JP", fingerprint="a91c02"))
    assert d.submit(de) is True
    assert d.submit(jp) is True
    assert d.duplicates == 0


def test_an_action_we_do_not_implement_is_recorded_not_guessed():
    """A rule asking for work that does not exist is a deployment mistake.
    Defaulting to `investigate` would hide it; dropping it silently would too."""
    started: list[Incident] = []
    d = Dispatcher(started.append)
    incident, = parse(grafana_payload(action="rewrite_the_film"))
    assert d.submit(incident) is False
    assert started == []
    assert [i.action for i in d.unroutable] == ["rewrite_the_film"]


def test_forget_allows_a_retry_after_an_aborted_run():
    d = Dispatcher()
    incident, = parse(grafana_payload())
    assert d.submit(incident) is True
    assert d.submit(incident) is False
    d.forget(incident.fingerprint)
    assert d.submit(incident) is True


# ---------------------------------------------------------------------------
# The HTTP surface -- exercised over a real socket
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint():
    d = Dispatcher()
    server = serve(d, host="127.0.0.1", port=0, token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", d
    finally:
        server.shutdown()
        server.server_close()


def post(url: str, payload: dict, token: str | None = "s3cret"):
    req = urllib.request.Request(
        url + "/alert", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_a_real_post_starts_the_loop(endpoint):
    url, dispatcher = endpoint
    status, body = post(url, grafana_payload())
    assert status == 200
    assert body == {"received": 1, "dispatched": 1}
    assert dispatcher.accepted[0].market == "de-DE"


def test_an_unauthenticated_caller_cannot_start_a_repair(endpoint):
    """This endpoint is publicly reachable and it starts autonomous work on
    real assets. It gets a door."""
    url, dispatcher = endpoint
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(url, grafana_payload(), token=None)
    assert exc.value.code == 401
    assert dispatcher.accepted == []


def test_a_duplicate_delivery_still_answers_200(endpoint):
    """Alertmanager retries on non-2xx. Answering an error to a correctly
    ignored re-delivery would turn one duplicate into a retry storm."""
    url, _ = endpoint
    post(url, grafana_payload())
    status, body = post(url, grafana_payload())
    assert status == 200
    assert body == {"received": 1, "dispatched": 0}


@pytest.mark.parametrize("path", ["/api/health", "/healthz"])
def test_health(endpoint, path):
    """Both paths answer, and /api/health is the one to probe in production.

    Cloud Run's frontend reserves /healthz and answers it itself with a Google
    error page -- it never reaches the container. A probe there reported the
    service down while it was serving every other route correctly, which is a
    worse failure than having no probe, because it points the investigation at
    the healthy thing.
    """
    url, _ = endpoint
    with urllib.request.urlopen(url + path, timeout=5) as resp:
        assert json.loads(resp.read()) == {"ok": True}


def test_a_rejected_post_answers_over_http_rather_than_aborting(endpoint):
    """Regression: the handler used to reply 401 without reading the request
    body. Replying and closing while the client is still sending makes the OS
    abort the connection, so the caller sees ConnectionAbortedError instead of
    a 401 -- and whoever debugs it goes looking at the network rather than at
    the credential. Run repeatedly, because the race is timing-dependent.
    """
    url, _ = endpoint
    payload = grafana_payload()
    payload["alerts"] *= 40           # a body big enough to still be in flight
    for _ in range(8):
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(url, payload, token="wrong-token")
        assert exc.value.code == 401
