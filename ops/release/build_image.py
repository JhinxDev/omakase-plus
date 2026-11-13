"""Build a container image from an exact git commit tree.

The build context is the archive of one commit, never the working tree, so an
image can only contain committed content. A dirty checkout therefore cannot
leak into a release.

The revision is derived from the ``src/omakase/plus`` tree alone. Two commits
that ship the same application tree get the same revision, and any change to a
shipped file produces a new one. The image carries that revision in the
``org.opencontainers.image.revision`` label and the manifest records the git
commit plus a sha256 for every shipped file.

Commands, from the repository root:

    python ops/release/build_image.py --dry-run --manifest release/manifest.json
    python ops/release/build_image.py --commit <sha> --manifest release/manifest.json
    python ops/release/build_image.py --commit <sha> --remote omakase-ct --readback

``--dry-run`` writes the manifest and prints the plan without touching Docker.
A real build also proves that the image content equals the commit tree by
hashing every installed file inside the built image.

``--remote HOST`` builds on the Docker host instead of the workstation. The
context is still the exact commit archive, streamed over ssh, and the host free
space is checked against the minimum before the build starts. The resulting
image carries the revision label and is verified with the same in-image hash
readback.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

SCHEMA = "omakase.release.manifest/1"
REVISION_PREFIX = "debrid-reliability-"
REVISION_HEX_LENGTH = 12
REVISION_LABEL = "org.opencontainers.image.revision"
IMAGE_REPOSITORY = "omakase-plus"
PACKAGE_TREE = "src/omakase/plus"
SOURCE_URL = "https://github.com/JhinxDev/omakase-plus"
REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_FREE_BYTES = 4 * 1024**3
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
# pip compiles bytecode during install, so an image legitimately contains derived
# files that are not part of the shipped tree.
RUNTIME_MARKERS = ("__pycache__",)
RUNTIME_SUFFIXES = (".pyc", ".pyo")

HASH_READER = """
import hashlib, importlib.util, json, pathlib

spec = importlib.util.find_spec("omakase.plus")
if spec is None or spec.origin is None:
    raise SystemExit("omakase.plus is not importable inside this image")
root = pathlib.Path(spec.origin).parent
files = {}
for path in sorted(root.rglob("*")):
    if path.is_file():
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({"package": str(root), "files": files}))
"""

TOOL_READBACK = """
import importlib, importlib.metadata, json, shutil, subprocess

report = {}
for binary in ("tesseract", "mkvmerge", "ffmpeg"):
    path = shutil.which(binary)
    if path is None:
        raise SystemExit("missing binary " + binary)
    output = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
    lines = (output.stdout or output.stderr).splitlines()
    report[binary] = {"path": path, "version": lines[0].strip() if lines else "unknown"}
for module, distribution in (("faster_whisper", "faster-whisper"), ("pgsrip", "pgsrip")):
    importlib.import_module(module)
    report[module] = {"version": importlib.metadata.version(distribution)}
print(json.dumps(report))
"""


class ReleaseError(RuntimeError):
    """A guarded build step failed."""


def canonical_json(value: object) -> bytes:
    """Encode a value as sorted, compact JSON with one trailing newline."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(files: Mapping[str, bytes]) -> dict[str, str]:
    """Return per-file sha256 hashes keyed by package relative path."""
    return {name: sha256_bytes(files[name]) for name in sorted(files)}


def derive_revision(hashes: Mapping[str, str]) -> str:
    """Derive the deterministic revision from package tree hashes."""
    digest = sha256_bytes(canonical_json(dict(sorted(hashes.items()))))
    return REVISION_PREFIX + digest[:REVISION_HEX_LENGTH]


def build_manifest(commit: str, hashes: Mapping[str, str]) -> dict[str, object]:
    """Build the deterministic JSON manifest for one commit."""
    files = dict(sorted(hashes.items()))
    return {
        "schema": SCHEMA,
        "git_commit": commit,
        "revision": derive_revision(files),
        "source": SOURCE_URL,
        "package_tree": PACKAGE_TREE,
        "file_count": len(files),
        "files": files,
    }


def manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    """Encode a manifest as stable, human readable JSON."""
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True)
    return (text + "\n").encode("utf-8")


