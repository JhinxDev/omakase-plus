"""Guarded cutover for the Omakase Plus container. Dry run by default.

This ports the behaviour of the hand edited production scripts
``omakase-tv-seek-overlap-20261111/promote.py`` and
``omakase-rd-complete-20261111/release.py`` into version control. It is invoked
from a workstation and executed on the Docker host over ssh. The script and the
release manifest travel together, so the host does not need the repository and
nothing is copied to it beyond the process input.

Guards, in order:
  * the running service is healthy and its image label matches its health revision
  * the candidate image exists on the host and carries the manifest revision label
  * the live compose chain is the reviewed one and the diff is image plus revision only
  * the player and browser are idle and the schema is not mid migration
  * the database is snapshotted, verified, compressed, fsynced and hashed before any swap
  * after the swap the image, health revision, HTTPS authorization behaviour,
    schema, linked device count and every shipped file hash are read back
  * any failure after the swap begins recreates the previous compose chain automatically

Commands, from the repository root:

    python ops/release/promote.py --manifest release/manifest.json
    python ops/release/promote.py --manifest release/manifest.json --execute
    python ops/release/promote.py --manifest release/manifest.json --rollback --execute

The default is a dry run: it performs the read only preflight and prints the
plan. ``--execute`` is required for any container, database or file change.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import lzma
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REVISION_PREFIX = "debrid-reliability-"
REVISION_HEX_LENGTH = 12
REVISION_LABEL = "org.opencontainers.image.revision"
MANIFEST_SCHEMA = "omakase.release.manifest/1"
RECEIPT_SCHEMA = "omakase.release.receipt/1"
IMAGE_REPOSITORY = "omakase-plus"
DEFAULT_STACK_DIR = Path("/opt/stacks/omakase-plus")
DEFAULT_SERVICE = "omakase-plus"
DEFAULT_HOST = "omakase-ct"
DEFAULT_ORIGIN = "https://anime.jhinx.dev"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8766/plus/health"
DEFAULT_RESERVE_BYTES = 4 * 1024**3
DEFAULT_BACKUP_BYTES = 26 * 1024**2
LOCK_NAME = ".omakase-release.lock"
FORCE_IDLE_FLAG = Path("/tmp/omakase-release-force-idle")
RUNTIME_MARKERS = ("__pycache__",)
RUNTIME_SUFFIXES = (".pyc", ".pyo")
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")

# Reads the payload from stdin, then runs this same file with the embedded manifest.
EMBEDDED_BOOTSTRAP = """
import json, sys, types

