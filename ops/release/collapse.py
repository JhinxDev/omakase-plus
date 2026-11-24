"""Collapse the live Compose chain into one self-contained file, safely.

Every release appends an overlay file to the running stack's compose invocation,
so the live chain grows every time anything ships and each overlay lives inside a
historical release directory. That is the reason retention cannot delete by age,
and the reason a plain ``docker compose up`` cannot reproduce the running stack.

This tool renders the live chain with ``--no-interpolate --no-env-resolution``
so ``${VAR}`` placeholders and ``env_file`` references stay parameterized, then
proves interpolated equivalence against the full chain and checks that no secret
values from ``.env`` leaked into the rendered YAML.

Two variables, ``PLUS_UI_REVISION`` and ``PLUS_UI_IMAGE_ID``, are required by a
historical UI overlay for interpolation checks. The tool supplies placeholders
and proves they are overridden by later overlays, by checking that the resolved
app image and revision still match the live container. If they do not, it refuses.

Dry run by default; ``--execute`` writes the file. Nothing is restarted: the
operator still decides when to recreate the service.

    python ops/release/collapse.py --remote omakase-ct
    python ops/release/collapse.py --remote omakase-ct --execute
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

PLACEHOLDER_VARIABLES = ("PLUS_UI_REVISION", "PLUS_UI_IMAGE_ID")
_SECRET_KEY_RE = re.compile(
    r"(?:SECRET|KEY|TOKEN|PASSWORD|PASS|AUTH|CREDENTIAL|COOKIE)",
    re.IGNORECASE,
)
_TRANSIENT_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])/(?:var/tmp|tmp)/[A-Za-z0-9._/@+-]+")


def parse_env_secrets(env_text: str) -> dict[str, str]:
    """Extract sensitive variable assignments from a dotenv file."""
    secrets: dict[str, str] = {}
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not _SECRET_KEY_RE.search(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if len(value) >= 8:
            secrets[key] = value
    return secrets


def find_secret_leaks(rendered_yaml: str, secrets: Mapping[str, str]) -> list[str]:
    """Return sorted environment variable names whose values appear in YAML."""
    leaked = [name for name, value in secrets.items() if value and value in rendered_yaml]
    return sorted(set(leaked))


def find_transient_paths(files: Sequence[str]) -> list[str]:
    """List Compose input files located under transient /tmp or /var/tmp paths."""
    return [item for item in files if item.startswith(("/var/tmp/", "/tmp/"))]


def rehome_transient_paths(yaml_text: str, stack_dir: str) -> tuple[str, list[dict[str, str]]]:
    """Rewrite transient /tmp and /var/tmp references into ``<stack_dir>/rehomed``."""
    base = stack_dir.rstrip("/")
    mapping: dict[str, str] = {}
    used_names: set[str] = set()
    mappings: list[dict[str, str]] = []

    def _allocate(source: str) -> str:
        if source in mapping:
            return mapping[source]
        name = PurePosixPath(source).name or "item"
        candidate = name
        if candidate in used_names:
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
            candidate = f"{digest}-{name}"
        used_names.add(candidate)
        target = f"{base}/rehomed/{candidate}"
        mapping[source] = target
        mappings.append({"source": source, "target": target})
        return target

    rewritten = _TRANSIENT_PATH_RE.sub(lambda match: _allocate(match.group(0)), yaml_text)
    return rewritten, mappings


REMOTE = r"""
import base64, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import PurePosixPath

config = json.loads(base64.b64decode(sys.argv[1]))
stack = config["stack"]
project = config["project"]
execute = config["execute"]
placeholders = config["placeholders"]
target = os.path.join(stack, "compose-collapsed.yaml")
work = os.path.join(stack, ".compose-collapsed.draft.yaml")

_SECRET_KEY_RE = re.compile(r"(?:SECRET|KEY|TOKEN|PASSWORD|PASS|AUTH|CREDENTIAL|COOKIE)", re.I)
_TRANSIENT_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])/(?:var/tmp|tmp)/[A-Za-z0-9._/@+-]+")


