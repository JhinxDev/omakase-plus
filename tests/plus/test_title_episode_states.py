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
    assert (
        "Media verified" not in parsed.rows[4]["text"] and "Unwatched" not in parsed.rows[4]["text"]
    )
    file_details = html.split('<details class="episode-file-details">', 1)[1].split(
        "</details>", 1
    )[0]
    assert "Episode 4" in file_details and "Media verified" in file_details
    assert "is-verified" not in parsed.rows[5]["classes"]
    assert "is-upcoming" in parsed.rows[7]["classes"] and "Upcoming" in parsed.rows[7]["text"]
    assert "is-watched" not in parsed.rows[7]["classes"]
    assert html.count('class="episode-verification-icon"') == 3
    assert html.index('<details class="title-technical-details"') < html.index(
        '<details class="episode-file-details">'
    )
    assert "3 verified · 3 in Plex" not in html
    assert "3 of 6 episodes verified" not in html
    assert html.count("3 / 6 episodes verified") == 1
    assert "Library evidence" in html


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


def test_anime_series_cover_and_playhead_keep_exact_play_targets():
    title = title_fixture()
    title.update(
        play_url="/plus/watch/1/3?play=1",
        play_episode=3,
        play_label="Resume",
        episode_play_urls={1: "/plus/watch/1/1?play=1", 3: "/plus/watch/1/3?play=1"},
        episode_resume={"percent": 28, "minutes_left": 17},
    )
    html = render_title(title)
    assert "Continue watching · E3" in html
    assert 'aria-label="Replay episode 1"' in html
    assert 'aria-label="Resume episode 3"' in html
    assert 'value="28" max="100" aria-label="Episode 3 saved position"' in html
    assert "17 min left" in html
    assert 'data-series-episodes="/plus/series/episodes/anime/1"' in html
    assert 'data-episode-still="/plus/series/artwork/anime/1/3"' in html


def test_quiet_anime_art_links_keep_single_keyboard_action_and_tv_default():
    title = title_fixture()
    title["episode_play_urls"] = {4: "/plus/watch/1/4?play=1"}
    html = render_title(title)
    assert (
        'class="series-episode-caption-link" href="/plus/watch/1/4?play=1" aria-label="Play episode 4"'
        in html
    )
    assert (
        'class="series-episode-art-action" href="/plus/watch/1/4?play=1" tabindex="-1" aria-hidden="true"'
        in html
    )
    assert "Unwatched" not in html
    folder = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(folder), autoescape=True)
    default = str(
        env.get_template("_series_components.html").module.episode_frame(
            4, "Not watched", play_url="/plus/watch/title/tv/321/1/4?play=1"
        )
    )
    assert 'class="series-episode-state">Not watched</span>' in default
    assert "playback-link--episode" in default and "series-episode-art-action" not in default


def test_quiet_episode_cards_preserve_fallback_upcoming_and_removed_warnings():
    title = title_fixture()
    title["episode_qualities"] = {
        4: {
            "quality": "720p",
            "label": "720p fallback release",
            "detail": "1080p not confirmed",
            "alternates": ["1080p"],
        }
    }
    html = render_title(title)
    assert html.count("720p fallback release") == 2  # Card warning and file evidence.
    assert "1080p found · publication unconfirmed" in html
    assert "Not yet verified" in html and "Upcoming" in html
    title["state"] = "archived"
    removed = render_title(title)
    assert "Media removed" in removed
    assert "Removed media is not available for playback." in removed


def test_unknown_episode_count_keeps_media_diagnostics_without_an_episode_range():
    title = title_fixture()
    title["display_episode_count"] = None
    html = render_title(title)
    assert "The episode list will appear when the title's episode count is known." in html
    assert 'class="episode-file-details"' not in html
    assert 'id="media-availability"' in html
