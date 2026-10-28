"""Settings content preserves operational states and gives users real next steps."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


def render(name, **context):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    return env.get_template(name).render(
        email="sample@example.test",
        csrf_token="fixture-csrf",
        active_page="settings",
        asset_version="test",
        **context,
    )


def render_updates(state="normal"):
    release = (
        None
        if state in {"normal", "disabled", "error"}
        else {
            "version": "2026.10.28.1",
            "newer": state == "newer",
            "summary": "Sample release information.",
            "released_at": "2026-10-28T00:00:00Z",
            "notes_url": "https://jhinx.dev/updates/omakase-plus",
        }
    )
    return render(
        "updates.html",
        installed_version="2026.10.27.2",
        checks_enabled=state != "disabled",
        updates={
            "release": release,
            "error": "Could not check release information." if state == "error" else "",
            "checked_at": "2026-10-28T00:00:00Z" if state != "normal" else "",
        },
        retry_after=300 if state == "cooldown" else 0,
    )


@pytest.mark.parametrize(
    "state,copy",
    [
        ("normal", "No check has been made"),
        ("disabled", "Update checks are disabled"),
        ("error", "Could not check release information."),
        ("newer", "Update available"),
        ("current", "No newer release found"),
        ("cooldown", "Please wait 300 seconds"),
    ],
)
def test_update_states_keep_their_status_and_explicit_check_contract(state, copy):
    html = render_updates(state)
    assert copy in html
    assert "What happens when you check?" in html
    if state != "disabled":
        assert 'action="/plus/settings/updates/check"' in html
        assert 'name="csrf_token" value="fixture-csrf"' in html
        assert 'data-busy-label="Checking releases…"' in html
    else:
        assert 'action="/plus/settings/updates/check"' not in html


def test_app_only_setup_links_to_public_quickstart_and_names_catalog_dependency():
    html = render(
        "onboarding.html",
        onboarding_step=2,
        onboarding_setup_path="standalone",
        onboarding_setup_path_label="Explore with Omakase only",
        onboarding_intent="explore",
        onboarding_intent_label="Explore first",
    )
    assert 'href="https://omakase-plus.jhinx.dev/docs/quickstart"' in html
    assert "public anime catalog" in html
    assert "Movie and TV catalog search needs a configured Seerr connection" in html
    assert "docs/getting-started.md" not in html
    assert "compose.standalone.yaml" in html


def test_agent_examples_remain_exact_and_keyboard_reachable():
    html = render("agents.html", agent_user_id=42)
    assert (
        "OMAKASE_PLUS_PRIVATE=true omakase plus agent --data-dir /path/to/app-data --user-id 42 status"
        in html
    )
    assert (
        "docker exec -i omakase-local-app omakase plus agent --data-dir /home/omakase/data --user-id 42 status"
        in html
    )
    assert html.count('class="agent-command" tabindex="0"') == 2
    assert "--allow-write preferences" in html and "--allow-write monitors" in html