def parse_env_secrets(env_text):
    secrets = {}
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not _SECRET_KEY_RE.search(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if len(value) >= 8:
            secrets[key] = value
    return secrets


def find_secret_leaks(rendered_yaml, secrets):
    return sorted({name for name, value in secrets.items() if value and value in rendered_yaml})


def rehome_transient_paths(yaml_text, stack_dir):
    base = stack_dir.rstrip("/")
    mapping = {}
    used_names = set()
    entries = []

    def allocate(source):
        if source in mapping:
            return mapping[source]
        name = PurePosixPath(source).name or "item"
        candidate = name
        if candidate in used_names:
            candidate = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8] + "-" + name
        used_names.add(candidate)
        dst = f"{base}/rehomed/{candidate}"
        mapping[source] = dst
        entries.append({"source": source, "target": dst})
        return dst

    rewritten = _TRANSIENT_PATH_RE.sub(lambda m: allocate(m.group(0)), yaml_text)
    return rewritten, entries


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


def render(files, fmt, extra_env, *, interpolate=True):
    command = base_command()
    for item in files:
        command.extend(["-f", item])
    command.append("config")
    if not interpolate:
        command.extend(["--no-interpolate", "--no-env-resolution"])
    command.extend(["--format", fmt])
    return run(command, env={**os.environ, **extra_env})


report = {"stack": stack, "execute": execute}
files, live_image, live_revision = inspect_container()
report["inputs"] = len(files)
report["transient_inputs"] = [item for item in files if item.startswith(("/var/tmp/", "/tmp/"))]
missing = [item for item in files if not os.path.isfile(item)]
report["missing"] = len(missing)
if missing:
    report["abort"] = "compose inputs missing; run retention checks first"
    print(json.dumps(report))
    raise SystemExit(0)

extra = {name: "collapsed-placeholder" for name in placeholders}
chain_json = render(files, "json", extra, interpolate=True)
chain_yaml = render(files, "yaml", extra, interpolate=False)
if chain_json.returncode != 0 or chain_yaml.returncode != 0:
    report["abort"] = (chain_json.stderr or chain_yaml.stderr)[-600:]
    print(json.dumps(report))
    raise SystemExit(0)
first = json.loads(chain_json.stdout)
draft_text, rehomed = rehome_transient_paths(chain_yaml.stdout, stack)
report["rehomed_paths"] = rehomed

# On dry-run keep validation read-only if transient paths are referenced in YAML.
validate_text = draft_text
if rehomed and execute:
    rehomed_dir = os.path.join(stack, "rehomed")
    os.makedirs(rehomed_dir, mode=0o700, exist_ok=True)
    for item in rehomed:
        if os.path.isfile(item["source"]):
            shutil.copyfile(item["source"], item["target"])
            os.chmod(item["target"], 0o600)
elif rehomed and not execute:
    validate_text = chain_yaml.stdout

env_path = os.path.join(stack, ".env")
env_secrets = {}
if os.path.isfile(env_path):
    with open(env_path, encoding="utf-8") as handle:
        env_secrets = parse_env_secrets(handle.read())
leaked_keys = find_secret_leaks(draft_text, env_secrets)
report["secret_leak"] = bool(leaked_keys)
report["leaked_keys"] = leaked_keys
report["preserves_placeholders"] = "${" in draft_text

with open(work, "w", encoding="utf-8") as handle:
    handle.write(validate_text)
after = render([work], "json", extra, interpolate=True)
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
    and not report["secret_leak"]
)
report["faithful"] = faithful
if execute and faithful:
    if validate_text != draft_text:
        with open(work, "w", encoding="utf-8") as handle:
            handle.write(draft_text)
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
    for key in (
        "inputs",
        "transient_inputs",
        "missing",
        "identical",
        "resolved_image_matches_live",
        "resolved_revision_matches_live",
        "secret_leak",
        "leaked_keys",
        "preserves_placeholders",
        "faithful",
        "bytes",
        "live_revision",
    ):
        if key in result:
            print(f"{key}: {result[key]}")
    if "abort" in result:
        print("ABORT:", result["abort"])
        return 2
    if "written" in result:
        print("written:", result["written"])
        print("swap command (operator decides when):")
        print(
            f"  docker compose --project-directory {args.stack} --env-file {args.stack}/.env "
            f"-p {args.project} -f {result['written']} up -d --no-deps {args.project}"
        )
    elif not result.get("execute"):
        print("dry run: nothing written")
    return 0 if result.get("faithful") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
