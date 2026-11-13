"""Player presentation preserves exact guarded actions and honest episode fallback."""

from pathlib import Path
from types import SimpleNamespace

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader


def render(**overrides):
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
    return env.get_template("player.html").render(
        **{
            "title": "Fixture series",
            "episode_number": 7,
            "monitor_id": 1,
            "library_id": "fixture-library",
            "csrf_token": "fixture-csrf",
            "native_tv_test_enabled": True,
            "overview_url": "/plus/library/1",
            "request": SimpleNamespace(url=SimpleNamespace(path="/plus/watch/1/7")),
            "description_preview": "The series synopsis.",
            **overrides,
        }
    )


def test_tv_link_has_separate_responsive_locations_with_the_same_guarded_contract():
    html = render()
    assert 'class="web-player-tv-test web-player-tv-test--desktop"' in html
    assert 'class="web-player-tv-test web-player-tv-test--phone"' in html
    assert html.count('action="/plus/native-tv-test"') == 2
    assert html.count('name="csrf_token" value="fixture-csrf"') == 2
    assert html.count('name="watch_path" value="/plus/watch/1/7"') == 2
    assert html.count('name="library_id" value="fixture-library"') == 2
    assert html.index("web-player-tv-test--phone") > html.index('class="web-player-tools"')
    assert "Create Apple TV link" not in render(native_tv_test_enabled=False)


def test_episode_summary_replaces_series_fallback_without_changing_series_heading():
    html = render(episode_name="A saved episode name", episode_summary="The episode synopsis.")
    assert '<h1 title="Fixture series">Fixture series</h1>' in html
    assert "Episode 7 · A saved episode name" in html
    assert "data-player-episode-synopsis>The episode synopsis.</p>" in html
    assert "data-player-series-synopsis hidden" in html
    fallback = render()
    assert "data-player-episode-heading>Episode 7</h2>" in fallback
    assert "data-player-episode-synopsis hidden" in fallback
    assert "About the series" in fallback and "The series synopsis." in fallback


def test_initial_episode_metadata_is_escaped_and_movies_keep_their_own_synopsis():
    html = render(episode_name="<script>name</script>", episode_summary="<img src=x>")
    assert "&lt;script&gt;name&lt;/script&gt;" in html
    assert "&lt;img src=x&gt;" in html
    assert "<img src=x>" not in html and "<script>name" not in html
    movie = render(episode_number=0)
    assert "About the series" not in movie
    assert "data-player-episode-synopsis" not in movie


def test_adjacent_episode_links_are_in_one_native_disclosure_with_direct_series_access():
    html = render(
        next_url="/plus/watch/1/8?library_id=fixture-library&play=1",
        previous_url="/plus/watch/1/6?library_id=fixture-library&play=1",
        season_number=2,
    )
    assert '<nav aria-label="Series"><a href="/plus/library/1">See episodes' in html
    assert "data-player-episode-navigation>" in html
    assert "data-player-episode-navigation open" not in html
    adjacent = html.split("data-player-episode-navigation>", 1)[1].split("</details>", 1)[0]
    assert "data-player-sidebar-next" in adjacent and "/plus/watch/1/8?" in adjacent
    assert "data-player-previous" in adjacent and "/plus/watch/1/6?" in adjacent
    assert "Season 2" in html
    assert "data-player-episode-navigation hidden" in render()
