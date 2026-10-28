"""Render real tracker templates with synthetic local data; no app DB/providers."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def render(root, output):
    env = Environment(
        loader=FileSystemLoader(root / "src/omakase/plus/templates"), autoescape=select_autoescape()
    )
    context = {
        "email": "member@example.test",
        "asset_version": "synthetic-tracker",
        "active_page": "settings",
        "csrf_token": "synthetic-csrf",
        "tracker_status": {
            "connections": {
                p: {"state": "disconnected", "name": None} for p in ["anilist", "myanimelist"]
            },
            "enabled": False,
            "apply_enabled": True,
            "pending": 0,
        },
        "mal_oauth_ready": False,
        "tracker_plan": None,
    }
    states = {"setup": copy.deepcopy(context)}
    context["mal_oauth_ready"] = True
    for p in context["tracker_status"]["connections"]:
        context["tracker_status"]["connections"][p] = {
            "state": "verified",
            "name": "Synthetic member with a long display name",
        }
    states["connected"] = copy.deepcopy(context)
    context["tracker_reading"] = {
        "scans": {
            "anilist": {"read": 100, "complete": False},
            "myanimelist": {"read": 200, "complete": True},
        }
    }
    states["reading"] = copy.deepcopy(context)
    del context["tracker_reading"]
    mapping = {"anilist_id": 11, "mal_id": 22, "observed_id_mal": 22, "media_kind": "ANIME"}
    context["tracker_plan"] = {
        "id": "a" * 32,
        "hash": "b" * 64,
        "state": "preview",
        "expires_at": 99999,
        "counts": {"proposals": 2, "conflicts": 2, "unmapped": 1},
        "items": [
            {
                "key": "0",
                "title": "Synthetic anime with a deliberately long title for phone wrapping and complete request review",
                "unsupported": [],
                "patch": {
                    "mapping": mapping,
                    "target": "myanimelist",
                    "changes": [["progress", 3], ["score", 70]],
                },
            },
            {
                "key": "1",
                "title": "Synthetic separate date preference",
                "unsupported": ["started_at"],
                "patch": {
                    "mapping": mapping,
                    "target": "myanimelist",
                    "changes": [["started_at", "2020"]],
                },
            },
        ],
        "conflicts": [
            {
                "mapping": mapping,
                "field": "score",
                "reason": "lossy_score",
                "anilist": 75,
                "mal": 80,
            },
            {
                "mapping": mapping,
                "field": "entry",
                "reason": "entry_absent",
                "anilist": None,
                "mal": None,
            },
        ],
        "unmapped": [["anilist", 99]],
    }
    states["review"] = copy.deepcopy(context)
    context["tracker_notice"] = (
        "Confirmed: 1. Needs review: 0. Waiting: 0. Uncertain: 1. No uncertain action is automatically replayed."
    )
    states["applied"] = copy.deepcopy(context)
    context["tracker_error"] = "The tracker asked us to wait. Continue after the indicated delay."
    context["tracker_status"].update(
        opted_in=True, enabled=False, apply_enabled=False, worker_active=False
    )
    states["error"] = copy.deepcopy(context)
    output.mkdir(parents=True, exist_ok=True)
    for name, data in states.items():
        (output / (name + ".html")).write_text(
            env.get_template("tracker_bridge.html").render(**data), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[2])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.root.resolve(), args.output.resolve())
