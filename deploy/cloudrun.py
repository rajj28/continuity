"""Deploy the wake receiver to Cloud Run, and wire Grafana to it.

Cloud Run rather than a tunnel because the alert -> agent hop is the claim the
project rests on, and a claim demonstrated through an ephemeral tunnel on a
laptop is a weaker claim. A judge should be able to POST at a real URL.

Most of this file is preflight. That is deliberate: every failure this project
has actually hit on Google Cloud was a disabled billing account or an
un-enabled API, and both produce errors far from their cause. `gcloud run
deploy` failing because Cloud Build could not start because billing is closed
reports a build error, three layers from the truth. So the checks run first and
say the actual thing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry.otel import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "continuity-wake"

# Everything the wake path and its workers touch. Enabled explicitly rather
# than on first use, so a missing permission surfaces here and not mid-repair.
APIS = (
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "pubsub.googleapis.com",
)

# Secrets the container needs. Passed as env vars rather than baked into the
# image: the image is a build artifact that outlives any one credential, and
# these rotate after the hackathon.
FORWARDED = (
    "WAKE_TOKEN",
    "GEMINI_API_KEY",
    "GRAFANA_URL",
    "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    "OTLP_ENDPOINT",
    "OTLP_INSTANCE_ID",
    "OTLP_TOKEN",
    "PROM_QUERY_URL",
    "PROM_USER_ID",
    "PROM_READ_TOKEN",
)


class DeployError(RuntimeError):
    pass


def _gcloud() -> str:
    """On Windows the executable is gcloud.cmd; `which gcloud` finds a shell
    wrapper Python cannot exec."""
    for name in ("gcloud.cmd", "gcloud"):
        found = shutil.which(name)
        if found:
            return found
    raise DeployError("gcloud not found on PATH")


def run(*args: str, capture: bool = True) -> str:
    proc = subprocess.run(
        [_gcloud(), *args], capture_output=capture, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DeployError(f"gcloud {' '.join(args[:3])} failed:\n{detail[:800]}")
    return proc.stdout or ""


def preflight(project: str) -> None:
    print(f"preflight for {project}")

    linked = json.loads(run(
        "billing", "projects", "describe", project, "--format=json"
    ) or "{}")
    account = linked.get("billingAccountName", "")
    if not linked.get("billingEnabled"):
        raise DeployError(
            f"billing is not enabled on {project}"
            + (f" (linked to {account}, which is closed)" if account else "")
            + ".\nCloud Run, Cloud Build and Pub/Sub all need an OPEN billing "
              "account.\nOpen or create one at "
              "https://console.cloud.google.com/billing and link this project, "
              "then re-run."
        )
    print(f"  billing enabled  ({account})")

    enabled = {
        line.strip() for line in run(
            "services", "list", "--enabled", f"--project={project}",
            "--format=value(config.name)"
        ).splitlines() if line.strip()
    }
    missing = [api for api in APIS if api not in enabled]
    if missing:
        print(f"  enabling {len(missing)} API(s): {', '.join(missing)}")
        run("services", "enable", *missing, f"--project={project}")
    print(f"  APIs ready       ({len(APIS)} of {len(APIS)})")


def env_flag(env: dict[str, str]) -> str:
    """Build --set-env-vars, refusing to deploy a public endpoint with no token.

    Values are joined with `^@^` because Grafana URLs and base64 tokens both
    contain commas' worse cousins, and gcloud's default comma delimiter mangles
    them silently.
    """
    if not env.get("WAKE_TOKEN"):
        raise DeployError(
            "WAKE_TOKEN is unset. This service starts autonomous work on real "
            "assets from a public URL; it does not go up without a door on it."
        )
    pairs = [f"{k}={env[k]}" for k in FORWARDED if env.get(k)]
    return "^@^" + "@".join(pairs)


def deploy(project: str, region: str, env: dict[str, str]) -> str:
    print(f"\ndeploying {SERVICE} to {region}")
    run(
        "run", "deploy", SERVICE,
        f"--source={ROOT}",
        f"--project={project}",
        f"--region={region}",
        # Public because Grafana Cloud's alertmanager has no Google identity to
        # authenticate as. The bearer token in wake.py is the actual control;
        # see docs/MCP_BOUNDARIES.md.
        "--allow-unauthenticated",
        "--port=8080",
        # A repair outlives a normal request. The default 300s would kill the
        # Conductor mid-verification and leave a half-written asset behind.
        "--timeout=900",
        "--memory=2Gi",
        "--cpu=2",
        # One instance keeps the in-process dedup set coherent until Pub/Sub
        # takes that job. Two instances would each start the same incident.
        "--max-instances=1",
        f"--set-env-vars={env_flag(env)}",
        capture=False,
    )
    url = run(
        "run", "services", "describe", SERVICE,
        f"--project={project}", f"--region={region}",
        "--format=value(status.url)",
    ).strip()
    if not url:
        raise DeployError("deployed, but Cloud Run reported no URL")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="default: $GCP_PROJECT_ID")
    parser.add_argument("--region", help="default: $GCP_REGION")
    parser.add_argument("--check", action="store_true",
                        help="run preflight only, deploy nothing")
    args = parser.parse_args()

    env = load_env()
    project = args.project or env.get("GCP_PROJECT_ID", "")
    region = args.region or env.get("GCP_REGION") or "us-central1"
    if not project:
        raise DeployError("no project; set GCP_PROJECT_ID or pass --project")

    preflight(project)
    if args.check:
        print("\npreflight only; nothing deployed")
        return 0

    url = deploy(project, region, env)
    print(f"\nservice up: {url}")
    print("\nnow point Grafana at it:")
    print(f"  python grafana/alerting.py --url {url}/alert")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