payload = json.load(sys.stdin)
module = types.ModuleType("omakase_promote")
module.__file__ = "promote.py"
sys.modules["omakase_promote"] = module
exec(compile(payload["source"], "promote.py", "exec"), module.__dict__)
sys.exit(module.main(payload["argv"], payload["manifest"]))
"""


class ReleaseError(RuntimeError):
    """A guarded cutover step refused or failed."""


@dataclass(frozen=True)
class Settings:
    """Runtime paths and limits for one host."""

    stack_dir: Path
    service: str
    host: str
    origin: str
    health_url: str
    db_path: Path
    reserve_bytes: int
    backup_bytes: int
    timeout: int
    allow_migrations: tuple[str, ...]
    expect_last_migration: str | None

    @property
    def backup_root(self) -> Path:
        return self.stack_dir / "backups"

    def state_dir(self, revision: str) -> Path:
        return self.backup_root / revision


# Pure decision helpers. These are the reviewed gates and are unit tested without
# Docker, ssh or a database.


def is_runtime_artifact(path: str) -> bool:
    """Return True for interpreter bytecode, which the image never ships."""
    return path.endswith(RUNTIME_SUFFIXES) or any(marker in path for marker in RUNTIME_MARKERS)


def normalize_environment(environment: object) -> dict[str, str | None]:
    """Normalise a compose environment entry to a mapping without losing empty values."""
    if environment is None:
        return {}
    if isinstance(environment, dict):
        return {str(name): value for name, value in environment.items()}
    if isinstance(environment, list):
        entries = [str(item).partition("=") for item in environment]
        names = [name for name, _, _ in entries]
        if len(set(names)) != len(names):
            raise ReleaseError("compose environment has a duplicate key")
        return {name: (value if separator else None) for name, separator, value in entries}
    raise ReleaseError("compose environment has an unsupported shape")


def changed_paths(left: object, right: object, prefix: str = "") -> list[str]:
    """List differing compose keys by path. Values are never returned."""
    paths: list[str] = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                paths.append(f"{prefix}{key} (added)")
            elif key not in right:
                paths.append(f"{prefix}{key} (removed)")
            else:
                paths.extend(changed_paths(left[key], right[key], f"{prefix}{key}."))
    elif left != right:
        paths.append(prefix.rstrip(".") + " (changed)")
    return paths


def deploy_override(service: str, image: str, revision: str) -> dict[str, object]:
    """Build the compose override that changes only the image and revision."""
    return {"services": {service: {"image": image, "environment": {"OMAKASE_REVISION": revision}}}}


def compose_scope_error(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    service: str,
    expected_before_image: str,
    expected_after_image: str,
    expected_flags: Mapping[str, str],
    expected_before_flags: Mapping[str, str],
) -> str | None:
    """Return a refusal message when the compose diff is not image and revision only."""
    prior_services = before.get("services")
    next_services = after.get("services")
    if not isinstance(prior_services, dict) or not isinstance(next_services, dict):
        return "compose config has no services mapping"
    prior_app = prior_services.get(service)
    next_app = next_services.get(service)
    if not isinstance(prior_app, dict) or not isinstance(next_app, dict):
        return f"compose config has no {service} service"
    prior_app = dict(prior_app)
    next_app = dict(next_app)
    prior_app["environment"] = normalize_environment(prior_app.get("environment"))
    next_app["environment"] = normalize_environment(next_app.get("environment"))
    if prior_app.pop("image", None) != expected_before_image:
        return f"live compose image is not {expected_before_image}"
    if next_app.pop("image", None) != expected_after_image:
        return f"candidate compose image is not {expected_after_image}"
    for name, value in expected_before_flags.items():
        if prior_app["environment"].pop(name, _MISSING) != value:
            return f"live flag {name} is not the reviewed value"
    for name, value in expected_flags.items():
        if next_app["environment"].pop(name, _MISSING) != value:
            return f"candidate flag {name} was not applied"
    if prior_app != next_app:
        detail = ", ".join(changed_paths(prior_app, next_app, f"services.{service}."))
        return f"compose diff extends beyond the image and revision: {detail}"
    prior_other = {key: value for key, value in before.items() if key != "services"}
    next_other = {key: value for key, value in after.items() if key != "services"}
    if prior_other != next_other:
        detail = ", ".join(changed_paths(prior_other, next_other))
        return f"compose diff changes top level fields: {detail}"
    prior_rest = {key: value for key, value in prior_services.items() if key != service}
    next_rest = {key: value for key, value in next_services.items() if key != service}
    if prior_rest != next_rest:
        detail = ", ".join(changed_paths(prior_rest, next_rest))
        return f"compose diff touches other services: {detail}"
    return None


def disk_gate_error(available: int, reserve_bytes: int, backup_bytes: int = 0) -> str | None:
    """Refuse when free space cannot hold the reserve plus the measured backup."""
    required = reserve_bytes + backup_bytes
    if available <= required:
        return f"need {required} bytes including the backup; {available} available"
    return None


def migration_error(
    before: Sequence[str], after: Sequence[str], allowed: Sequence[str] = ()
) -> str | None:
    """Refuse when the applied migration set is not the reviewed set plus allowances."""
    expected = list(before) + [name for name in allowed if name not in before]
    if list(after) != expected:
        return f"unexpected migration set: expected {len(expected)} entries, saw {len(after)}"
    return None


def rollback_decision(*, cutover_started: bool, candidate_healthy: bool) -> bool:
    """Restore the previous chain whenever a cutover started and the candidate is unproven."""
    return bool(cutover_started) and not candidate_healthy


def native_playback_active(lines: Sequence[str], *, force_idle: bool) -> bool:
    """Detect recent native playback traffic in container logs."""
    if force_idle:
        return False
    for line in lines:
        if "/plus/api/native-tv-test/v1/media/" in line:
            return True
        if "/plus/api/native-tv-test/v1/library/media/" in line and "/resume " in line:
            return True
    return False


def health_ok(payload: Mapping[str, object], revision: str) -> bool:
    """Return True only for the exact revision advertised over HTTP health."""
    return payload.get("status") == "ok" and payload.get("revision") == revision


def parse_compose_files(label: str | None) -> tuple[str, ...]:
    """Split a compose project config_files label into paths."""
    return tuple(part.strip() for part in (label or "").split(",") if part.strip())


def tree_problems(expected: Mapping[str, str], observed: Mapping[str, str]) -> list[str]:
    """Return human readable problems between expected and observed file hashes."""
    expected_shipped = {
        name: value for name, value in expected.items() if not is_runtime_artifact(name)
    }
    observed_shipped = {
        name: value for name, value in observed.items() if not is_runtime_artifact(name)
    }
    changed = sorted(
        n
        for n in set(expected_shipped) & set(observed_shipped)
        if expected_shipped[n] != observed_shipped[n]
    )
    missing = sorted(set(expected_shipped) - set(observed_shipped))
    unexpected = sorted(set(observed_shipped) - set(expected_shipped))
    problems = []
    if missing:
        problems.append(f"missing: {', '.join(missing[:10])}")
    if unexpected:
        problems.append(f"unexpected: {', '.join(unexpected[:10])}")
    if changed:
        problems.append(f"content differs: {', '.join(changed[:10])}")
    return problems


_MISSING = object()


# Host side helpers. They run only on the Docker host.


def capture(*command: str, timeout: int = 60) -> str:
    """Run a read only command and return stripped output."""
    return subprocess.check_output(command, text=True, timeout=timeout).strip()


def run(*command: str, cwd: Path | None = None) -> None:
    """Run a command whose output should be visible to the operator."""
    subprocess.run(command, cwd=None if cwd is None else str(cwd), check=True)


def compose_command(settings: Settings, files: Sequence[str]) -> list[str]:
    """Build a docker compose command for one reviewed file chain."""
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(settings.stack_dir),
        "--env-file",
        str(settings.stack_dir / ".env"),
        "-p",
        settings.service,
    ]
    for path in files:
        if not Path(path).is_file():
            raise ReleaseError(f"missing compose input {path}")
        command.extend(("-f", str(path)))
    return command


def compose_config(settings: Settings, files: Sequence[str]) -> dict[str, object]:
    """Resolve a compose chain to JSON without printing secrets."""
    command = compose_command(settings, files)
    run(*command, "config", "--quiet", cwd=settings.stack_dir)
    output = capture(
        *command,
        "config",
        "--no-interpolate",
        "--no-env-resolution",
        "--format",
        "json",
        timeout=120,
    )
    try:
        return json.loads(output)
    except ValueError as error:
        raise ReleaseError("compose config did not return JSON") from error


def image_details(reference: str) -> dict[str, object]:
    """Return the local inspect record for one image reference."""
    output = capture("docker", "image", "inspect", reference, timeout=120)
    records = json.loads(output)
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise ReleaseError(f"image {reference} is not present on this host")
    return records[0]


def image_label(details: Mapping[str, object]) -> str | None:
    """Return the release revision label from an inspect record."""
    config = details.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        return None
    value = labels.get(REVISION_LABEL)
    return value if isinstance(value, str) else None


def service_state(settings: Settings) -> dict[str, object]:
    """Read the live container image, health, revision and compose chain."""
    output = capture("docker", "inspect", settings.service, timeout=60)
    records = json.loads(output)
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise ReleaseError(f"container {settings.service} is not present")
    record = records[0]
    config = record.get("Config") if isinstance(record.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    state = record.get("State") if isinstance(record.get("State"), dict) else {}
    health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
    return {
        "image": record.get("Image"),
        "health": health.get("Status"),
        "revision": labels.get(REVISION_LABEL),
        "config_files": parse_compose_files(labels.get("com.docker.compose.project.config_files")),
    }


def health_payload(settings: Settings) -> dict[str, object]:
    """Read the local health endpoint."""
    with urlopen(settings.health_url, timeout=8) as response:
        payload = json.load(response)
    return payload if isinstance(payload, dict) else {}


def wait_healthy(
    settings: Settings,
    image: str,
    revision: str | None,
    *,
    attempts: int = 40,
    interval: int = 2,
) -> None:
    """Wait until the service runs the exact image, reports healthy and names the revision."""
    if not revision:
        raise ReleaseError("cannot verify health without an expected revision")
    for _ in range(attempts):
        state = service_state(settings)
        if state["image"] == image and state["health"] == "healthy":
            with contextlib.suppress(OSError, ValueError):
                if health_ok(health_payload(settings), revision):
                    print(f"healthy {revision} {image}", flush=True)
                    return
        time.sleep(interval)
    raise ReleaseError("health and revision verification failed")


def disk_gate(settings: Settings, backup_bytes: int = 0) -> int:
    """Enforce the free space reserve and return available bytes."""
    available = shutil.disk_usage(settings.stack_dir).free
    error = disk_gate_error(available, settings.reserve_bytes, backup_bytes)
    if error:
        raise ReleaseError(error)
    return available


def native_device_count(settings: Settings) -> int:
    """Count unexpired linked native devices without writing."""
    with sqlite3.connect(settings.db_path.as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM native_tv_devices "
            "WHERE user_id IS NOT NULL AND expires_at>unixepoch()"
        ).fetchone()
    return int(row[0])


def idle_and_schema(settings: Settings) -> dict[str, object]:
    """Refuse a cutover while playback or cleanup work is active, and read the schema."""
    logs = subprocess.run(
        ["docker", "logs", "--since", "90s", "--tail", "2000", settings.service],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    force_idle = os.getenv("OMAKASE_RELEASE_FORCE_IDLE") == "1" or FORCE_IDLE_FLAG.exists()
    if native_playback_active((logs.stdout + logs.stderr).splitlines(), force_idle=force_idle):
        raise ReleaseError("recent native playback; wait for the player to be idle")
    if force_idle:
        print("WARNING: idle protection overridden by OMAKASE_RELEASE_FORCE_IDLE", flush=True)
    with sqlite3.connect(settings.db_path.as_uri() + "?mode=ro", uri=True) as conn:
        active = conn.execute(
            "SELECT 1 FROM web_playback_sessions WHERE closed=0 "
            "AND expires_at>unixepoch() AND last_seen>unixepoch()-1200 LIMIT 1"
        ).fetchone()
        if active:
            raise ReleaseError("browser playback is active")
        if conn.execute("SELECT count(*) FROM movie_cleanup_jobs").fetchone()[0]:
            raise ReleaseError("movie cleanup jobs are running")
        if conn.execute(
            "SELECT count(*) FROM movie_cleanup_preferences WHERE enabled=1"
        ).fetchone()[0]:
            raise ReleaseError("movie cleanup is enabled")
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        size = page_count * page_size
        if shutil.disk_usage("/dev/shm").free <= size + 128 * 1024**2:
            raise ReleaseError("not enough shared memory for the database snapshot")
        schema = conn.execute("SELECT max(id), count(*) FROM _migrations").fetchone()
        names = [row[0] for row in conn.execute("SELECT name FROM _migrations ORDER BY id")]
    return {"schema": [int(schema[0]), int(schema[1])], "migrations": names}


@contextlib.contextmanager
def durable_sqlite_backup(
    settings: Settings, expected_schema: Sequence[int], state: Path
) -> Iterator[str]:
    """Snapshot, verify, compress, fsync and hash the database before any swap."""
    with tempfile.TemporaryDirectory(prefix="omakase-release-backup-", dir="/dev/shm") as scratch:
        snapshot = Path(scratch) / "pre-swap.db"
        with (
            sqlite3.connect(settings.db_path.as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(snapshot) as backup,
        ):
            source.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ReleaseError("database snapshot failed integrity checking")
            if list(backup.execute("SELECT max(id), count(*) FROM _migrations").fetchone()) != list(
                expected_schema
            ):
                raise ReleaseError("database snapshot schema changed during the backup")
        measured = Path(scratch) / "pre-swap.db.xz"
        compressed = state / measured.name
        with snapshot.open("rb") as saved:
            digest = hashlib.file_digest(saved, "sha256").hexdigest()
            saved.seek(0)
            with lzma.open(measured, "wb", preset=3) as archive:
                shutil.copyfileobj(saved, archive)
        disk_gate(settings, measured.stat().st_size)
        with measured.open("rb") as archive, compressed.open("xb") as durable:
            shutil.copyfileobj(archive, durable)
            durable.flush()
            os.fsync(durable.fileno())
        with lzma.open(compressed, "rb") as archive:
            if hashlib.file_digest(archive, "sha256").hexdigest() != digest:
                raise ReleaseError("compressed database backup failed its checksum")
        receipt = state / "pre-swap.db.sha256"
        with receipt.open("x") as checksum:
            checksum.write(digest + "  pre-swap.db\n")
            checksum.flush()
            os.fsync(checksum.fileno())
        directory_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        disk_gate(settings)
        print(
            f"verified fresh durable SQLite backup {compressed.stat().st_size} bytes",
            flush=True,
        )
        yield digest


def verify_https_authorization(settings: Settings, revision: str) -> None:
    """Verify private HTTPS health plus the native library authorization contract."""
    origin = settings.origin.rstrip("/")
    with urlopen(origin + "/plus/health", timeout=8) as response:
        payload = json.load(response)
    if payload.get("revision") != revision:
        raise ReleaseError(
            f"HTTPS health revision is {payload.get('revision')!r}, expected {revision!r}"
        )
    api = origin + "/plus/api/native-tv-test/v1/library"
    invalid = "0" * 32
    checks: tuple[tuple[Request, int, str | None, bool], ...] = (
        (
            Request(
                api + "/redeem",
                data=b'{"code":""}',
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            404,
            "Apple TV code unavailable",
            True,
        ),
        (Request(api + "/catalog?kind=anime"), 401, "Apple TV library link expired", False),
        (
            Request(
                api + f"/media/{invalid}/resume",
                data=b'{"seq":1,"position_seconds":12}',
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            401,
            None,
            True,
        ),
        (Request(api + "/libraries"), 401, None, True),
        (
            Request(api + "/device?kind=invalid", method="POST"),
            422,
            "Unsupported device kind",
            True,
        ),
        (Request(api + f"/media/{invalid}/access"), 401, "Apple TV library link expired", True),
    )
    for request, status, detail, require_no_store in checks:
        try:
            urlopen(request, timeout=8).close()
        except HTTPError as error:
            if error.code != status:
                raise ReleaseError(
                    f"authorization check for {request.get_method()} {request.full_url} "
                    f"expected {status}, saw {error.code}"
                ) from error
            if require_no_store and "no-store" not in error.headers.get("Cache-Control", ""):
                raise ReleaseError(f"authorization response for {request.full_url} is cacheable")
            if detail is not None:
                body = json.load(error)
                if body.get("detail") != detail:
                    raise ReleaseError(f"authorization detail for {request.full_url} did not match")
        else:
            raise ReleaseError(
                f"authorization check accepted a rejected request: {request.full_url}"
            )
    print(
        "private HTTPS ingress verified device validation and catalog, media, "
        "and resume authorization",
        flush=True,
    )


def verify_deployed_files(settings: Settings, expected: Mapping[str, str]) -> None:
    """Prove that every shipped file inside the running container matches the manifest."""
    locate = (
        "import importlib.util,pathlib;"
        "print(pathlib.Path(importlib.util.find_spec('omakase.plus').origin).parent)"
    )
    package = capture(
        "docker",
        "exec",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        settings.service,
        "python",
        "-B",
        "-c",
        locate,
        timeout=60,
    )
    hash_pass = 'cd "$1" && find . -type f -print0 | sort -z | xargs -0 sha256sum'
    listing = capture(
        "docker", "exec", settings.service, "sh", "-c", hash_pass, "sh", package, timeout=120
    )
    observed: dict[str, str] = {}
    for line in listing.splitlines():
        digest, separator, name = line.partition("  ")
        if separator:
            observed[name.removeprefix("./")] = digest
    problems = tree_problems(expected, observed)
    if problems:
        raise ReleaseError("deployed files do not match the manifest: " + "; ".join(problems))
    shipped = {name for name in expected if not is_runtime_artifact(name)}
    print(f"verified {len(shipped)} shipped files against the manifest", flush=True)


def verify_database(
    settings: Settings, before: Mapping[str, object], allowed_migrations: Sequence[str]
) -> dict[str, object]:
    """Verify the schema, integrity and linked device count after the swap."""
    with sqlite3.connect(settings.db_path.as_uri() + "?mode=ro", uri=True) as conn:
        after_names = [row[0] for row in conn.execute("SELECT name FROM _migrations ORDER BY id")]
        error = migration_error(list(before["migrations"]), after_names, allowed_migrations)
        if error:
            raise ReleaseError(error)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReleaseError("database quick_check failed after the cutover")
        for table in ("native_tv_devices", "household_title_requests"):
            if conn.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None:
                raise ReleaseError(f"foreign key check failed for {table}")
    return {"migrations": after_names}


def host_check(settings: Settings) -> int:
    """Print read only host readiness facts. Safe against production."""
    probe: dict[str, object] = {
        "host_python": sys.version.split()[0],
        "docker": capture("docker", "version", "--format", "{{.Server.Version}}"),
        "stack_dir": str(settings.stack_dir),
        "stack_dir_present": settings.stack_dir.is_dir(),
        "env_file_present": (settings.stack_dir / ".env").is_file(),
        "database_present": settings.db_path.is_file(),
        "lock_present": (settings.stack_dir / LOCK_NAME).exists(),
        "service": settings.service,
    }
    try:
        state = service_state(settings)
    except (ReleaseError, OSError, ValueError) as error:
        probe["live"] = f"unreadable: {error}"
    else:
        probe["live"] = {
            "image": state["image"],
            "health": state["health"],
            "revision": state["revision"],
            "compose_files": len(state["config_files"]),
        }
    try:
        probe["health"] = health_payload(settings)
    except (OSError, ValueError) as error:
        probe["health"] = f"unreachable: {error}"
    print(json.dumps(probe, indent=2))
    return 0


def load_manifest(path: Path | None) -> dict[str, object]:
    """Load and validate a release manifest produced by build_image.py."""
    if path is None:
        raise ReleaseError("--manifest is required")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ReleaseError(f"cannot read manifest {path}: {error}") from error
    except ValueError as error:
        raise ReleaseError(f"manifest {path} is not valid JSON") from error
    return validate_manifest(manifest)


def validate_manifest(manifest: object, commit: str | None = None) -> dict[str, object]:
    """Refuse a manifest that is not a complete, revision labelled file map."""
    if not isinstance(manifest, dict):
        raise ReleaseError("manifest is not a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReleaseError("manifest schema is not the release schema")
    revision = manifest.get("revision")
    if (
        not isinstance(revision, str)
        or not revision.startswith(REVISION_PREFIX)
        or len(revision) != len(REVISION_PREFIX) + REVISION_HEX_LENGTH
    ):
        raise ReleaseError("manifest revision is not a deterministic release revision")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError("manifest has no file hashes")
    for name, digest in files.items():
        if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
            raise ReleaseError(f"manifest hash for {name!r} is malformed")
    recorded = manifest.get("git_commit")
    if commit is not None and recorded != commit:
        raise ReleaseError(f"manifest is for commit {recorded!r}, not {commit!r}")
    return manifest


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Build runtime settings from parsed arguments."""
    stack_dir = Path(args.stack_dir)
    db_path = Path(args.db_path) if args.db_path else stack_dir / "data/omakase-plus.db"
    return Settings(
        stack_dir=stack_dir,
        service=args.service,
        host=args.host,
        origin=args.origin,
        health_url=args.health_url,
        db_path=db_path,
        reserve_bytes=args.reserve_bytes,
        backup_bytes=args.backup_bytes,
        timeout=args.timeout,
        allow_migrations=tuple(args.allow_migrations or ()),
        expect_last_migration=args.expect_last_migration,
    )


