"""Standalone Linux limits/watchdog and strict source-free argument construction."""

import ctypes
import json
import os
import re
import signal
import sys
import time


def build_args(recipe):
    """Validate a small trusted recipe again across the exec boundary."""
    if not isinstance(recipe, dict) or recipe.get("mode") not in {"probe", "remux", "observe"}:
        raise ValueError("invalid recipe")
    seconds = recipe.get("seconds", 45)
    if type(seconds) not in {int, float} or not 0 < seconds <= 45:
        raise ValueError("invalid deadline")
    if recipe["mode"] == "probe":
        if set(recipe) - {"mode", "seconds"}:
            raise ValueError("invalid recipe")
        return [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-f",
            "matroska",
            "-protocol_whitelist",
            "pipe,file",
            "-read_intervals",
            "%+#1",
            "-show_entries",
            "stream=index,codec_type,codec_name,channels,width,height,time_base:format=start_time:stream_tags=language,mimetype:stream_disposition=default,forced",
            "-of",
            "json",
            "-i",
            "pipe:0",
        ]
    if recipe["mode"] == "observe":
        if set(recipe) - {"mode", "seconds"}:
            raise ValueError("invalid observer recipe")
        return [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-f",
            "matroska",
            "-protocol_whitelist",
            "pipe,file",
            "-select_streams",
            "a",
            "-show_packets",
            "-show_entries",
            "packet=stream_index,pts,duration:packet_side_data=",
            "-of",
            "compact=p=0:nk=0",
            "-i",
            "pipe:0",
        ]
    if set(recipe) - {
        "mode",
        "seconds",
        "video",
        "audio",
        "subtitles",
        "fonts",
        "input_start_time",
    }:
        raise ValueError("invalid recipe")
    if (
        type(recipe.get("input_start_time", 0)) not in {int, float}
        or not -0.25 <= recipe.get("input_start_time", 0) <= 0.25
    ):
        raise ValueError("invalid timestamp basis")
    tracks = [recipe.get("video")] + recipe.get("audio", []) + recipe.get("subtitles", [])
    if not 1 <= len(recipe.get("audio", [])) <= 8 or len(recipe.get("subtitles", [])) > 16:
        raise ValueError("invalid inventory")
    seen = set()
    for track in tracks:
        if not isinstance(track, dict) or set(track) - {"time_base"} != {
            "index",
            "id",
            "language",
            "default",
            "forced",
        }:
            raise ValueError("invalid track")
        if "time_base" in track and not re.fullmatch(
            r"[1-9][0-9]{0,8}/[1-9][0-9]{0,8}", track["time_base"]
        ):
            raise ValueError("invalid timebase")
        index = track["index"]
        if type(index) is not int or not 0 <= index <= 127 or index in seen:
            raise ValueError("invalid index")
        seen.add(index)
        if not re.fullmatch("[0-9]{1,20}", track["id"]):
            raise ValueError("invalid id")
        if not re.fullmatch("[a-z]{2,3}", track["language"]):
            raise ValueError("invalid language")
        if type(track["default"]) is not bool or type(track["forced"]) is not bool:
            raise ValueError("invalid role")
    fonts = recipe.get("fonts", [])
    if (
        len(fonts) > 16
        or any(type(i) is not int or not 0 <= i <= 127 or i in seen for i in fonts)
        or len(set(fonts)) != len(fonts)
    ):
        raise ValueError("invalid font inventory")
    args = ["/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for index in fonts:
        args += [f"-dump_attachment:{index}", f"font-{index}.bin"]
    args += [
        "-copyts",
        "-start_at_zero",
        "-f",
        "matroska",
        "-protocol_whitelist",
        "pipe,file",
        "-i",
        "pipe:0",
    ]
    for track in [recipe["video"]] + recipe["audio"]:
        args += ["-map", f"0:{track['index']}"]
    args += ["-c", "copy", "-map_metadata", "-1"]
    for index, track in enumerate([recipe["video"]] + recipe["audio"]):
        flags = "+".join(name for name in ("default", "forced") if track[name]) or "0"
        args += [
            f"-metadata:s:{index}",
            f"language={track['language']}",
            f"-disposition:{index}",
            flags,
        ]
    args += [
        "-f",
        "dash",
        "-dash_segment_type",
        "mp4",
        "-seg_duration",
        "4",
        "-use_timeline",
        "1",
        "-use_template",
        "1",
        "-window_size",
        "0",
        "-streaming",
        "0",
        "-init_seg_name",
        "init-$RepresentationID$.mp4",
        "-media_seg_name",
        "chunk-$RepresentationID$-$Number%05d$.m4s",
        "manifest.mpd",
    ]
    for track in recipe["subtitles"]:
        args += [
            "-map",
            f"0:{track['index']}",
            "-c:s",
            "copy",
            "-map_metadata",
            "-1",
            "-f",
            "ass",
            f"subtitle-{track['id']}.ass",
        ]
    return args


def main():
    if sys.platform != "linux" or len(sys.argv) != 3 or len(sys.argv[2]) > 16384:
        raise SystemExit(126)
    import resource

    expected = int(sys.argv[1])
    recipe = json.loads(sys.argv[2])
    args = build_args(recipe)
    if os.getppid() != expected:
        raise SystemExit(126)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected:
        raise SystemExit(126)
    for key, value in (
        (resource.RLIMIT_AS, 512 * 1024**2),
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_FSIZE, 768 * 1024**2),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(key, (value, value))
    worker = os.getpid()
    if os.fork() == 0:
        for fd in (0, 1, 2):
            os.close(fd)
        deadline = time.monotonic() + recipe.get("seconds", 45)
        while time.monotonic() < deadline:
            if os.getppid() != worker:
                os._exit(0)
            time.sleep(0.05)
        os.killpg(worker, signal.SIGKILL)
        os._exit(0)
    os.execve(args[0], args, {"PATH": "/usr/bin:/bin", "LANG": "C"})


if __name__ == "__main__":
    main()
