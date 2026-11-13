"""Saved health evidence gets concrete safe navigation, never provider mutations."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from omakase.plus.db import run_migrations
from omakase.plus.health import affected_health_titles, health_guidance

ROOT = Path(__file__).resolve().parents[2] / "src/omakase/plus"


@pytest.mark.parametrize(
    "kind,code,section,href,cause",
    [
        (
            "animetosho",
            "CATALOG_UNAVAILABLE",
            "catalog",
            "/plus/settings/sources#animetosho",
            "Every search",
        ),
        ("nyaa", "PARTIAL_CATALOG_ERROR", "catalog", "/plus/settings/sources", "Some searches"),
        (
            "prowlarr",
            "CATALOG_SETUP_INCOMPLETE",
            "catalog",
            "/plus/settings/connections#connection-prowlarr",
            "required settings",
        ),
        (
            "realdebrid",
            "PROVIDER_AUTH_REQUIRED",
            "provider",
            "/plus/settings/connections#connection-realdebrid",
            "API access",
        ),
        ("local_import", "CAPACITY_LOW", "provider", "/plus/settings/storage", "free storage"),
        (
            "",
            "PLEX_SECTION_MISMATCH",
            "plex",
            "/plus/settings/connections#connection-plex",
            "mapping mismatch",
        ),
        ("qbittorrent", "VPN_REQUIRED", "provider", "/plus/settings/storage", "download tunnel"),
    ],
)
def test_warning_cause_and_relevant_existing_action(kind, code, section, href, cause):
    advice = health_guidance(dict(kind=kind, reason_code=code, status="error"), section)
    assert advice["href"] == href and cause in advice["cause"]
    assert advice["next_step"] and advice["label"]
    assert not any(word in advice["href"] for word in ("retry", "disable", "delete", "disconnect"))


def test_anime_source_failure_is_not_diagnosed_as_disconnected_key():
    advice = health_guidance(
        dict(kind="animetosho", reason_code="CATALOG_UNAVAILABLE", status="error")
    )
    assert "not a saved API-key connection" in advice["next_step"]
    assert "server administrator" in advice["next_step"]
    assert "temporary" not in advice["cause"].lower()  # Actual saved fact is all searches failed.
    assert "HTTP" not in advice["cause"]


def test_waiting_and_unknown_evidence_are_not_diagnosed_as_outages():
    waiting = health_guidance(
        dict(kind="nyaa", reason_code="CATALOG_NOT_CHECKED", status="unknown")
    )
    assert "No source search result" in waiting["cause"]
    assert "does not start" in waiting["next_step"]
    assert health_guidance(dict(reason_code="READY", status="healthy")) is None
    unknown = health_guidance(
        dict(reason_code="UNRECOGNIZED", kind="https://untrusted.invalid", status="unknown"),
        "provider",
    )
    assert unknown["href"] == "/plus/settings/storage"
    assert "does not prove" in unknown["next_step"]


def test_affected_titles_are_bounded_owner_scoped_and_exclude_raw_errors(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    run_migrations(db)
    for user in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"sample{user}@example.invalid", "synthetic"),
        )
    for index in range(23):
        db.execute(
            "INSERT INTO media_monitors(user_id,anilist_id,title,consecutive_failures,last_result) VALUES(1,?,?,1,?)",
            (1000 + index, f"Own title {index}", "token=NEVER-RENDER"),
        )
    db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,consecutive_failures) VALUES(2,9999,'FOREIGN-TITLE',1)"
    )
    db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,consecutive_failures) VALUES(1,9998,'Healthy title',0)"
    )
    before = db.total_changes
    result = affected_health_titles(db, user_id=1, limit=999)
    assert result["total"] == 23 and len(result["items"]) == 20
    assert all(row["href"].startswith("/plus/library/") for row in result["items"])
    assert (
        "FOREIGN" not in str(result)
        and "Healthy title" not in str(result)
        and "NEVER-RENDER" not in str(result)
    )
    assert db.total_changes == before
    other = affected_health_titles(db, user_id=2)
    assert other["total"] == 1 and other["items"][0]["title"] == "FOREIGN-TITLE"
    assert affected_health_titles(db, user_id=3) == {"total": 0, "items": []}
    db.close()


def render_fixture():
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())

    def item(label, kind, code):
        return dict(
            label=label,
            kind=kind,
            reason_code=code,
            reason_label="Source search unavailable"
            if code == "CATALOG_UNAVAILABLE"
            else "Connection needs attention",
            status="error",
            status_label="Needs attention",
            detail="Saved technical receipt",
            source="Saved check",
            checked_at="2026-10-28T10:00:00Z",
        )

    return env.get_template("health.html").render(
        email="sample@example.invalid",
        csrf_token="synthetic-health-csrf",
        active_page="health",
        asset_version="fixture",
        synthetic_review=True,
        overall="attention",
        revision="synthetic",
        monitor_failures=1,
        apple_tv_integration=None,
        apple_tv_watch_enabled=False,
        scheduler={"state": "idle"},
        catalogs=[
            item("AnimeTosho release metadata", "animetosho", "CATALOG_UNAVAILABLE"),
            item("Expanded search", "prowlarr", "CATALOG_SETUP_INCOMPLETE"),
        ],
        providers=[
            item("Real-Debrid", "realdebrid", "PROVIDER_AUTH_REQUIRED"),
            item("Local storage", "local_import", "CAPACITY_LOW"),
        ],
        plex_targets=[item("Plex library", "", "PLEX_SECTION_MISMATCH")],
        health_guidance=health_guidance,
        affected_titles={
            "total": 1,
            "items": [
                {
                    "title": "A very long saved title with a failed check and its own episode availability details",
                    "href": "/plus/library/42",
                    "active": True,
                    "checked_at": None,
                }
            ],
        },
    )


def test_actual_rendered_warning_has_cause_action_and_title_before_diagnostics():
    html = render_fixture()
    before = html.split('<details class="health-check-details">', 1)[0]
    assert "Every search against this source failed" in before
    assert "not a saved API-key connection" in before
    assert 'href="/plus/settings/sources#animetosho"' in before
    assert "CATALOG_UNAVAILABLE" not in before
    assert 'href="/plus/library/42"' in html
    assert "not attributed to a particular source" in html
    assert "1 monitored title needs attention." in html
    assert "CATALOG_UNAVAILABLE" in html.split('<details class="health-check-details">', 1)[1]


def test_animetosho_action_lands_on_rendered_operator_guidance_not_prowlarr_controls():
    advice = health_guidance(
        dict(kind="animetosho", reason_code="CATALOG_UNAVAILABLE", status="error")
    )
    assert advice["href"] == "/plus/settings/sources#animetosho"
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    html = env.get_template("_settings_sources.html").render(
        catalog_configuration={"state": "optional", "label": "Optional", "detail": "Synthetic"},
        catalog_keys={},
        catalog_stored={},
    )
    assert 'id="animetosho"' in html
    assert "Prowlarr filters above do not configure or disable AnimeTosho" in html
    assert "OMAKASE_ANIMETOSHO_ENABLED=true" in html
    assert "OMAKASE_ANIMETOSHO_ENABLED=false" in html
    assert "server administrator" in html and "does not apply that change" in html
    assert 'name="OMAKASE_ANIMETOSHO_ENABLED"' not in html


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-to", required=True)
    args = parser.parse_args()
    Path(args.render_to).write_text(render_fixture(), encoding="utf-8")
