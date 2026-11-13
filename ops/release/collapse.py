"""Collapse the live Compose chain into one self-contained file, safely.

Every release appends an overlay file to the running stack's compose invocation,
so the live chain grows every time anything ships and each overlay lives inside a
historical release directory. That is the reason retention cannot delete by age,
and the reason a plain ``docker compose up`` cannot reproduce the running stack.

This tool renders the live chain once, writes the fully resolved result as a
single file, then re-renders that single file and compares the two documents.
The comparison is the safety gate: if the collapsed file describes a different
stack, nothing is written.

Two variables, ``PLUS_UI_REVISION`` and ``PLUS_UI_IMAGE_ID``, are required by a
historical UI overlay for interpolation only. The tool supplies placeholders and
proves they are overridden by later overlays, by checking that the resolved app
image and revision still match the live container. If they do not, it refuses.

Dry run by default; ``--execute`` writes the file. Nothing is restarted: the
operator still decides when to recreate the service.

    python ops/release/collapse.py --remote omakase-ct
    python ops/release/collapse.py --remote omakase-ct --execute
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys

PLACEHOLDER_VARIABLES = ("PLUS_UI_REVISION", "PLUS_UI_IMAGE_ID")

REMOTE = r"""
import base64, json, os, subprocess, sys

config = json.loads(base64.b64decode(sys.argv[1]))
stack = config["stack"]
project = config["project"]
execute = config["execute"]
placeholders = config["placeholders"]
target = os.path.join(stack, "compose-collapsed.yaml")
work = os.path.join(stack, ".compose-collapsed.draft.yaml")


def run(args, timeout=600, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def inspect_container():
    raw = run(["docker", "inspect", project]).stdout
    record = json.loads(raw)[0]
    labels = record["Config"].get("Labels") or {}
    files = [item for item in (labels.get("com.docker.compose.project.config_files") or "").split(",") if item]
    return files, record["Image"], labels.get("org.opencontainers.image.revision")


def base_command():
    return ["docker", "compose", "--project-directory", stack,
            "--env-file", os.path.join(stack, ".env"), "-p", project]


def render(files, fmt, extra_env):
    command = base_command()
    for item in files:
        command.extend(["-f", item])
    command.extend(["config", "--format", fmt])
    return run(command, env={**os.environ, **extra_env})


report = {"stack": stack, "execute": execute}
files, live_image, live_revision = inspect_container()
report["inputs"] = len(files)
missing = [item for item in files if not os.path.isfile(item)]
report["missing"] = len(missing)
if missing:
    report["abort"] = "compose inputs missing; run retention checks first"
    print(json.dumps(report))
    raise SystemExit(0)

extra = {name: "collapsed-placeholder" for name in placeholders}
chain_json = render(files, "json", extra)
chain_yaml = render(files, "yaml", extra)
if chain_json.returncode != 0 or chain_yaml.returncode != 0:
    report["abort"] = (chain_json.stderr or chain_yaml.stderr)[-600:]
    print(json.dumps(report))
    raise SystemExit(0)
first = json.loads(chain_json.stdout)
# A resolved value can contain a dollar sign, which compose would interpolate
# again on the next render. Escape it so the collapsed file is stable.
draft_text = chain_yaml.stdout.replace("$", "$$")
with open(work, "w", encoding="utf-8") as handle:
    handle.write(draft_text)
after = render([work], "json", extra)
if after.returncode != 0:
    report["abort"] = "collapsed file does not render: " + after.stderr[-400:]
    os.unlink(work)
    print(json.dumps(report))
    raise SystemExit(0)
second = json.loads(after.stdout)
report["identical"] = first == second
report["services"] = sorted((first.get("services") or {}).keys())
service = (first.get("services") or {}).get(project) or {}
environment = service.get("environment") or {}
report["resolved_image_matches_live"] = service.get("image") == live_image
report["resolved_revision_matches_live"] = environment.get("OMAKASE_REVISION") == live_revision
report["placeholder_leak"] = "collapsed-placeholder" in json.dumps(first)
report["bytes"] = len(draft_text)
report["live_image"] = live_image
report["live_revision"] = live_revision
report["draft"] = work
if not report["identical"]:
    differing = [name for name in (first.get("services") or {})
                 if (first["services"][name] != ((second.get("services") or {}).get(name)))]
    report["differing_services"] = differing[:8]
faithful = (
    report["identical"]
    and report["resolved_image_matches_live"]
    and report["resolved_revision_matches_live"]
    and not report["placeholder_leak"]
)
report["faithful"] = faithful
if execute and faithful:
    os.replace(work, target)
    os.chmod(target, 0o600)
    report["written"] = target
elif os.path.exists(work):
    os.unlink(work)
print(json.dumps(report))
"""


def remote(host: str, payload: dict) -> dict:
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    process = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3 - " + encoded],
        input=REMOTE,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if process.returncode != 0:
        raise SystemExit(process.stderr[-2000:] or "collapse failed")
    return json.loads(process.stdout)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default="/opt/stacks/omakase-plus")
    parser.add_argument("--project", default="omakase-plus")
    parser.add_argument("--remote", default=None)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    payload = {
        "stack": args.stack,
        "project": args.project,
        "execute": args.execute,
        "placeholders": list(PLACEHOLDER_VARIABLES),
    }
    result = remote(args.remote, payload) if args.remote else {}
    for key in ("inputs", "missing", "identical", "resolved_image_matches_live",
                "resolved_revision_matches_live", "faithful", "bytes", "live_revision"):
        if key in result:
            print(f"{key}: {result[key]}")
    if "abort" in result:
        print("ABORT:", result["abort"])
        return 2
    if "written" in result:
        print("written:", result["written"])
        print("swap command (operator decides when):")
        print(f"  docker compose --project-directory {args.stack} --env-file {args.stack}/.env "
              f"-p {args.project} -f {result['written']} up -d --no-deps {args.project}")
    elif not result.get("execute"):
        print("dry run: nothing written")
    return 0 if result.get("faithful") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
