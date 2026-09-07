"""Push the verdict rules into the live Mimir ruler.

The rules in grafana/rules/ are the source of truth and they are unit-tested
with promtool. This script is the only thing that moves them into the running
stack, so what evaluates in production is byte-identical to what the tests
proved -- there is no "edited it in the UI afterwards" gap.

Route
-----
Writes go through Grafana's ruler proxy:

    POST {GRAFANA_URL}/api/ruler/grafanacloud-prom/api/v1/rules/{namespace}

rather than straight at the Mimir ruler API. Two reasons, one practical and
one architectural:

  - the Grafana Cloud access-policy token we hold carries metrics:read/write
    but not rules:write, while the stack's own Prometheus datasource holds
    credentials that do. Grafana proxies with those.
  - it keeps a single admin credential (the Grafana service account) rather
    than minting a second class of secret. The agents never get this token;
    see docs/MCP_BOUNDARIES.md.

Idempotent: POSTing a group replaces it wholesale, so re-running converges.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry.otel import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = ROOT / "grafana" / "rules"

# One namespace so the whole verdict can be listed, diffed, or torn down as a
# unit. Mimir namespaces are just strings; Grafana shows them as rule folders.
NAMESPACE = "continuity"
DATASOURCE_UID = "grafanacloud-prom"

RULE_FILES = ("recording.yaml", "alerts.yaml")


class ProvisionError(RuntimeError):
    pass


def _session(env: dict[str, str]) -> tuple[requests.Session, str]:
    token = env.get("GRAFANA_PROVISIONER_TOKEN") or env.get(
        "GRAFANA_SERVICE_ACCOUNT_TOKEN"
    )
    if not token:
        raise ProvisionError("no Grafana admin token in .env.local")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    base = env["GRAFANA_URL"].rstrip("/")
    return s, f"{base}/api/ruler/{DATASOURCE_UID}/api/v1/rules"


def load_groups() -> list[dict]:
    """Every group across both rule files, in file order.

    Recording rules are loaded before alerts on purpose: an alert on
    `market_release_ready` is meaningless until the rule that records it
    exists, and the ruler will happily accept the alert and evaluate it to
    nothing.
    """
    groups: list[dict] = []
    for name in RULE_FILES:
        doc = yaml.safe_load((RULES_DIR / name).read_text(encoding="utf-8"))
        for group in doc["groups"]:
            group["_source"] = name
            groups.append(group)
    return groups


def push(session: requests.Session, url: str, group: dict) -> None:
    source = group.pop("_source", "?")
    # JSON, not YAML. Grafana's ruler proxy advertises both and answers a YAML
    # body with a bare `400 bad request data`; only the JSON path round-trips.
    r = session.post(
        f"{url}/{NAMESPACE}",
        json=group,
    )
    if r.status_code not in (200, 202):
        raise ProvisionError(
            f"{group['name']} ({source}) -> {r.status_code} {r.text[:300]}"
        )
    print(f"  pushed {group['name']:<28} {len(group['rules']):>2} rules "
          f"({source})")


def fetch(session: requests.Session, url: str) -> dict[str, list[str]]:
    """What the ruler currently holds, as {group: [rule names]}."""
    r = session.get(url)
    r.raise_for_status()
    live: dict[str, list[str]] = {}
    for _namespace, groups in (r.json() or {}).items():
        for group in groups:
            live[group["name"]] = [
                rule.get("record") or rule.get("alert") for rule in group["rules"]
            ]
    return live


def verify(session: requests.Session, url: str, groups: list[dict]) -> None:
    """Read the rules back and confirm the ruler holds what we sent.

    A 200 on the POST only means Grafana accepted the payload. The rules are
    not real until the ruler lists them, so the check is a read-back, not a
    status code.
    """
    live = fetch(session, url)
    missing: list[str] = []
    for group in groups:
        want = {rule.get("record") or rule.get("alert") for rule in group["rules"]}
        have = set(live.get(group["name"], []))
        if not want <= have:
            missing.extend(f"{group['name']}/{n}" for n in sorted(want - have))
    if missing:
        raise ProvisionError("ruler is missing after push: " + ", ".join(missing))
    total = sum(len(v) for v in live.values())
    print(f"\nverified: ruler holds {len(live)} groups, {total} rules")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="list what the ruler holds and exit")
    args = parser.parse_args()

    env = load_env()
    session, url = _session(env)

    if args.check:
        live = fetch(session, url)
        if not live:
            print("ruler holds no rules")
        for name, rules in sorted(live.items()):
            print(f"{name}")
            for rule in rules:
                print(f"    {rule}")
        return 0

    groups = load_groups()
    print(f"pushing {len(groups)} groups to {NAMESPACE}/")
    for group in groups:
        push(session, url, group)
    verify(session, url, groups)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
