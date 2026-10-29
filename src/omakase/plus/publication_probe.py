"""Bounded local-only probe execution shared by typed local import and curator."""

from __future__ import annotations

import subprocess
import threading


def bounded_probe_runner(
    command,
    *,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="strict",
    timeout=30,
    check=False,
):
    """Cap ffprobe stdout and decode using the caller's supported UTF-8 policy."""
    if (
        capture_output is not True
        or text is not True
        or check is not False
        or encoding not in ("utf-8", "utf8", "utf_8")
        or errors not in ("strict", "replace")
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 120
    ):
        raise ValueError("Unsupported bounded probe decoding or execution options.")
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
    return subprocess.CompletedProcess(
        command, process.returncode, output.decode("utf-8", errors=errors), ""
    )


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