def _git(repo: Path, *args: str, timeout: int = 300, stdin: bytes | None = None) -> bytes:
    command = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(command, input=stdin, capture_output=True, timeout=timeout)
    except FileNotFoundError as error:
        raise ReleaseError("git is not available on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ReleaseError(f"git {args[0]} timed out after {timeout}s") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"git {args[0]} failed: {detail or 'no detail'}")
    return result.stdout


def resolve_commit(repo: Path, reference: str) -> str:
    """Resolve a reference to a full commit id."""
    output = _git(repo, "rev-parse", "--verify", f"{reference}^{{commit}}")
    return output.decode("ascii").strip()


def _tree_entries(repo: Path, commit: str, package: str | None) -> list[tuple[str, str, str]]:
    """Return (path, mode, object id) for every blob under one path at a commit."""
    pathspec = ("--", package) if package is not None else ()
    listing = _git(repo, "ls-tree", "-r", "-z", commit, *pathspec)
    entries: list[tuple[str, str, str]] = []
    for record in listing.decode("utf-8").split("\0"):
        if not record:
            continue
        meta, separator, name = record.partition("\t")
        fields = meta.split()
        if not separator or not name or len(fields) != 3:
            raise ReleaseError("unreadable git tree record")
        mode, kind, oid = fields
        if kind != "blob":
            continue
        if mode not in ("100644", "100755"):
            raise ReleaseError(f"unsupported tree mode {mode} for {name}")
        entries.append((name, mode, oid))
    return entries


def _blob_contents(repo: Path, object_ids: Sequence[str]) -> dict[str, bytes]:
    """Read raw blob bytes, which no end of line or clean filter has touched."""
    request = "".join(f"{oid}\n" for oid in object_ids).encode("ascii")
    output = _git(repo, "cat-file", "--batch", stdin=request)
    contents: dict[str, bytes] = {}
    position = 0
    for oid in object_ids:
        newline = output.find(b"\n", position)
        if newline < 0:
            raise ReleaseError("truncated git object stream")
        header = output[position:newline].decode("ascii", "replace")
        fields = header.split(" ")
        if len(fields) != 3 or fields[0] != oid or fields[1] != "blob":
            raise ReleaseError(f"unexpected git object header {header!r}")
        size = int(fields[2])
        start = newline + 1
        data = output[start : start + size]
        if len(data) != size:
            raise ReleaseError("truncated git object content")
        digest = hashlib.sha1(b"blob %d\0" % size + data).hexdigest()
        if digest != oid:
            raise ReleaseError(f"git object {oid} failed its content check")
        contents[oid] = data
        position = start + size + 1
    return contents


def commit_tree(
    repo: Path, commit: str, package: str | None = None
) -> dict[str, tuple[str, bytes]]:
    """Return path to (mode, exact bytes) for a commit or one path inside it."""
    entries = _tree_entries(repo, commit, package)
    if not entries:
        raise ReleaseError(f"no files under {package or 'the repository'} at {commit}")
    contents = _blob_contents(repo, list(dict.fromkeys(oid for _, _, oid in entries)))
    return {name: (mode, contents[oid]) for name, mode, oid in entries}


def tree_files(repo: Path, commit: str, package: str = PACKAGE_TREE) -> dict[str, bytes]:
    """Read the exact bytes of one package directory at a commit."""
    tree = commit_tree(repo, commit, package)
    prefix = package.rstrip("/") + "/"
    files: dict[str, bytes] = {}
    for name, (_mode, data) in tree.items():
        if not name.startswith(prefix):
            raise ReleaseError(f"unexpected tree path {name!r}")
        files[name[len(prefix) :]] = data
    return files


def commit_archive(repo: Path, commit: str) -> bytes:
    """Build a deterministic tar of the commit, byte for byte, for a docker build context."""
    tree = commit_tree(repo, commit)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in sorted(tree):
            mode, data = tree[name]
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if mode == "100755" else 0o644
            info.mtime = 0
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _docker(*args: str, timeout: int = 900, stdin: bytes | None = None) -> bytes:
    command = ["docker", *args]
    try:
        result = subprocess.run(command, input=stdin, capture_output=True, timeout=timeout)
    except FileNotFoundError as error:
        raise ReleaseError("docker is not available on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ReleaseError(f"docker {args[0]} timed out after {timeout}s") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"docker {args[0]} failed:\n{detail[-4000:]}")
    return result.stdout


def image_details(reference: str) -> dict[str, object]:
    """Return the inspect record for one local image."""
    details = json.loads(_docker("image", "inspect", reference, timeout=120))
    if not isinstance(details, list) or not details:
        raise ReleaseError(f"image {reference} is not present locally")
    record = details[0]
    if not isinstance(record, dict):
        raise ReleaseError(f"image {reference} has an unreadable inspect record")
    return record


def image_label(details: Mapping[str, object]) -> str | None:
    """Return the release revision label from an inspect record."""
    config = details.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        return None
    value = labels.get(REVISION_LABEL)
    return value if isinstance(value, str) else None


def compare_trees(
    expected: Mapping[str, str], actual: Mapping[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Return changed, missing and unexpected paths between two hash maps."""
    changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    return changed, missing, unexpected


def is_runtime_artifact(path: str) -> bool:
    """Return True for interpreter bytecode, which is derived and not shipped content."""
    return path.endswith(RUNTIME_SUFFIXES) or any(marker in path for marker in RUNTIME_MARKERS)


def shipped_files(hashes: Mapping[str, str]) -> dict[str, str]:
    """Drop derived bytecode from a hash map."""
    return {path: digest for path, digest in hashes.items() if not is_runtime_artifact(path)}


def verify_tree(record: object, expected: Mapping[str, str]) -> dict[str, object]:
    """Compare an in-image hash readback with the commit tree and refuse a mismatch."""
    observed = record.get("files") if isinstance(record, dict) else None
    if not isinstance(observed, dict):
        raise ReleaseError("could not read installed files from the built image")
    changed, missing, unexpected = compare_trees(shipped_files(expected), shipped_files(observed))
    if changed or missing or unexpected:
        problems = []
        if missing:
            problems.append(f"missing from image: {', '.join(missing[:10])}")
        if unexpected:
            problems.append(f"not in the commit tree: {', '.join(unexpected[:10])}")
        if changed:
            problems.append(f"content differs: {', '.join(changed[:10])}")
        raise ReleaseError("image content does not match the commit tree: " + "; ".join(problems))
    return record


def verify_image(reference: str, expected: Mapping[str, str]) -> dict[str, object]:
    """Prove that the installed package inside an image equals the commit tree."""
    raw = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "python",
        reference,
        "-B",
        "-c",
        HASH_READER,
        timeout=300,
    )
    return verify_tree(json.loads(raw), expected)


def tool_readback(reference: str) -> dict[str, object]:
    """Read back the subtitle tooling inside a local image."""
    raw = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--entrypoint",
        "python",
        reference,
        "-B",
        "-c",
        TOOL_READBACK,
        timeout=300,
    )
    return json.loads(raw)


def disk_refusal(available: int, minimum: int = MIN_FREE_BYTES) -> str | None:
    """Refuse a build when the target host has less than the minimum free space."""
    if available < minimum:
        return f"refusing to build: {available} bytes free is under the {minimum} byte minimum"
    return None


def _ssh(host: str, command: str, *, stdin: bytes | None = None, timeout: int = 900) -> bytes:
    try:
        result = subprocess.run(
            ["ssh", *SSH_OPTIONS, host, command],
            input=stdin,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise ReleaseError("ssh is not available on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ReleaseError(f"remote command timed out after {timeout}s") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"remote command failed on {host}:\n{detail[-4000:]}")
    return result.stdout


def remote_free_bytes(host: str, path: str = "/var/lib/docker") -> int:
    """Read free bytes on the Docker host without changing anything."""
    command = f"df -B1 --output=avail {shlex.quote(path)} | tail -1"
    return int(_ssh(host, command, timeout=60).decode("ascii").strip())


def remote_docker_root(host: str) -> str:
    """Read the Docker root directory on the host."""
    return _ssh(host, "docker info --format '{{.DockerRootDir}}'", timeout=60).decode().strip()


def remote_image_details(host: str, reference: str) -> dict[str, object]:
    """Return the inspect record for one image on the host."""
    raw = _ssh(host, shlex.join(["docker", "image", "inspect", reference]), timeout=120)
    details = json.loads(raw)
    if not isinstance(details, list) or not details or not isinstance(details[0], dict):
        raise ReleaseError(f"image {reference} is not present on {host}")
    return details[0]


def remote_build_image(host: str, context: bytes, revision: str, tag: str, timeout: int) -> None:
    """Build the exact commit context on the Docker host over ssh."""
    command = shlex.join(
        [
            "docker",
            "build",
            "--network",
            "host",
            "--build-arg",
            f"OMAKASE_REVISION={revision}",
            "--label",
            f"{REVISION_LABEL}={revision}",
            "--tag",
            tag,
            "-",
        ]
    )
    _ssh(host, command, stdin=context, timeout=timeout)


def remote_verify_image(
    host: str, reference: str, expected: Mapping[str, str]
) -> dict[str, object]:
    """Hash the installed package inside a host image and compare with the tree."""
    command = shlex.join(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "python",
            reference,
            "-B",
            "-c",
            HASH_READER,
        ]
    )
    return verify_tree(json.loads(_ssh(host, command, timeout=300)), expected)


def remote_tool_readback(host: str, reference: str) -> dict[str, object]:
    """Read back the subtitle tooling inside a host image."""
    command = shlex.join(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            reference,
            "-B",
            "-c",
            TOOL_READBACK,
        ]
    )
    return json.loads(_ssh(host, command, timeout=300))


def build_image(repo: Path, commit: str, revision: str, tag: str, timeout: int) -> str:
    """Build the commit archive with the repository Dockerfile and return the image id."""
    context = commit_archive(repo, commit)
    _docker(
        "build",
        "--network",
        "host",
        "--build-arg",
        f"OMAKASE_REVISION={revision}",
        "--label",
        f"{REVISION_LABEL}={revision}",
        "--tag",
        tag,
        "-",
        stdin=context,
        timeout=timeout,
    )
    return str(image_details(tag).get("Id", ""))


def write_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest_bytes(manifest))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an image from an exact git commit tree.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--commit", default="HEAD", help="commit or ref to build")
    parser.add_argument("--tag", default=None, help="image tag, default omakase-plus:<revision>")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest output path")
    parser.add_argument(
        "--remote",
        default=None,
        help="build on this ssh host instead of the local Docker daemon",
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=MIN_FREE_BYTES,
        help="refuse the build when the target has less free space than this",
    )
    parser.add_argument(
        "--readback",
        action="store_true",
        help="read back tesseract, mkvmerge, faster_whisper and pgsrip from the image",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="write the manifest and print the plan without building",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="skip the post build content comparison (not recommended)",
    )
    parser.add_argument("--timeout", type=int, default=1800, help="docker build timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        commit = resolve_commit(args.repo, args.commit)
        files = tree_files(args.repo, commit)
        hashes = file_hashes(files)
        revision = derive_revision(hashes)
        tag = args.tag or f"{IMAGE_REPOSITORY}:{revision}"
        manifest = build_manifest(commit, hashes)
        if args.manifest is not None:
            write_manifest(args.manifest, manifest)
        plan: dict[str, object] = {
            "mode": "dry-run" if args.dry_run else "build",
            "repo": str(args.repo),
            "git_commit": commit,
            "package_tree": PACKAGE_TREE,
            "file_count": len(hashes),
            "revision": revision,
            "tag": tag,
            "manifest": str(args.manifest) if args.manifest is not None else None,
        }
        if args.remote is not None:
            docker_root = remote_docker_root(args.remote)
            room = remote_free_bytes(args.remote, docker_root)
            plan.update(
                {
                    "remote": args.remote,
                    "docker_root": docker_root,
                    "available_bytes_before": room,
                    "min_free_bytes": args.min_free_bytes,
                }
            )
            refusal = disk_refusal(room, args.min_free_bytes)
            if refusal:
                raise ReleaseError(refusal)
            if args.dry_run:
                plan["image"] = None
                plan["verified_files"] = 0
                print(json.dumps(plan, indent=2))
                print(
                    "DRY RUN: manifest written and the remote disk gate passed; no image was built",
                    file=sys.stderr,
                )
                return 0
            remote_build_image(
                args.remote, commit_archive(args.repo, commit), revision, tag, args.timeout
            )
            details = remote_image_details(args.remote, tag)
            image = str(details.get("Id", ""))
            label = image_label(details)
            if label != revision:
                raise ReleaseError(f"image label {label!r} does not match revision {revision!r}")
            plan["image"] = image
            plan["label"] = label
            plan["image_size_bytes"] = details.get("Size")
            if not args.skip_verify:
                remote_verify_image(args.remote, tag, hashes)
                plan["verified_files"] = len(hashes)
            if args.readback:
                plan["readback"] = remote_tool_readback(args.remote, tag)
            plan["available_bytes_after"] = remote_free_bytes(args.remote, docker_root)
            print(json.dumps(plan, indent=2))
            return 0
        if args.dry_run:
            plan["image"] = None
            plan["verified_files"] = 0
            print(json.dumps(plan, indent=2))
            print("DRY RUN: manifest written, no image was built", file=sys.stderr)
            return 0
        image = build_image(args.repo, commit, revision, tag, args.timeout)
        details = image_details(image)
        label = image_label(details)
        if label != revision:
            raise ReleaseError(f"image label {label!r} does not match revision {revision!r}")
        plan["image"] = image
        plan["label"] = label
        plan["image_size_bytes"] = details.get("Size")
        plan["verified_files"] = 0
        if not args.skip_verify:
            verify_image(image, hashes)
            plan["verified_files"] = len(hashes)
        if args.readback:
            plan["readback"] = tool_readback(image)
        print(json.dumps(plan, indent=2))
        return 0
    except ReleaseError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
