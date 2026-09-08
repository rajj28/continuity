"""Deploy Continuity to Cloud Run: the wake receiver and the control room.

Cloud Run rather than a tunnel because the alert -> agent hop is the claim the
project rests on, and a claim demonstrated through an ephemeral tunnel on a
laptop is a weaker claim. A judge should be able to POST at a real URL, and
open the control room at another.

## One image, built once, deployed twice

Both services come from the same image with a different argument. That is not
a packaging convenience -- it is the guarantee that a repair started by an
alert and a repair approved by an operator run the same code against the same
contracts. `gcloud run deploy --source` would build the image again per
service, which takes four minutes and, worse, allows the two to drift if a
file changes between them. So: build once, resolve the digest, deploy that
exact digest to both.

## Preflight

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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry.otel import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IMAGE_NAME = "continuity"
REPOSITORY = "cloud-run-source-deploy"

# The two services, and why each is shaped the way it is.
#
# `args` is what docker-entrypoint.sh dispatches on. Everything else is a
# consequence of what the service does, and is written down here rather than
# passed per deploy so the two cannot quietly diverge.
SERVICES: dict[str, dict] = {
    "wake": {
        "name": "continuity-wake",
        "args": "wake",
        # A repair outlives a normal request. The default 300s would kill the
        # Conductor mid-verification and leave a half-written asset behind.
        "timeout": "900",
        "memory": "2Gi",
        "cpu": "2",
        # One instance keeps the in-process dedup set coherent until Pub/Sub
        # takes that job. Two instances would each start the same incident.
        "max_instances": "1",
        "min_instances": "0",
        "why": "receives Grafana alerts and starts investigations",
    },
    "control": {
        "name": "continuity-control",
        "args": "control",
        # Investigations stream as server-sent events. A 300s ceiling would cut
        # the connection mid-reasoning and look like the agent had crashed.
        "timeout": "900",
        "memory": "2Gi",
        "cpu": "2",
        # Pending proposals live on the instance's disk, so an operator who
        # investigated on one instance and approved on another would be told
        # there was nothing to approve.
        "max_instances": "1",
        # Warm. This is the URL a judge opens, and a cold start on an image
        # this size is thirty seconds of blank page.
        "min_instances": "1",
        "why": "the operator control room",
        # This service, and only this service, republishes the store to
        # Grafana. See docker-entrypoint.sh for why exactly one may.
        "env": {"RUN_STATE_EXPORTER": "1"},
    },
}

# Everything the wake path and its workers touch. Enabled explicitly rather
# than on first use, so a missing permission surfaces here and not mid-repair.
APIS = (
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "pubsub.googleapis.com",
)

# What the container needs. Passed as env vars rather than baked into the
# image: the image is a build artifact that outlives any one credential, and
# these rotate after the hackathon.
#
# No Google credential appears here. The services run AS a service account, so
# Vertex authenticates from the metadata server -- a key file copied into a
# container is a key file that can leak out of one.
FORWARDED = (
    "WAKE_TOKEN",
    "GCP_PROJECT_ID",
    "GCP_VERTEX_LOCATION",
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


def ensure_repository(project: str, region: str) -> None:
    """Create the Artifact Registry repo the build pushes to.

    Created explicitly rather than left to gcloud, which offers to make it and
    then WAITS ON A PROMPT. A deploy that blocks on stdin looks exactly like a
    deploy that hung, and in CI or a background run it simply fails with an
    empty error -- which is how this first went wrong.
    """
    existing = run(
        "artifacts", "repositories", "list", f"--project={project}",
        f"--location={region}", "--format=value(name)",
    )
    if REPOSITORY in existing:
        print(f"  registry ready   ({REPOSITORY})")
        return
    print(f"  creating registry {REPOSITORY} in {region}")
    run("artifacts", "repositories", "create", REPOSITORY,
        "--repository-format=docker", f"--location={region}",
        f"--project={project}",
        "--description=Continuity Cloud Run source deploys", "--quiet")


def _tag() -> str:
    """A build tag that says what was built.

    The git sha when there is one, because "which commit is live" is a question
    someone always ends up asking at the worst moment; a UTC timestamp
    otherwise. Marked dirty when the tree has uncommitted changes, so a tag can
    never claim to be a commit it is not.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=ROOT,
                capture_output=True, text=True,
            ).stdout.strip()
            return proc.stdout.strip() + ("-dirty" if dirty else "")
    except OSError:
        pass
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def build(project: str, region: str) -> str:
    """Build the one image and return the reference both services will run.

    Returned as an immutable digest rather than the tag that was pushed. Two
    services pinned to a moving tag are two services that can end up on
    different code the moment anything reruns -- and the whole point of one
    image is that they cannot.
    """
    ensure_repository(project, region)
    tag = (f"{region}-docker.pkg.dev/{project}/{REPOSITORY}/"
           f"{IMAGE_NAME}:{_tag()}")
    print(f"\nbuilding {tag}")
    run("builds", "submit", str(ROOT), f"--tag={tag}",
        f"--project={project}", "--quiet", capture=False)

    digest = run(
        "artifacts", "docker", "images", "describe", tag,
        f"--project={project}", "--format=value(image_summary.digest)",
    ).strip()
    if not digest:
        raise DeployError(f"built {tag} but could not resolve its digest")
    print(f"  digest {digest[:26]}...")
    return tag.split(":", 1)[0] + "@" + digest


