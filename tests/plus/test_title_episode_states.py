"""Render actual title markup with independent viewing and media evidence."""

from html.parser import HTMLParser
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader


class EpisodeRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "li" and "data-episode" in attrs:
            self.current = int(attrs["data-episode"])
            self.rows[self.current] = {"classes": attrs["class"].split(), "text": ""}

    def handle_data(self, data):
        if self.current:
            self.rows[self.current]["text"] += data

    def handle_endtag(self, tag):
        if tag == "li":
            self.current = None


def title_fixture():
    return {
        "id": 1,
        "anilist_id": 21,
        "title": "Synthetic title",
        "watch_status": "watching",
        "progress_episodes": 2,
        "required": 6,
        "usable": 3,
        "plex_visible": 3,
        "episode_total": 7,
        "display_episode_count": 7,
        "season_number": 1,
        "release_status_label": "Airing",
        "lifecycle": "watching",
        "lifecycle_label": "Watching",
        "description": "",
        "genres": [],
        "removal": None,
        "publication_pending": False,
        "next_episode": 3,
        "usable_episodes": [1, 3, 4],
        "upcoming_episodes": [7],
        "next_release_at": "2026-11-01T19:30:00Z",
        "last_source_label": "Manual",
        "state": "partial",
        "status_label": "Some episodes ready",
        "last_result": "Coverage 3/6; partial",
        "timeline": [
            {
                "created_at": "2026-10-27T19:30:00Z",
                "outcome": "partial",
                "detail": "Three episodes verified",
            }
        ],
        "current_assets": [
            {
                "id": 2,
                "provider_label": "Fixture provider",
                "quality": "1080p",
                "status_label": "Downloaded",
                "progress": 100,
                "usable_coverage": [1, 3, 4],
                "source": "Original-release-" + "long-name-" * 24,
                "subtitle_state": "automatic",
                "subtitle_label": "Verified automatically",
                "subtitle_detail": "Subtitle and publication checks completed.",
                "subtitles": [],
                "last_error_label": "",
                "last_error": "",
                "storage_backend": "local",
                "probe": None,
            }
        ],
        "other_assets": [],
    }


def render_title(title=None):
    folder = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(folder),
            ]
        ),
        autoescape=True,
    )
    return env.get_template("title.html").render(
        title=title or title_fixture(), csrf_token="fixture-csrf"
    )


def test_watched_and_media_verified_are_independent_rendered_states():
    html = render_title()
    parsed = EpisodeRows()
    parsed.feed(html)
    assert "is-watched" in parsed.rows[1]["classes"] and "is-verified" in parsed.rows[1]["classes"]
    assert (
        "is-watched" in parsed.rows[2]["classes"] and "is-verified" not in parsed.rows[2]["classes"]
    )
    assert "Not yet verified" in parsed.rows[2]["text"]
    assert "is-next" in parsed.rows[3]["classes"] and "is-watched" not in parsed.rows[3]["classes"]
    assert (
        "is-verified" in parsed.rows[4]["classes"] and "is-watched" not in parsed.rows[4]["classes"]
    )
    assert "Media verified" in parsed.rows[4]["text"] and "Unwatched" in parsed.rows[4]["text"]
    assert "is-verified" not in parsed.rows[5]["classes"]
    assert "is-upcoming" in parsed.rows[7]["classes"] and "Upcoming" in parsed.rows[7]["text"]
    assert "is-watched" not in parsed.rows[7]["classes"]
    assert html.count('class="episode-verification-icon"') == 3


def test_original_release_and_episode_numbers_are_disclosed_without_changing_action_contracts():
    html = render_title()
    technical = html.split('<details class="evidence-drawer">', 1)[1].split("</details>", 1)[0]
    assert (
        "Original release name" in technical
        and title_fixture()["current_assets"][0]["source"] in technical
    )
    assert "Verified episode numbers" in technical and "1, 3, 4" in technical
    assert 'action="/plus/library/1/reprobe"' in technical
    assert 'action="/plus/library/1/quarantine"' in technical
    assert 'name="asset_id" value="2"' in technical and 'value="fixture-csrf"' in technical
    assert 'name="reason" value="User stopped using this release"' in technical
    assert "3 / 6 episodes verified" in html
    assert "Releases in use" in html
    assert '<details class="media-last-check">' in html


def test_completed_watch_status_does_not_invent_episode_progress():
    title = title_fixture()
    title.update(watch_status="completed", progress_episodes=0, next_episode=None)
    parsed = EpisodeRows()
    parsed.feed(render_title(title))
    assert all("is-watched" not in row["classes"] for row in parsed.rows.values())
