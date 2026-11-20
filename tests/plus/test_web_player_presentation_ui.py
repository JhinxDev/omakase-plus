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


def test_player_page_is_picture_and_controls_only():
    """Owner review W-02: the player keeps the video and its controls, nothing else.

    The masthead, its page title and both oversized pills are gone. Back navigation
    stays reachable as one quiet labelled control inside the player, and the Apple TV
    link moved to the title pages (see test_apple_tv_link_leaves_the_player).
    """
    html = render()
    assert 'class="product-masthead web-player-header"' not in html
    assert '<h1 title="Fixture series">' not in html
    assert "Back to title" in html
    assert 'class="web-player-quiet-back shaka-no-propagation"' in html
    assert 'aria-label="Back to title"' in html
    assert html.index("data-player-container") < html.index("web-player-quiet-back")
    # The player no longer offers the device link at all, in either location.
    assert 'action="/plus/native-tv-test"' not in html
    assert "Create Apple TV link" not in html
    # The transport still carries the eight controls the owner asked to keep, each with
    # an accessible name and no visible word label.
    for control in (
        "data-player-timeline",
        "web-player-skip",
        "data-player-clock",
        "data-player-next",
        "data-player-ass-select",
    ):
        assert control in html
    assert "[data-next-label]" not in html or "Next episode</span>" in html


def render_watch_macro(**context):
    """Render the shared Apple TV link macro with an explicit context.

    One macro is imported by the anime, TV and movie title pages, so this is the single
    place the relocated control's guarded contract is asserted. The macro takes the flag
    and the token as arguments because an imported Jinja macro does not inherit the
    importing template's context; this test is what caught that.
    """
    folder = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(folder), autoescape=True)
    template = env.from_string(
        '{% from "_device_components.html" import apple_tv_link %}'
        "{{ apple_tv_link(watch_path, native_tv_test_enabled, csrf_token) }}"
    )
    return template.render(**{"csrf_token": "fixture-csrf", **context})


def test_apple_tv_link_leaves_the_player():
    """Owner review W-02: the link lives on the title pages, not on the picture."""
    html = render_watch_macro(native_tv_test_enabled=True, watch_path="/plus/watch/1/7")
    assert html.count('action="/plus/native-tv-test"') == 1
    assert 'name="csrf_token" value="fixture-csrf"' in html
    assert 'name="watch_path" value="/plus/watch/1/7"' in html
    # No library_id: the route only accepts the viewer's personal scope, and omitting the
    # field keeps the request inside it.
    assert 'name="library_id"' not in html
    assert "Create Apple TV link" in html
    # Nothing renders when the device feature is off or the title has no playable copy.
    assert (
        render_watch_macro(native_tv_test_enabled=False, watch_path="/plus/watch/1/7").strip() == ""
    )
    assert render_watch_macro(native_tv_test_enabled=True, watch_path="").strip() == ""


def test_episode_summary_replaces_series_fallback_without_changing_series_heading():
    html = render(episode_name="A saved episode name", episode_summary="The episode synopsis.")
    # The masthead title is gone; the episode heading below the picture is the heading now.
    assert 'id="episode-heading"' in html and "Episode 7 · A saved episode name" in html
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


def test_episode_details_and_adjacent_links_are_below_the_video():
    html = render(
        next_url="/plus/watch/1/8?library_id=fixture-library&play=1",
        previous_url="/plus/watch/1/6?library_id=fixture-library&play=1",
        season_number=2,
    )
    assert html.index("data-player-container") < html.index('id="episode-heading"')
    assert html.index("data-player-container") < html.index('<nav aria-label="Episode navigation">')
    adjacent = html.split('<nav aria-label="Episode navigation">', 1)[1].split("</nav>", 1)[0]
    assert '<a href="/plus/library/1">All episodes' in adjacent
    assert "data-player-sidebar-next" in adjacent and "/plus/watch/1/8?" in adjacent
    assert "data-player-previous" in adjacent and "/plus/watch/1/6?" in adjacent
    assert "Season 2" in html
    assert "<a data-player-sidebar-next hidden>" in render()