def write_env_file(env: dict[str, str], dest: Path,
                   extra: dict[str, str] | None = None) -> Path:
    """Write the container's environment as YAML for --env-vars-file.

    A file rather than --set-env-vars. The delimiter-escaping form
    (`^@^KEY=v@KEY=v`) silently produced ONE variable literally named
    `@WAKE_TOKEN` whose value was the entire concatenated string -- so the
    service came up with no token and answered an unauthenticated POST with
    200. It deployed successfully and was wrong, which is the worst way for a
    credential to fail.

    Grafana tokens are base64 and contain `=`; URLs contain `/` and `:`. YAML
    quotes all of it and gcloud parses the file rather than the command line.
    """
    if not env.get("WAKE_TOKEN"):
        raise DeployError(
            "WAKE_TOKEN is unset. These services start autonomous work on real "
            "assets from a public URL; they do not go up without a door on them."
        )
    values = {k: env[k] for k in FORWARDED if env.get(k)}
    values.update(extra or {})
    lines = []
    for key, value in values.items():
        # Single-quoted YAML, with the only escape that form needs.
        lines.append(f"{key}: '{value.replace(chr(39), chr(39) * 2)}'")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def deploy(project: str, region: str, env: dict[str, str],
           image: str, spec: dict) -> str:
    service = str(spec["name"])
    # Written next to the deploy script and removed afterwards: it holds every
    # token the service needs and has no business outliving the deploy.
    env_file = write_env_file(env, ROOT / ".cloudrun-env.yaml",
                              spec.get("env"))
    print(f"\ndeploying {service} to {region}  ({spec['why']})")
    try:
        run(
            "run", "deploy", service,
            # Never prompt. This runs unattended, and a question on stdin is
            # indistinguishable from a hang -- the first deploy stalled on
            # gcloud offering to create the Artifact Registry repository.
            "--quiet",
            f"--image={image}",
            f"--args={spec['args']}",
            f"--project={project}",
            f"--region={region}",
            # Public because Grafana Cloud's alertmanager has no Google identity
            # to authenticate as, and because a control room a judge cannot open
            # is not a control room. Reads are open by design; everything that
            # CHANGES anything needs the bearer token. See docs/MCP_BOUNDARIES.md.
            "--allow-unauthenticated",
            "--port=8080",
            f"--timeout={spec['timeout']}",
            f"--memory={spec['memory']}",
            f"--cpu={spec['cpu']}",
            f"--max-instances={spec['max_instances']}",
            f"--min-instances={spec['min_instances']}",
            # Runs as its own identity rather than the default compute account,
            # which carries far more than this needs. Vertex, Storage and Pub/Sub
            # access come from the roles bound to it, with no key in the image.
            f"--service-account=continuity@{project}.iam.gserviceaccount.com",
            f"--env-vars-file={env_file}",
            capture=False,
        )
    finally:
        env_file.unlink(missing_ok=True)

    url = run(
        "run", "services", "describe", service,
        f"--project={project}", f"--region={region}",
        "--format=value(status.url)",
    ).strip()
    if not url:
        raise DeployError(f"deployed {service}, but Cloud Run reported no URL")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="default: $GCP_PROJECT_ID")
    parser.add_argument("--region", help="default: $GCP_REGION")
    parser.add_argument("--service", choices=(*SERVICES, "both"), default="both",
                        help="which service to deploy (default: both)")
    parser.add_argument("--image", help="skip the build and deploy this image")
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

    wanted = list(SERVICES) if args.service == "both" else [args.service]
    image = args.image or build(project, region)

    urls = {}
    for key in wanted:
        urls[key] = deploy(project, region, env, image, SERVICES[key])

    print("\nup:")
    for key, url in urls.items():
        print(f"  {SERVICES[key]['name']:<20} {url}")
    if "wake" in urls:
        print("\nnow point Grafana at the wake receiver:")
        print(f"  python grafana/alerting.py --url {urls['wake']}/alert")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