@contextlib.contextmanager
def acquire_lock(path: Path, *, create: bool):
    """Hold the release lock for the whole guarded sequence."""
    import fcntl

    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileNotFoundError:
        print(
            "no release lock present; the dry run continues without creating one", file=sys.stderr
        )
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(handle)
        raise ReleaseError("another release holds the lock") from error
    try:
        yield
    finally:
        os.close(handle)


def preflight(
    settings: Settings, args: argparse.Namespace, manifest: Mapping[str, object]
) -> dict[str, object]:
    """Run every read only guard and return the cutover plan."""
    revision = str(manifest["revision"])
    files = manifest["files"]
    if not isinstance(files, dict):
        raise ReleaseError("manifest has no file hashes")
    current = service_state(settings)
    previous_image = current["image"]
    previous_revision = current["revision"]
    if not isinstance(previous_image, str) or not previous_image:
        raise ReleaseError("cannot read the live image id")
    if not isinstance(previous_revision, str) or not previous_revision:
        raise ReleaseError("cannot read the live revision label")
    wait_healthy(settings, previous_image, previous_revision)
    candidate_ref = args.image or f"{IMAGE_REPOSITORY}:{revision}"
    candidate = image_details(candidate_ref)
    candidate_image = str(candidate.get("Id", ""))
    if not candidate_image.startswith("sha256:"):
        raise ReleaseError(f"candidate image {candidate_ref} has no image id")
    if candidate_image == previous_image:
        raise ReleaseError("the candidate image is already running")
    if image_label(candidate) != revision:
        raise ReleaseError(f"candidate image label {image_label(candidate)!r} is not {revision!r}")
    compose_files = current["config_files"]
    if not compose_files:
        raise ReleaseError("the live compose chain label is empty")
    before = compose_config(settings, compose_files)
    available = disk_gate(settings, settings.backup_bytes)
    schema = idle_and_schema(settings)
    if args.expect_last_migration is not None:
        migrations = schema["migrations"]
        if not isinstance(migrations, list) or migrations[-1:] != [args.expect_last_migration]:
            raise ReleaseError(f"the live schema does not end with {args.expect_last_migration}")
    devices = native_device_count(settings)
    override = deploy_override(settings.service, candidate_image, revision)
    with tempfile.TemporaryDirectory(prefix="omakase-release-", dir="/dev/shm") as scratch:
        preview = Path(scratch) / "candidate.json"
        preview.write_text(json.dumps(override), encoding="utf-8")
        after = compose_config(settings, (*compose_files, str(preview)))
    error = compose_scope_error(
        before,
        after,
        service=settings.service,
        expected_before_image=previous_image,
        expected_after_image=candidate_image,
        expected_flags={"OMAKASE_REVISION": revision},
        expected_before_flags={"OMAKASE_REVISION": previous_revision},
    )
    if error:
        raise ReleaseError(error)
    return {
        "git_commit": manifest.get("git_commit"),
        "revision": revision,
        "image_ref": candidate_ref,
        "image": candidate_image,
        "previous_image": previous_image,
        "previous_revision": previous_revision,
        "compose_files": list(compose_files),
        "schema": schema,
        "devices": devices,
        "available_bytes": available,
        "manifest": manifest,
        "override": override,
    }


