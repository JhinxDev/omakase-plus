"""Reclaim release debris on the Docker host without breaking the compose chain.

Every release appends an overlay file to the running stack's compose invocation,
and each overlay lives inside a historical release, receipt or backup directory.
Those directories can therefore never be deleted by age alone: the next restart
would fail with a missing Compose input. This tool reads the live container's
``com.docker.compose.project.config_files`` label, treats every directory that
holds one of those files as protected, and only deletes what is left.

Anything that is not protected and is older than the recent rollback window is
removed, along with old release images, stopped probe containers and known
one-off media artifacts. The default is a dry run; ``--execute`` is required to
delete.

Usage, from the repository root:

    python ops/release/retention.py                  # report only
    python ops/release/retention.py --execute        # reclaim
    python ops/release/retention.py --remote omakase-ct --execute
"""

from __future__ import annotations

import argparse
import json
import posixpath
import subprocess
import sys
from dataclasses import dataclass, field

MANAGED_ROOTS = ("backups", "receipts", "releases", "reviews", "qualification")
# Keep this many of the newest unprotected directories per root even when the
# compose chain does not reference them, so a manual rollback still has a target.
KEEP_RECENT = 3
# Keep the live image and this many previous release images.
KEEP_IMAGES = 3
ARTIFACT_GLOBS = (
    "*.failed-candidate-*",
    "*.db.pre-*",
    "*.ts",
    "*-test.tgz",
    "mob-hls.tgz",
    "mobland-asr.wav",
)
STALE_CONTAINER_MARKERS = (
    "probe",
    "review",
    "staging",
    "snapshot",
    "qualification",
    "benchmark",
)


@dataclass
class Plan:
    protected_files: list[str] = field(default_factory=list)
    protected_dirs: list[str] = field(default_factory=list)
    delete_dirs: list[dict] = field(default_factory=list)
    keep_dirs: list[dict] = field(default_factory=list)
    delete_images: list[str] = field(default_factory=list)
    keep_images: list[str] = field(default_factory=list)
    delete_containers: list[str] = field(default_factory=list)
    delete_artifacts: list[dict] = field(default_factory=list)
    survivors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def reclaim_bytes(self) -> int:
        return sum(item["bytes"] for item in self.delete_dirs + self.delete_artifacts)


def protected_directories(compose_files: list[str], stack_dir: str) -> set[str]:
    """Return every directory that holds a live compose input."""
    protected = {stack_dir.rstrip("/")}
    for raw in compose_files:
        if not raw:
            continue
        protected.add(posixpath.dirname(raw))
    return protected


def select_directory_victims(
    entries: list[dict], protected: set[str], keep_recent: int = KEEP_RECENT
) -> tuple[list[dict], list[dict]]:
    """Split directory entries into (delete, keep) given protection rules."""
    ordered = sorted(entries, key=lambda item: item["mtime"], reverse=True)
    keep: list[dict] = []
    delete: list[dict] = []
    seen_recent = 0
    for entry in ordered:
        if entry["path"].rstrip("/") in protected:
            entry["why"] = "compose input"
            keep.append(entry)
            continue
        if seen_recent < keep_recent:
            seen_recent += 1
            entry["why"] = "recent rollback window"
            keep.append(entry)
            continue
        entry["why"] = "unprotected and outside the rollback window"
        delete.append(entry)
    return delete, keep


