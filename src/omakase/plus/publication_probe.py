"""Bounded local-only probe execution shared by typed local import and curator."""

from __future__ import annotations

import subprocess
import threading


def bounded_probe_runner(command, *, capture_output=True, text=True, timeout=30, check=False):
    """Cap stdout while the process runs; never buffer unbounded ffprobe output."""
    output = bytearray()
    exceeded = threading.Event()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )

    def read():
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                return
            if len(output) + len(chunk) > 256 * 1024:
                exceeded.set()
                process.kill()
                return
            output.extend(chunk)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join(timeout=2)
        process.stdout.close()
    if exceeded.is_set() or reader.is_alive():
        raise ValueError("Exact-file probe output exceeded its size or read bound.")
    return subprocess.CompletedProcess(command, process.returncode, output.decode("utf-8"), "")


def local_only_probe_runner(command, **kwargs):
    guarded = [
        command[0],
        "-protocol_whitelist",
        "file",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "5000000",
        *command[1:],
    ]
    return bounded_probe_runner(guarded, **kwargs)