def print_plan(settings: Settings, plan: Mapping[str, object], *, execute: bool) -> None:
    """Print the cutover plan without secrets."""
    schema = plan["schema"]
    summary = {
        "mode": "execute" if execute else "dry-run",
        "host": settings.host,
        "stack_dir": str(settings.stack_dir),
        "service": settings.service,
        "git_commit": plan["git_commit"],
        "revision": plan["revision"],
        "candidate_image": plan["image"],
        "candidate_image_ref": plan["image_ref"],
        "previous_image": plan["previous_image"],
        "previous_revision": plan["previous_revision"],
        "compose_files": plan["compose_files"],
        "database": str(settings.db_path),
        "backup_dir": str(settings.state_dir(str(plan["revision"]))),
        "schema": schema,
        "linked_native_devices": plan["devices"],
        "available_bytes": plan["available_bytes"],
    }
    print(json.dumps(summary, indent=2), flush=True)


def execute_flow(settings: Settings, args: argparse.Namespace, plan: Mapping[str, object]) -> int:
    """Perform the guarded cutover with an automatic rollback."""
    revision = str(plan["revision"])
    manifest = plan["manifest"]
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ReleaseError("manifest has no file hashes")
    files = list(plan["compose_files"])
    state = settings.state_dir(revision)
    try:
        state.mkdir(exist_ok=False)
    except FileExistsError as error:
        raise ReleaseError(
            f"release state {state} already exists; inspect it before retrying"
        ) from error
    (state / "original-compose-files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
    candidate_path = state / "candidate.json"
    override = plan["override"]
    candidate_path.write_text(json.dumps(override), encoding="utf-8")
    before_command = compose_command(settings, files)
    candidate_command = compose_command(settings, (*files, str(candidate_path)))
    schema = plan["schema"]
    if not isinstance(schema, dict) or not isinstance(schema.get("schema"), list):
        raise ReleaseError("preflight did not record the schema")
    idle_and_schema(settings)
    disk_gate(settings, settings.backup_bytes)
    cutover_started = False
    try:
        cutover_started = True
        run(*before_command, "stop", "-t", "20", settings.service)
        with durable_sqlite_backup(settings, schema["schema"], state) as digest:
            run(
                *candidate_command,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--no-build",
                settings.service,
            )
            wait_healthy(settings, str(plan["image"]), revision)
            verify_https_authorization(settings, revision)
            after_schema = verify_database(settings, schema, settings.allow_migrations)
            verify_deployed_files(settings, manifest["files"])
            if native_device_count(settings) != plan["devices"]:
                raise ReleaseError("linked native device count changed during the cutover")
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "revision": revision,
            "image": plan["image"],
            "image_ref": plan["image_ref"],
            "git_commit": plan["git_commit"],
            "previous_image": plan["previous_image"],
            "previous_revision": plan["previous_revision"],
            "compose_files": files,
            "backup": {
                "path": str(state / "pre-swap.db.xz"),
                "sha256": digest,
            },
            "verified_files": len(
                [name for name in manifest["files"] if not is_runtime_artifact(name)]
            ),
            "database_migrations": len(after_schema["migrations"]),
            "available_bytes": disk_gate(settings),
            "health": "ok",
        }
        (state / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(receipt, indent=2, sort_keys=True), flush=True)
        return 0
    except BaseException:
        if rollback_decision(cutover_started=cutover_started, candidate_healthy=False):
            print(
                "cutover failed; restoring the previous compose chain", file=sys.stderr, flush=True
            )
            run(
                *before_command,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--no-build",
                settings.service,
            )
            wait_healthy(settings, str(plan["previous_image"]), str(plan["previous_revision"]))
            print("previous release restored and healthy", file=sys.stderr, flush=True)
        raise