REMOTE = r"""
import base64, json, os, shutil, subprocess, sys

payload = json.loads(base64.b64decode(sys.argv[1]))
stack = payload["stack"]
execute = payload["execute"]
roots = payload["roots"]
keep_recent = payload["keep_recent"]
keep_images = payload["keep_images"]
globs = payload["globs"]
markers = payload["markers"]


def run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def size_of(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


def directory_entries(root):
    entries = []
    if not os.path.isdir(root):
        return entries
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        entries.append({"path": path, "mtime": mtime, "bytes": size_of(path)})
    return entries


raw = run(["docker", "inspect", "omakase-plus"]).stdout
labels = (json.loads(raw)[0]["Config"].get("Labels") or {}) if raw else {}
compose_files = [item for item in (labels.get("com.docker.compose.project.config_files") or "").split(",") if item]
protected = set()
protected.add(stack.rstrip("/"))
for item in compose_files:
    protected.add(os.path.dirname(item))
result = {"protected_files": compose_files, "protected_dirs": sorted(protected), "delete_dirs": [],
          "keep_dirs": [], "delete_images": [], "keep_images": [], "delete_containers": [],
          "delete_artifacts": [], "notes": []}
for name in roots:
    entries = directory_entries(os.path.join(stack, name))
    ordered = sorted(entries, key=lambda item: item["mtime"], reverse=True)
    seen = 0
    for entry in ordered:
        if entry["path"].rstrip("/") in protected:
            entry["why"] = "compose input"
            result["keep_dirs"].append(entry)
            continue
        if seen < keep_recent:
            seen += 1
            entry["why"] = "recent rollback window"
            result["keep_dirs"].append(entry)
            continue
        entry["why"] = "unprotected, outside rollback window"
        result["delete_dirs"].append(entry)

live = run(["docker", "inspect", "--format", "{{.Image}}", "omakase-plus"]).stdout.strip()
images = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}"]).stdout
candidates = []
for line in images.splitlines():
    parts = line.split()
    if len(parts) < 3:
        continue
    tag = parts[0]
    if tag.startswith("omakase-plus-debrid-reliability:") or tag == "omakase-plus:<none>":
        candidates.append((tag, parts[1]))
keep_ids = set()
for tag, image_id in candidates[:keep_images]:
    keep_ids.add(image_id)
    result["keep_images"].append(tag)
for tag, image_id in candidates:
    if image_id in keep_ids:
        continue
    inspect = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag]).stdout.strip()
    if inspect == live:
        result["keep_images"].append(tag)
        continue
    result["delete_images"].append(tag)

containers = run(["docker", "ps", "-a", "--filter", "status=exited", "--format", "{{.Names}}"]).stdout
for name in containers.splitlines():
    lowered = name.lower()
    if any(marker in lowered for marker in markers):
        result["delete_containers"].append(name)

data_dir = os.path.join(stack, "data")
if os.path.isdir(data_dir):
    for pattern in globs:
        for path in sorted(__import__("glob").glob(os.path.join(data_dir, pattern))):
            try:
                result["delete_artifacts"].append({"path": path, "bytes": os.lstat(path).st_size})
            except OSError:
                continue

if execute:
    for entry in result["delete_dirs"]:
        shutil.rmtree(entry["path"], ignore_errors=True)
    for path in result["delete_artifacts"]:
        try:
            os.unlink(path["path"])
        except OSError:
            pass
    for name in result["delete_containers"]:
        run(["docker", "rm", name])
    for tag in result["delete_images"]:
        run(["docker", "rmi", tag])
    result["executed"] = True
else:
    result["executed"] = False
result["free_bytes_before"] = shutil.disk_usage("/").free
result["free_bytes_after"] = shutil.disk_usage("/").free
print(json.dumps(result))
"""


def remote(host: str, payload: dict) -> dict:
    import base64

    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    process = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3 - " + encoded],
        input=REMOTE,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if process.returncode != 0:
        raise SystemExit(process.stderr[-2000:] or "remote retention failed")
    return json.loads(process.stdout)


def human(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default="/opt/stacks/omakase-plus")
    parser.add_argument("--remote", default=None, help="SSH alias of the Docker host")
    parser.add_argument("--execute", action="store_true", help="delete, instead of reporting")
    args = parser.parse_args(argv)
    payload = {
        "stack": args.stack,
        "execute": args.execute,
        "roots": list(MANAGED_ROOTS),
        "keep_recent": KEEP_RECENT,
        "keep_images": KEEP_IMAGES,
        "globs": list(ARTIFACT_GLOBS),
        "markers": list(STALE_CONTAINER_MARKERS),
    }
    if args.remote:
        result = remote(args.remote, payload)
    else:
        result = json.loads(
            subprocess.run(
                [sys.executable, "-c", REMOTE],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    removable = sum(item["bytes"] for item in result["delete_dirs"]) + sum(
        item["bytes"] for item in result["delete_artifacts"]
    )
    print(f"compose inputs protected: {len(result['protected_files'])}")
    print(f"directories kept:         {len(result['keep_dirs'])}")
    print(f"directories removable:    {len(result['delete_dirs'])}  {human(removable)}")
    print(f"images kept:              {len(result['keep_images'])}")
    print(f"images removable:         {len(result['delete_images'])}")
    print(f"containers removable:     {len(result['delete_containers'])}")
    print(f"artifact files removable: {len(result['delete_artifacts'])}")
    for entry in result["delete_dirs"][:12]:
        print(f"  rmdir {entry['path']}  {human(entry['bytes'])}")
    for path in result["delete_artifacts"][:12]:
        print(f"  rm    {path['path']}  {human(path['bytes'])}")
    stamp = "executed" if result.get("executed") else "dry run"
    print(
        f"{stamp}: free before {human(result['free_bytes_before'])} "
        f"after {human(result['free_bytes_after'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
