"""Deterministically regenerate three compatibility modules from one accepted commit.

No Git checkout/reset, database access, Docker operation or activation occurs.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path

BASE = "c9d491ccbdfa68aedd31692028da5820fcd6638d"
FILES = ("watch/models.py", "product_views.py", "viewing_history.py")
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent
SITE_PACKAGES = "/usr/local/lib/python3.12/site-packages"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_exact(source, old, new, count=1):
    if source.count(old) != count:
        raise ValueError("Pinned source anchor mismatch")
    return source.replace(old, new)


def overlay(relative, original):
    source = original.decode("utf-8")
    if relative == "watch/models.py":
        source = replace_exact(
            source,
            'WatchSource = Literal["plex", "apple_tv", "anilist", "manual", "migration"]',
            'WatchSource = Literal["plex", "apple_tv", "anilist", "manual", "migration", "web_player"]',
        )
        source = replace_exact(
            source,
            '_WATCH_SOURCES = frozenset({"plex", "apple_tv", "anilist", "manual", "migration"})',
            '_WATCH_SOURCES = frozenset({"plex", "apple_tv", "anilist", "manual", "migration", "web_player"})',
        )
    elif relative == "product_views.py":
        source, count = re.subn(
            r'(?m)^([ \t]*)"plex": "Plex",$',
            r'\1"plex": "Plex",\n\1"web_player": "Web player",',
            source,
        )
        if count != 4:
            raise ValueError("Pinned label dictionaries changed")
    elif relative == "viewing_history.py":
        source = replace_exact(
            source,
            "e.source IN ('plex','apple_tv','anilist','manual')",
            "e.source IN ('plex','apple_tv','anilist','manual','web_player')",
        )
        source = replace_exact(
            source,
            'source = {"plex": "Plex", "apple_tv": "Apple TV", "anilist": "AniList", "manual": "Manual"}[\n        row["source"]\n    ]',
            'source = {\n        "plex": "Plex",\n        "apple_tv": "Apple TV",\n        "anilist": "AniList",\n        "manual": "Manual",\n        "web_player": "Web player",\n    }[row["source"]]',
        )
    else:
        raise ValueError("Unsupported recovery module")
    return source.encode("utf-8")


def generate(*, check=False, base=BASE):
    if base != BASE:
        raise ValueError("Recovery requires the exact accepted base commit")
    actual = (
        subprocess.check_output(["git", "rev-parse", "--verify", BASE + "^{commit}"], cwd=ROOT)
        .decode()
        .strip()
    )
    if actual != BASE:
        raise ValueError("Accepted base identity mismatch")
    outputs = {}
    records = []
    for relative in FILES:
        source_path = "src/omakase/plus/" + relative
        original = subprocess.check_output(["git", "show", BASE + ":" + source_path], cwd=ROOT)
        patched = overlay(relative, original)
        destination = "overlay/omakase/plus/" + relative
        patch_path = "patches/" + relative.replace("/", "-") + ".patch"
        patch = "".join(
            difflib.unified_diff(
                original.decode().splitlines(True),
                patched.decode().splitlines(True),
                fromfile=BASE + "/" + source_path,
                tofile=destination,
            )
        ).encode()
        outputs[destination] = patched
        outputs[patch_path] = patch
        records.append(
            {
                "source_path": source_path,
                "base_sha256": digest(original),
                "overlay_path": destination,
                "overlay_sha256": digest(patched),
                "overlay_bytes": len(patched),
                "patch_path": patch_path,
                "patch_sha256": digest(patch),
                "container_target": SITE_PACKAGES + "/omakase/plus/" + relative,
            }
        )
    manifest = {
        "schema": "omakase-player-recovery-v1",
        "accepted_commit": BASE,
        "python": "3.12",
        "mode": "binary-compatibility overlay; not an activation engine",
        "files": records,
        "effective_overlay_sha256": digest(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        ),
    }
    outputs["manifest.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    for relative, data in outputs.items():
        destination = PACKAGE / relative
        if check:
            if not destination.is_file() or destination.read_bytes() != data:
                raise ValueError("Recovery artifact differs: " + relative)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE, choices=[BASE])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(generate(check=args.check, base=args.base), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