def rollback_flow(settings: Settings, args: argparse.Namespace) -> int:
    """Restore the compose chain recorded for the currently running release."""
    current = service_state(settings)
    revision = current["revision"]
    if not isinstance(revision, str) or not revision:
        raise ReleaseError("cannot read the live revision label")
    if args.image is not None and current["image"] != args.image:
        raise ReleaseError("the live image is not the requested image")
    state = settings.state_dir(revision)
    original = state / "original-compose-files.txt"
    receipt_path = state / "receipt.json"
    for path in (original, receipt_path):
        if not path.is_file():
            raise ReleaseError(f"missing recorded release state {path}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ReleaseError("recorded receipt is not valid JSON") from error
    if not isinstance(receipt, dict) or receipt.get("revision") != revision:
        raise ReleaseError("recorded receipt does not match the live revision")
    if receipt.get("image") != current["image"]:
        raise ReleaseError("live image is not the image recorded for this release")
    files = tuple(line for line in original.read_text(encoding="utf-8").splitlines() if line)
    idle_and_schema(settings)
    print(f"restoring compose chain for {revision}", flush=True)
    run(
        *compose_command(settings, files),
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--no-build",
        settings.service,
    )
    wait_healthy(settings, str(receipt["previous_image"]), str(receipt["previous_revision"]))
    print(
        json.dumps(
            {
                "rolled_back_from": revision,
                "restored_image": receipt["previous_image"],
                "restored_revision": receipt["previous_revision"],
                "compose_files": list(files),
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Guarded cutover for the Omakase Plus container. Dry run by default."
    )
    parser.add_argument("--manifest", type=Path, default=None, help="manifest from build_image.py")
    parser.add_argument("--commit", default=None, help="optional commit cross-check")
    parser.add_argument("--image", default=None, help="candidate image reference override")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="ssh host alias for the workstation entry"
    )
    parser.add_argument(
        "--stack-dir", default=str(DEFAULT_STACK_DIR), help="compose project directory"
    )
    parser.add_argument(
        "--service", default=DEFAULT_SERVICE, help="compose service and container name"
    )
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="public HTTPS origin")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL, help="local health endpoint")
    parser.add_argument("--db-path", default=None, help="SQLite database path")
    parser.add_argument(
        "--allow-migrations",
        action="append",
        default=[],
        help="migration file name allowed to appear during the cutover",
    )
    parser.add_argument(
        "--expect-last-migration",
        default=None,
        help="require the live schema to end with this migration",
    )
    parser.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    parser.add_argument("--backup-bytes", type=int, default=DEFAULT_BACKUP_BYTES)
    parser.add_argument(
        "--timeout", type=int, default=300, help="remote command timeout in seconds"
    )
    parser.add_argument("--execute", action="store_true", help="perform the cutover")
    parser.add_argument(
        "--rollback", action="store_true", help="restore the recorded previous chain"
    )
    parser.add_argument(
        "--check", action="store_true", help="print read only host readiness facts and exit"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="run on this host instead of over ssh, only when already on the Docker host",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, manifest: dict[str, object] | None = None) -> int:
    """Run the guarded sequence. Called directly on the Docker host."""
    args = parse_args(argv)
    try:
        settings = settings_from_args(args)
        if args.check:
            if args.execute:
                raise ReleaseError("--check cannot be combined with --execute")
            return host_check(settings)
        release = manifest if manifest is not None else load_manifest(args.manifest)
        validate_manifest(release, args.commit)
        with acquire_lock(settings.stack_dir / LOCK_NAME, create=args.execute):
            if args.rollback:
                if not args.execute:
                    raise ReleaseError("--rollback requires --execute")
                return rollback_flow(settings, args)
            plan = preflight(settings, args, release)
            print_plan(settings, plan, execute=args.execute)
            if not args.execute:
                print(
                    "DRY RUN: no container, database or file was changed; "
                    "pass --execute to perform the cutover"
                )
                return 0
            return execute_flow(settings, args, plan)
    except ReleaseError as error:
        print(f"promote failed: {error}", file=sys.stderr)
        return 1


def dispatch_remote(argv: list[str]) -> int:
    """Send this script and the manifest to the Docker host over ssh."""
    if "--local" in argv:
        return main([item for item in argv if item != "--local"])
    parsed = parse_args(argv)
    manifest: dict[str, object] | None = None
    if not parsed.check:
        try:
            manifest = load_manifest(parsed.manifest)
        except ReleaseError as error:
            print(f"promote failed: {error}", file=sys.stderr)
            return 1
    source = Path(__file__).read_text(encoding="utf-8")
    payload = {"source": source, "argv": argv, "manifest": manifest}
    command = shlex.join(["python3", "-c", EMBEDDED_BOOTSTRAP])
    result = subprocess.run(
        ["ssh", *SSH_OPTIONS, parsed.host, command],
        input=json.dumps(payload).encode("utf-8"),
        timeout=parsed.timeout,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(dispatch_remote(sys.argv[1:]))
