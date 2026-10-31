"""Finite, network-free synthetic watchdog/death/restart qualification.

Only an operator runs the Linux native gate. No original media or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import time
from pathlib import Path

SEED_BYTES = 626512
SENTINEL = b"unknown synthetic lifecycle sentinel: preserve\n"


class LifecycleFailure(ValueError):
    def __init__(self, invariant):
        self.invariant = invariant
        super().__init__("lifecycle qualification invariant")


def require(ok, invariant="unspecified_invariant"):
    if not ok:
        raise LifecycleFailure(invariant)


def failure_details(error):
    trace = []
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        trace.append(
            {"file": Path(code.co_filename).name, "function": code.co_name, "line": tb.tb_lineno}
        )
        tb = tb.tb_next
    result = {"failure_type": type(error).__name__, "sanitized_traceback": trace[-8:]}
    if isinstance(error, LifecycleFailure):
        result["invariant"] = error.invariant
    if hasattr(error, "child_diagnostic"):
        result["child_diagnostic"] = error.child_diagnostic
    return result


def selected_cases(selection):
    require(selection in {"all", "parentdeath"}, "invalid_case_selection")
    return ("deadline", "parentdeath") if selection == "all" else ("parentdeath",)


def processes():
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        result.append(
            {
                "pid": int(path.name),
                "ppid": int(fields[1]),
                "pgrp": int(fields[2]),
                "state": fields[0],
                "start_ticks": fields[19],
            }
        )
        require(len(result) <= 256)
    return result


def identity(pid):
    from omakase.plus import web_playback_remux as remux

    return remux._identity(pid)


def signal_owned(pid, expected, sig):
    require(type(pid) is int and pid > 1)
    require(identity(pid) == tuple(expected))
    os.kill(pid, sig)  # Only the exact child coordinator; never a discovered group.


def group_rows(workers, rows=None):
    groups = {worker["pid"] for worker in workers}
    return [
        row
        for row in (processes() if rows is None else rows)
        if row["pid"] in groups or row["pgrp"] in groups
    ]


def require_live_media(workers, rows):
    by_pid = {row["pid"]: row for row in rows}
    for worker in workers:
        row = by_pid.get(worker["pid"])
        require(row is not None and row["state"] not in {"Z", "X", "x"})
        require(row["start_ticks"] == worker["identity"][0])
        require(identity(worker["pid"]) == tuple(worker["identity"]))


def require_deadline_result(finished):
    require(
        finished.get("error_type") == "RemuxError" and finished.get("code") == "deadline_exceeded"
    )
    media = [
        row for row in finished.get("child_returncodes", []) if row["mode"] in {"remux", "observe"}
    ]
    require(
        len(media) == 2
        and {row["mode"] for row in media} == {"remux", "observe"}
        and all(row["returncode"] == -9 for row in media)
    )  # Linux SIGKILL.


def require_parentdeath_window(deadline, killed, absent_at=None):
    require(deadline - killed >= 1.0, "parentdeath_insufficient_deadline_margin")
    if absent_at is not None:
        require(absent_at < deadline, "parentdeath_not_absent_before_watchdog")


async def absent(workers, *, seconds=0.5, read_rows=processes):
    until = time.monotonic() + seconds
    while True:
        if not group_rows(workers, read_rows()):
            return
        require(time.monotonic() < until)
        await asyncio.sleep(0.02)


async def observed_exits(workers, *, seconds=1, read_rows=processes):
    observed = {}
    until = time.monotonic() + seconds
    while len(observed) < len(workers):
        rows = {row["pid"]: row for row in read_rows()}
        for worker in workers:
            row = rows.get(worker["pid"])
            if row is None or row["state"] == "Z":
                observed.setdefault(str(worker["pid"]), time.monotonic())
        require(time.monotonic() < until)
        await asyncio.sleep(0.02)
    return observed


def measurement():
    import resource

    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "wall": time.monotonic(),
        "parent_cpu": time.process_time(),
        "children_user": child.ru_utime,
        "children_system": child.ru_stime,
        "cgroup_cpu": int(
            dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())[
                "usage_usec"
            ]
        )
        / 1_000_000,
    }


def delta(before):
    return {key: value - before[key] for key, value in measurement().items()}


def database(path, *, initialize=False):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if initialize:
        from omakase.plus import web_playback_remux as remux

        migration = Path(remux.__file__).with_name("migrations") / "040-web-playback-remux.sql"
        db.executescript(migration.read_text())
    return db


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


async def coordinate(args):
    import httpx

    from omakase.plus import web_playback_remux as remux
    from omakase.plus.web_playback_source import PlexStream, ResolvedAnimeSource

    media, cache, work = Path(args.media), Path(args.cache), Path(args.work)
    require(media.is_file() and not media.is_symlink() and media.stat().st_size == SEED_BYTES)
    db_case = "parentdeath" if args.case == "admission" else args.case
    db = database(work / (db_case + ".db"))
    workers, processes_owned = [], []
    counts = {"prefix_requests": 0, "prefix_bytes": 0, "full_requests": 0, "full_bytes": 0}
    version = hashlib.sha256(("synthetic-lifecycle-" + args.case).encode()).hexdigest()
    source = ResolvedAnimeSource(
        1,
        "synthetic-lifecycle",
        1,
        1,
        1,
        "synthetic-server",
        "synthetic-account",
        "1",
        "/library/parts/1/1/seed.mkv",
        SEED_BYTES,
        10000,
        "mkv",
        "h264",
        "opus",
        (
            PlexStream("100", "video", "h264", "und", False, False, True, 0),
            PlexStream("101", "audio", "opus", "jpn", False, False, True, 1),
            PlexStream("102", "audio", "opus", "eng", False, False, False, 2),
        ),
        version,
        "synthetic-source",
        "https://lifecycle.invalid",
        "synthetic-no-provider-token",
        (),
        None,
    )
    announced = False
    prefix_opened = asyncio.Event()
    started = time.monotonic()

    async def launch(folder, recipe):
        process = await remux._launch(folder, recipe)
        processes_owned.append(process)
        workers.append(
            {"pid": process.pid, "identity": identity(process.pid), "mode": recipe["mode"]}
        )
        return process

    def announce():
        nonlocal announced
        if announced:
            return
        announced = True
        row = db.execute(
            "SELECT job_id,deadline,state FROM web_playback_remux WHERE state='RUNNING'"
        ).fetchone()
        require(row is not None)
        emit(
            {
                "event": "stalled",
                "case": args.case,
                "coordinator_pid": os.getpid(),
                "coordinator_identity": identity(os.getpid()),
                "workers": workers,
                "job_id": row["job_id"],
                "deadline": row["deadline"],
                "media_version": version,
                "monotonic": time.monotonic(),
                "started_monotonic": started,
                "counts": counts,
            }
        )

    class Stream(httpx.AsyncByteStream):
        def __init__(self, prefix):
            self.prefix = prefix

        async def __aiter__(self):
            count = 0
            with media.open("rb") as handle:
                while block := handle.read(65536):
                    count += len(block)
                    counts["prefix_bytes" if self.prefix else "full_bytes"] += len(block)
                    if self.prefix and args.case == "admission":
                        prefix_opened.set()
                    yield block
                    if self.prefix and args.case == "admission":
                        await asyncio.Event().wait()
                    if not self.prefix:
                        announce()
                        await asyncio.Event().wait()
                require(count == SEED_BYTES)

    def upstream(request):
        require(
            request.method == "GET"
            and request.url.host == "lifecycle.invalid"
            and request.url.path == source.part_key
            and request.headers.get("x-plex-token") == source.token
            and request.headers.get("accept-encoding") == "identity"
        )
        prefix = "range" in request.headers
        headers = {"content-length": str(SEED_BYTES), "content-encoding": "identity"}
        if prefix:
            require(request.headers["range"] == f"bytes=0-{SEED_BYTES - 1}")
            counts["prefix_requests"] += 1
            headers["content-range"] = f"bytes 0-{SEED_BYTES - 1}/{SEED_BYTES}"
        else:
            counts["full_requests"] += 1
        return httpx.Response(206 if prefix else 200, headers=headers, stream=Stream(prefix))

    task = asyncio.create_task(
        remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=cache,
            transport=httpx.MockTransport(upstream),
            launcher=launch,
            deadline_seconds=2,
        )
    )
    try:
        if args.case == "admission":
            # Prove a new prefix is admitted after reconciliation, then cancel;
            # no second complete COPY or READY artifact is needed.
            await asyncio.wait_for(prefix_opened.wait(), 1.5)
            task.cancel()
        await task
        emit({"event": "unexpected_ready"})
    except BaseException as error:
        require(not isinstance(error, (KeyboardInterrupt, SystemExit)))
        await absent(workers)
        remaining = db.execute("SELECT state FROM web_playback_remux").fetchall()
        require(not remaining and not remux.held_media_versions(db))
        emit(
            {
                "event": "finished",
                "case": args.case,
                "error_type": type(error).__name__,
                "code": error.code if isinstance(error, remux.RemuxError) else "cancelled",
                "workers": workers,
                "all_process_groups_absent": True,
                "child_returncodes": [
                    {"pid": worker["pid"], "mode": worker["mode"], "returncode": process.returncode}
                    for worker, process in zip(workers, processes_owned)
                ],
                "counts": counts,
                "no_ready_or_running": True,
                "elapsed": time.monotonic() - started,
            }
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        db.close()


async def read_event(child, expected):
    raw = await asyncio.wait_for(child.stdout.readline(), 5)
    require(raw.endswith(b"\n") and len(raw) <= 16384)
    value = json.loads(raw)
    if value.get("event") != expected:
        error = LifecycleFailure("unexpected_child_event")
        error.child_diagnostic = {
            key: value[key]
            for key in ("failure_type", "invariant", "failure_phase", "sanitized_traceback")
            if key in value
        }
        raise error
    return value


async def spawn(args, case):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--coordinator",
        "--case",
        case,
        "--work",
        args.work,
        "--cache",
        args.cache,
        "--media",
        args.media,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        start_new_session=True,
        close_fds=True,
    )
    expected = identity(child.pid)
    require(expected is not None)
    return child, expected


def sentinel_valid(cache):
    path = cache / "unknown-preserve-sentinel" / "keep.txt"
    return (
        not path.parent.is_symlink()
        and path.is_file()
        and not path.is_symlink()
        and path.read_bytes() == SENTINEL
    )


def lease_state(db):
    return {
        "rows": db.execute("SELECT COUNT(*) FROM web_playback_remux").fetchone()[0],
        "ready": db.execute(
            "SELECT COUNT(*) FROM web_playback_remux WHERE state='READY'"
        ).fetchone()[0],
        "running": db.execute(
            "SELECT COUNT(*) FROM web_playback_remux WHERE state='RUNNING'"
        ).fetchone()[0],
        "reserved_bytes": db.execute(
            "SELECT coalesce(sum(reserved_bytes),0) FROM web_playback_remux WHERE state IN ('RUNNING','READY')"
        ).fetchone()[0],
    }


def reconciliation_diagnostic(db, cache, event, workers):
    """Read only fixed synthetic metadata; no SQL bodies, tokens or paths emitted."""
    from omakase.plus import web_playback_remux as remux

    rows = db.execute(
        "SELECT state,phase,deadline,pid,coordinator_pid FROM web_playback_remux LIMIT 4"
    ).fetchall()
    result = {
        "wall": time.time(),
        "monotonic": time.monotonic(),
        "lease": lease_state(db),
        "records": [dict(row) for row in rows],
        "held_version": version_held(db, event["media_version"]),
        "owned_process_states": group_rows(workers),
        "unknown_sentinel_preserved": sentinel_valid(cache),
    }
    require(re.fullmatch("[0-9a-f]{32}", event["job_id"]) is not None, "diagnostic_invalid_job")
    folder = cache / event["job_id"]
    result["owned_folder_exists"] = folder.exists()
    if folder.is_dir() and not folder.is_symlink():
        marker = folder / ".omakase-remux"
        result["owned_marker_valid"] = (
            marker.is_file() and not marker.is_symlink() and marker.read_text() == event["job_id"]
        )
        entries = list(folder.iterdir())
        require(len(entries) <= 4097, "diagnostic_file_bound")
        result["owned_entries"] = len(entries)
        result["owned_bytes"] = sum(
            path.stat().st_size for path in entries if path.is_file() and not path.is_symlink()
        )
        result["actual_cwd_scan_absent"] = remux._cwd_scan_absent(folder)
    return result


async def run(args):
    from omakase.plus import web_playback_remux as remux

    work, cache = Path(args.work), Path(args.cache)
    require(work.is_dir() and not work.is_symlink() and cache.is_dir() and not cache.is_symlink())
    require(not list(work.iterdir()) and not list(cache.iterdir()))
    sentinel = cache / "unknown-preserve-sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_bytes(SENTINEL)
    report = {
        "schema": "omakase-player-lifecycle-v1",
        "status": "failed",
        "provider_requests": 0,
        "original_media_reads": 0,
    }
    cases = selected_cases(args.cases)
    report["selected_cases"] = list(cases) + ["admission"]
    phase, trace = "initialize", []
    event, workers = None, []

    def checkpoint(name, diagnostic=None):
        nonlocal phase
        phase = name
        require(len(trace) < 64, "diagnostic_trace_bound")
        trace.append(
            {
                "phase": name,
                "monotonic": time.monotonic(),
                **({"diagnostic": diagnostic} if diagnostic is not None else {}),
            }
        )
        raw = json.dumps(trace, separators=(",", ":"))
        require(len(raw.encode()) <= 256 * 1024, "diagnostic_trace_byte_bound")
        temp = work / "lifecycle-trace.tmp"
        temp.write_text(raw)
        temp.replace(work / "lifecycle-trace.json")

    owned = []
    for case in cases:
        path = work / (case + ".db")
        db = database(path, initialize=True)
        db.close()
    try:
        async with asyncio.timeout(28):
            for case in cases:
                checkpoint(case + ":spawn")
                before = measurement()
                child, expected = await spawn(args, case)
                owned.append((child, expected))
                checkpoint(case + ":wait_stalled")
                event = await read_event(child, "stalled")
                checkpoint(case + ":stalled_event")
                require(
                    event["coordinator_pid"] == child.pid
                    and tuple(event["coordinator_identity"]) == expected
                )
                workers = event["workers"]
                media_workers = [
                    worker for worker in workers if worker["mode"] in {"remux", "observe"}
                ]
                require(len(media_workers) == 2 and event["counts"]["full_bytes"] == 65536)
                require(
                    all(
                        identity(worker["pid"]) == tuple(worker["identity"])
                        for worker in media_workers
                    )
                )
                # Both worker/watchdog groups must exist before lifecycle signal.
                checkpoint(case + ":wait_watchdogs")
                until = time.monotonic() + 0.3
                while True:
                    rows = processes()
                    if all(
                        sum(row["pgrp"] == worker["pid"] for row in rows) >= 2
                        for worker in media_workers
                    ):
                        break
                    require(time.monotonic() < until)
                    await asyncio.sleep(0.01)
                (work / (case + "-ready.json")).write_text(json.dumps(event))
                if case == "deadline":
                    require_live_media(media_workers, processes())
                    stopped_at = time.monotonic()
                    signal_owned(child.pid, expected, signal.SIGSTOP)
                    first_exit = {}
                    until = time.monotonic() + 3
                    while len(first_exit) < 2:
                        rows = {row["pid"]: row for row in processes()}
                        for worker in media_workers:
                            row = rows.get(worker["pid"])
                            if row is None or row["state"] == "Z":
                                first_exit.setdefault(str(worker["pid"]), time.monotonic())
                        require(time.monotonic() < until)
                        await asyncio.sleep(0.02)
                    require(
                        all(
                            1.5 <= stamp - event["started_monotonic"] <= 3
                            for stamp in first_exit.values()
                        )
                    )
                    require(
                        next(row["state"] for row in processes() if row["pid"] == child.pid) == "T"
                    )
                    signal_owned(child.pid, expected, signal.SIGCONT)
                    finished = await read_event(child, "finished")
                    require_deadline_result(finished)
                    require(await asyncio.wait_for(child.wait(), 2) == 0)
                    await absent(workers)
                    report[case] = delta(before) | {
                        "workers": workers,
                        "first_exit_monotonic": first_exit,
                        "worker_deadline_origin_monotonic": event["started_monotonic"],
                        "coordinator_stopped_monotonic": stopped_at,
                        "all_process_groups_absent_monotonic": time.monotonic(),
                        "coordinator_continued_after_worker_exit": True,
                        "finished": finished,
                    }
                else:
                    db = database(work / "parentdeath.db")
                    try:
                        checkpoint(
                            "parentdeath:before_kill",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                    finally:
                        db.close()
                    require_live_media(media_workers, processes())
                    killed_wall = time.time()
                    checkpoint(
                        "parentdeath:kill_margin",
                        {"deadline_margin_seconds": event["deadline"] - killed_wall},
                    )
                    require_parentdeath_window(event["deadline"], killed_wall)
                    killed_at = time.monotonic()
                    signal_owned(child.pid, expected, signal.SIGKILL)
                    require(await asyncio.wait_for(child.wait(), 2) == -signal.SIGKILL)
                    checkpoint("parentdeath:coordinator_reaped")
                    first_exit = await observed_exits(media_workers)
                    checkpoint("parentdeath:media_exits_observed")
                    await absent(workers)
                    absent_at = time.monotonic()
                    absent_wall = time.time()
                    checkpoint(
                        "parentdeath:all_groups_absent",
                        {"deadline_margin_seconds": event["deadline"] - absent_wall},
                    )
                    require_parentdeath_window(event["deadline"], killed_wall, absent_wall)
                    db = database(work / "parentdeath.db")
                    try:
                        checkpoint(
                            "parentdeath:before_early_lease_assert",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        require(
                            lease_state(db)["running"] == 1
                            and version_held(db, event["media_version"]),
                            "parentdeath_missing_running_lease_or_hold",
                        )
                        checkpoint(
                            "parentdeath:before_early_cleanup",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        remux.cleanup(db, cache)
                        checkpoint(
                            "parentdeath:after_early_cleanup",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        require(
                            lease_state(db)["running"] == 1,
                            "parentdeath_early_cleanup_released_lease",
                        )
                        checkpoint("parentdeath:wait_real_grace")
                        while time.time() <= event["deadline"] + 5:
                            await asyncio.sleep(
                                min(0.05, max(0.001, event["deadline"] + 5.01 - time.time()))
                            )
                        # A diagnostic hold makes ERROR visible before the normal
                        # unheld cleanup removes the known partial. It grants no reads.
                        checkpoint(
                            "parentdeath:before_reconcile_cleanup",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        remux.cleanup(db, cache, held_versions=lambda: {event["media_version"]})
                        checkpoint(
                            "parentdeath:after_reconcile_cleanup",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        state = lease_state(db)
                        require(
                            state["running"] == state["ready"] == state["reserved_bytes"] == 0,
                            "parentdeath_reconcile_retained_active_reservation",
                        )
                        require(
                            db.execute("SELECT state FROM web_playback_remux").fetchone()[0]
                            == "ERROR",
                            "parentdeath_reconcile_not_error",
                        )
                        checkpoint(
                            "parentdeath:before_partial_cleanup",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        remux.cleanup(db, cache)
                        checkpoint(
                            "parentdeath:after_partial_cleanup",
                            reconciliation_diagnostic(db, cache, event, workers),
                        )
                        require(
                            lease_state(db)["rows"] == 0 and not (cache / event["job_id"]).exists(),
                            "parentdeath_known_partial_not_removed",
                        )
                    finally:
                        db.close()
                    report[case] = delta(before) | {
                        "workers": workers,
                        "first_exit_monotonic": first_exit,
                        "coordinator_killed_monotonic": killed_at,
                        "deadline_margin_at_kill_seconds": event["deadline"] - killed_wall,
                        "all_groups_absent_before_watchdog_deadline_seconds": event["deadline"]
                        - absent_wall,
                        "all_process_groups_absent_monotonic": absent_at,
                        "all_process_groups_absent": True,
                        "real_deadline_and_grace_elapsed": True,
                        "observed_error": True,
                        "lease_and_reservation_released": True,
                        "known_partial_removed": True,
                    }
                require(sentinel_valid(cache))
            checkpoint("admission:spawn")
            before = measurement()
            child, expected = await spawn(args, "admission")
            owned.append((child, expected))
            checkpoint("admission:wait_finished")
            event = await read_event(child, "finished")
            require(await asyncio.wait_for(child.wait(), 2) == 0)
            require(
                event["counts"]["prefix_requests"] == 1 and event["counts"]["full_requests"] == 0
            )
            await absent(event["workers"])
            require(sentinel_valid(cache))
            report["restored_admission"] = delta(before) | {
                "prefix_requests": 1,
                "full_requests": 0,
                "all_process_groups_absent": True,
                "no_ready_or_running": True,
            }
            require(list(cache.iterdir()) == [sentinel])
            report.update(
                status="pass",
                unknown_sentinel_preserved=True,
                sentinel_sha256=hashlib.sha256(SENTINEL).hexdigest(),
            )
            checkpoint("complete")
    except Exception as error:
        report.update(failure_phase=phase, **failure_details(error))
        if event is not None and "parentdeath" in cases:
            db = database(work / "parentdeath.db")
            try:
                checkpoint(
                    "failure:preserved_state", reconciliation_diagnostic(db, cache, event, workers)
                )
            except Exception as diagnostic_error:
                report["diagnostic_failure"] = failure_details(diagnostic_error)
            finally:
                db.close()
    finally:
        for child, expected in owned:
            if child.returncode is None and identity(child.pid) == expected:
                signal_owned(child.pid, expected, signal.SIGCONT)
                signal_owned(child.pid, expected, signal.SIGKILL)
                await asyncio.wait_for(child.wait(), 2)
    report["diagnostic_trace"] = trace
    return report


def version_held(db, version):
    from omakase.plus import web_playback_remux as remux

    return version in remux.held_media_versions(db)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator", action="store_true")
    parser.add_argument("--case", choices=("deadline", "parentdeath", "admission"))
    parser.add_argument("--cases", choices=("all", "parentdeath"), default="all")
    parser.add_argument("--work", required=True)
    parser.add_argument("--media", required=True)
    parser.add_argument("--cache", default="/run/omakase-playback")
    args = parser.parse_args()
    require(sys.platform == "linux" and os.getpid() != 1)
    if Path("/probe/src").is_dir():
        sys.path.insert(0, "/probe/src")

    def timeout(*_):
        raise TimeoutError("finite synthetic lifecycle deadline")

    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        if args.coordinator:
            require(args.case is not None)
            asyncio.run(coordinate(args))
        else:
            report = asyncio.run(run(args))
            report["helper_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            emit(report)
    except Exception as error:
        emit({"status": "failed", **failure_details(error)})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    main()
