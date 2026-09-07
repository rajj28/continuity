"""Provision the contact point and route that turn an alert into agent work.

This is the second half of the inversion. `grafana/rules/alerts.yaml` decides
WHEN the agent wakes; this decides WHERE that wake-up goes. Both are code, both
are pushed by a script, and neither is editable by an agent.

The routing key is the `continuity_action` label. Any alert rule that carries
it is, by that fact alone, a rule that dispatches work -- so adding a new kind
of autonomous response is a rule change, not a code change, and the set of
things that can start the agent is enumerable by reading one YAML file.

Two settings on the route are load-bearing rather than defaults:

  group_by: [alertname, title, market]
      A market is the unit of work. Grouping any coarser would deliver five
      markets as one notification and the Conductor would have to guess which
      one it was hired to fix.

  repeat_interval: 30m
      Long, because the receiver dedups on fingerprint anyway and a repair
      takes minutes. Short repeats would be silently discarded, which looks
      identical to a broken webhook when you are reading logs at 2am.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry.otel import load_env  # noqa: E402

CONTACT_POINT = "continuity-conductor"
ROUTE_LABEL = "continuity_action"


class AlertingError(RuntimeError):
    pass


def _session(env: dict[str, str]) -> tuple[requests.Session, str]:
    token = env.get("GRAFANA_PROVISIONER_TOKEN") or env.get(
        "GRAFANA_SERVICE_ACCOUNT_TOKEN"
    )
    if not token:
        raise AlertingError("no Grafana admin token in .env.local")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    # Without this, provisioned objects are locked against UI edits. We want
    # them editable in the UI during a demo -- a judge asking "can you change
    # the threshold live?" should get yes -- while still being reproducible
    # from this script.
    s.headers["X-Disable-Provenance"] = "true"
    return s, env["GRAFANA_URL"].rstrip("/") + "/api/v1/provisioning"


def contact_point(url: str, token: str) -> dict:
    return {
        "name": CONTACT_POINT,
        "type": "webhook",
        "settings": {
            "url": url,
            "httpMethod": "POST",
            "authorization_scheme": "Bearer",
            "authorization_credentials": token,
        },
        "disableResolveMessage": False,  # the agent needs to hear recovery too
    }


def upsert_contact_point(session: requests.Session, base: str, body: dict) -> None:
    existing = session.get(f"{base}/contact-points").json()
    match = next((c for c in existing if c.get("name") == body["name"]), None)
    if match:
        r = session.put(f"{base}/contact-points/{match['uid']}",
                        json={**body, "uid": match["uid"]})
    else:
        r = session.post(f"{base}/contact-points", json=body)
    if r.status_code >= 300:
        raise AlertingError(f"contact point -> {r.status_code} {r.text[:300]}")
    print(f"  contact point {body['name']} -> {body['settings']['url']}")


def upsert_route(session: requests.Session, base: str) -> None:
    """Insert our route into the existing tree rather than replacing it.

    `PUT /policies` writes the WHOLE notification tree. Replacing it wholesale
    would silently delete every other route on the stack, which is a rude thing
    for a provisioning script to do and an easy thing not to notice.
    """
    tree = session.get(f"{base}/policies").json()
    routes = [
        r for r in tree.get("routes", [])
        if r.get("receiver") != CONTACT_POINT
    ]
    routes.insert(0, {
        "receiver": CONTACT_POINT,
        "object_matchers": [[ROUTE_LABEL, "=~", ".+"]],
        "group_by": ["alertname", "title", "market"],
        "group_wait": "10s",
        "group_interval": "1m",
        "repeat_interval": "30m",
        # Keep delivering to the default receiver as well, so a human still
        # sees what the agent was dispatched to do.
        "continue": True,
    })
    tree["routes"] = routes
    r = session.put(f"{base}/policies", json=tree)
    if r.status_code >= 300:
        raise AlertingError(f"policy tree -> {r.status_code} {r.text[:300]}")
    print(f"  route: {ROUTE_LABEL}=~.+  ->  {CONTACT_POINT}")


def show(session: requests.Session, base: str) -> None:
    points = session.get(f"{base}/contact-points").json()
    for c in points:
        if c.get("name") == CONTACT_POINT:
            print(f"contact point {c['name']}: {c['settings'].get('url')}")
    tree = session.get(f"{base}/policies").json()
    print(f"default receiver: {tree.get('receiver')}")
    for r in tree.get("routes", []):
        print(f"  route -> {r.get('receiver')}  matchers="
              f"{r.get('object_matchers')}  group_by={r.get('group_by')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="public URL of the wake receiver "
                                      "(default: $WAKE_PUBLIC_URL)")
    parser.add_argument("--check", action="store_true",
                        help="print the current routing and exit")
    args = parser.parse_args()

    env = load_env()
    session, base = _session(env)

    if args.check:
        show(session, base)
        return 0

    url = args.url or env.get("WAKE_PUBLIC_URL", "")
    if not url:
        raise AlertingError(
            "no public URL for the wake receiver.\n"
            "Grafana Cloud can only POST to something reachable from the "
            "internet, so the receiver needs a public address:\n"
            "  - a tunnel for local runs:  "
            "bin/cloudflared.exe tunnel --url http://127.0.0.1:8080\n"
            "  - or the Cloud Run service URL once deployed\n"
            "Then: python grafana/alerting.py --url https://<host>/alert"
        )
    if not url.startswith("https://"):
        raise AlertingError(
            f"refusing to send a bearer token to {url!r} over plaintext"
        )

    token = env.get("WAKE_TOKEN", "")
    if not token:
        raise AlertingError(
            "WAKE_TOKEN is unset. This endpoint starts autonomous work on real "
            "assets from a public URL; it does not go up without a door on it."
        )

    print("provisioning alert routing")
    upsert_contact_point(session, base, contact_point(url, token))
    upsert_route(session, base)
    print("\nverifying")
    show(session, base)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlertingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
