"""Saved credential UI contracts use synthetic context; secrets are never rendered."""

import argparse
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2] / "src/omakase/plus"


def render_fixture(kind="connections", saved=True):
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    common = dict(
        email="sample@example.invalid",
        csrf_token="synthetic-credential-csrf",
        active_page="settings",
        asset_version="fixture",
        synthetic_review=True,
    )
    if kind == "connections":
        return env.get_template("connections.html").render(
            **common,
            movies_ready=False,
            connections=[
                dict(
                    id="realdebrid",
                    name="Real-Debrid",
                    purpose="Cloud downloads",
                    label="Connected" if saved else "Not connected",
                    key_saved=saved,
                    has_url=False,
                    url_saved=False,
                    health=dict(
                        state="connected" if saved else "not_configured",
                        detail="Synthetic saved state",
                        checked_at=None,
                        retry_at=None,
                        indexers=[],
                    ),
                )
            ],
        )
    return env.get_template("settings.html").render(
        **common,
        settings_section="ai",
        keys={"llm_api_key": "Synthetic recommendation key"},
        stored={"llm_api_key": "NEVER-RENDER-SYNTHETIC-KEY" if saved else ""},
    )


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


@pytest.mark.parametrize("kind", ["connections", "ai"])
def test_saved_key_is_only_a_boolean_state_with_empty_replacement(kind):
    html = render_fixture(kind, True)
    assert "Key saved securely" in html and "Change key" in html
    assert "NEVER-RENDER-SYNTHETIC-KEY" not in html
    assert "Leave blank to keep saved key" not in html
    assert "••••" not in html
    password = [field for field in Inputs(html).inputs if field.get("type") == "password"]
    assert len(password) == 1 and not password[0].get("value")
    assert password[0]["name"] == ("api_key" if kind == "connections" else "llm_api_key")
    assert 'type="button" class="button button-secondary" data-credential-cancel' in html
    assert 'name="csrf_token" value="synthetic-credential-csrf"' in html


@pytest.mark.parametrize("kind", ["connections", "ai"])
def test_unsaved_key_has_normal_empty_paste_field(kind):
    html = render_fixture(kind, False)
    assert "Paste your key" in html and "<summary>Change key</summary>" not in html
    fields = [field for field in Inputs(html).inputs if field.get("type") == "password"]
    assert len(fields) == 1 and not fields[0].get("value")
    if kind == "connections":
        assert "required" in fields[0]


def test_connection_actions_and_ai_forget_remain_separate_and_csrf_protected():
    html = render_fixture("connections", True)
    for action in ("save", "test", "disconnect"):
        assert f'action="/plus/settings/connections/realdebrid/{action}"' in html
    ai = render_fixture("ai", True)
    assert 'name="delete_llm_api_key" value="1" formnovalidate' in ai
    assert 'action="/plus/settings"' in ai and 'name="settings_section" value="ai"' in ai


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-to", required=True)
    args = parser.parse_args()
    out = Path(args.render_to)
    out.mkdir(parents=True, exist_ok=True)
    for kind in ("connections", "ai"):
        for saved in (True, False):
            (out / f"{kind}-{'saved' if saved else 'empty'}.html").write_text(
                render_fixture(kind, saved), encoding="utf-8"
            )


@pytest.mark.parametrize(
    "replacement,remove,expected",
    [
        ("replacement-value", True, "remove"),
        ("", True, "remove"),
        ("replacement-value", False, "replace"),
        ("", False, "unchanged"),
    ],
)
def test_explicit_remove_wins_over_an_in_progress_replacement(
    monkeypatch, replacement, remove, expected
):
    import asyncio
    from types import SimpleNamespace
    from urllib.parse import urlencode

    from starlette.requests import Request

    from omakase.plus import routes
    from omakase.plus.web_security import csrf_token

    cookie = b"omakase_session=synthetic-credential-session"
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"cookie", cookie), (b"content-type", b"application/x-www-form-urlencoded")],
    }
    form = {
        "settings_section": "ai",
        "csrf_token": csrf_token(Request(scope)),
        "llm_api_key": replacement,
    }
    if remove:
        form["delete_llm_api_key"] = "1"
    body = urlencode(form).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    operations = []
    monkeypatch.setattr(
        routes,
        "store_secret",
        lambda db, user, key, value: operations.append(("replace", user, key, value)),
    )
    monkeypatch.setattr(
        routes, "delete_secret", lambda db, user, key: operations.append(("remove", user, key))
    )
    response = asyncio.run(
        routes.settings_post(Request(scope, receive), db=object(), user=SimpleNamespace(id=7))
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("/plus/settings/ai?saved=")
    if expected == "unchanged":
        assert operations == []
    else:
        assert len(operations) == 1 and operations[0][:3] == (expected, 7, "llm_api_key")
    if remove:
        assert "removed" in response.headers["location"]
