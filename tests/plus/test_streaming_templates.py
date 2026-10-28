"""Security and truthful state contracts for the poster-led viewing surfaces."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None
        self.details = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            assert self.current is None, "nested forms break action boundaries"
            self.current = {**attributes, "inputs": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"][attributes.get("name")] = attributes.get("value")
        elif tag == "details":
            self.details[attributes.get("id", attributes.get("class"))] = attributes

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class MediaSemantics(HTMLParser):
    """Record named availability values and whether assistive tech can reach them."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.badges = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (
            attributes.get("aria-hidden") == "true"
            or "hidden" in attributes
            or any(node["hidden"] for node in self.stack)
        )
        node = {"tag": tag, "attrs": attributes, "hidden": hidden, "text": ""}
        if "ready-count" in attributes.get("class", "").split():
            self.badges.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data


def render(name, **context):
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    environment = Environment(
        loader=FileSystemLoader(templates), autoescape=select_autoescape(["html"])
    )
    return environment.get_template(name).render(
        csrf_token="test-csrf",
        email="viewer@example.test",
        active_page="library",
        asset_version="test",
        **context,
    )


@pytest.mark.parametrize(
    "state,label",
    [
        ("ready", "Ready in Plex"),
        ("verifying", "Check in Plex"),
        ("deferred", "Needs attention"),
        ("not_requested", "Not requested"),
    ],
)
def test_movie_badge_does_not_confuse_unrequested_with_absent(state, label):
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    macro = environment.get_template("_movie_components.html").module.movie_poster
    html = str(macro(dict(imdb_id="tt0084787", title="The Thing", requested=False, state=state)))
    assert label in html
    assert "Not in your library" not in html


def library_context(lifecycle="active"):
    return dict(
        lifecycle_filter=lifecycle,
        query="",
        state_filter="all",
        provider_filter="all",
        content_type_filter="all",
        title_suggestions=[],
        titles=[],
        saved_picks=[],
        plex_library={"connected": False},
    )


def title_context():
    return dict(
        id=7,
        anilist_id=182205,
        title="Example anime",
        season_number=4,
        format="TV",
        release_status_label="Finished airing",
        episode_total=24,
        required=24,
        usable=19,
        plex_visible=19,
        usable_episodes=list(range(1, 16)) + [18, 19, 20, 21],
        state="acquiring",
        lifecycle="in_progress",
        lifecycle_label="Watching",
        watch_status="watching",
        progress_episodes=10,
        progress_percent=42,
        next_episode=11,
        last_source_label="Manual",
        publication_pending=False,
        pending_confirmations=[
            dict(id=9, proposed_episode=11, prompt="Confirm episode 11", source_label="Plex")
        ],
        current_assets=[],
        other_assets=[],
        watch_timeline=[],
        timeline=[],
        genres=["Fantasy", "Adventure"],
        removal=None,
    )


def test_saved_watchlist_is_local_planning_with_exact_intake_and_csrf():
    context = library_context()
    context["saved_picks"] = [dict(anilist_id=182205, title="Saved anime")]
    html = render("library.html", **context)
    parsed = Forms()
    parsed.feed(html)
    posts = {form["action"]: form for form in parsed.forms if form["method"] == "post"}
    assert posts["/plus/add/preview"]["inputs"]["query"] == ("https://anilist.co/anime/182205/")
    assert "/plus/watchlist/182205/remove" in posts
    assert all(form["inputs"]["csrf_token"] == "test-csrf" for form in posts.values())
    assert "Availability not checked" in html
    assert "Saved for later" in html
    assert 'href="/plus/library?lifecycle=in_progress' in html
    assert ">Watching</a>" in html
    assert ">Pending</a>" in html


def test_completed_selection_preserves_cleanup_boundary_without_nested_forms():
    context = library_context("completed")
    context["titles"] = [title_context()]
    html = render("library.html", **context)
    parsed = Forms()
    parsed.feed(html)
    cleanup = next(
        form for form in parsed.forms if form["action"] == "/plus/library/completed/cleanup/preview"
    )
    assert cleanup["inputs"]["csrf_token"] == "test-csrf"
    assert cleanup["inputs"]["monitor_id"] == "7"
    assert "open" not in parsed.details["watchlist-coverage"]
    assert "Partial <b>19/24</b>" in html
    assert 'aria-label="Example anime: 19 of 24 required episodes verified and in Plex"' in html
    assert 'aria-label="Example anime: 10 of 24 episodes watched"' in html


@pytest.mark.parametrize(
    ("usable", "plex_visible", "state", "ready", "status_class"),
    [
        (24, 24, "ready", 24, "is-ready"),
        (24, 19, "acquiring", 19, "is-partial"),
        (19, 24, "acquiring", 19, "is-partial"),
        (0, 0, "acquiring", 0, "is-waiting"),
    ],
)
def test_readiness_count_requires_both_usable_files_and_plex_visibility(
    usable, plex_visible, state, ready, status_class
):
    context = library_context()
    title = title_context()
    title.update(usable=usable, plex_visible=plex_visible, state=state)
    context["titles"] = [title]
    parsed = MediaSemantics()
    parsed.feed(render("library.html", **context))

    assert len(parsed.badges) == 1
    badge = parsed.badges[0]
    assert badge["text"].strip() == (
        f"In Plex {ready}/24"
        if state == "ready"
        else f"Partial {ready}/24"
        if ready
        else "Missing 24 episodes"
    )
    assert status_class in badge["attrs"]["class"].split()
    assert badge["attrs"]["aria-label"] == (
        f"Example anime: {ready} of 24 required episodes verified and in Plex"
    )
    assert not badge["hidden"], "availability must remain accessible beside poster artwork"


def test_ready_unstarted_title_keeps_full_title_without_empty_watched_progress():
    context = library_context()
    title = title_context()
    title.update(
        title="BLEACH: Thousand-Year Blood War - The Calamity",
        usable=24,
        plex_visible=24,
        state="ready",
        progress_episodes=0,
        progress_percent=0,
        watch_status="unwatched",
    )
    context["titles"] = [title]
    html = render("library.html", **context)

    assert f'<h2 title="{title["title"]}">{title["title"]}</h2>' in html
    assert "In Plex <b>24/24</b>" in html
    progress = html.split('<div class="media-progress media-progress-compact">', 1)[1].split(
        "</div>", 1
    )[0]
    assert "Not started" in progress
    assert "<progress" not in progress
    assert "Watched" not in progress


def test_title_keeps_watch_and_confirmation_security_and_optional_synopsis_escaped():
    title = title_context()
    title["description"] = "A quiet adventure. <script>unsafe()</script>"
    html = render("title.html", title=title)
    parsed = Forms()
    parsed.feed(html)
    posts = {form["action"]: form for form in parsed.forms if form["method"] == "post"}
    for suffix in (
        "watch/next",
        "watch/episode",
        "watch/complete",
        "watch/confirm/9/accept",
        "watch/confirm/9/dismiss",
        "retry",
        "rescan",
        "rematch-plan",
    ):
        assert posts[f"/plus/library/7/{suffix}"]["inputs"]["csrf_token"] == "test-csrf"
    assert "open" not in parsed.details["watch-tracking"]
    assert "Fantasy" in html and "Adventure" in html
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in html
    assert "/plus/library/7/remove" in html
    assert "not individual episode playback" in html


def test_completed_title_preserves_reopen_and_separate_media_removal():
    title = title_context()
    title["watch_status"] = "completed"
    html = render("title.html", title=title, completed=True)
    parsed = Forms()
    parsed.feed(html)
    posts = {form["action"]: form for form in parsed.forms if form["method"] == "post"}
    assert "/plus/library/7/watch/reopen" in posts
    assert "/plus/library/7/watch/next" not in posts
    assert "Review media removal" in html
    assert "Your watch history is saved" in html
    assert "requires a separate confirmation" in html


def test_collection_search_scope_and_form_contract_are_explicit():
    library = render("library.html", **library_context())
    movies = render("movies.html", cards=[], query="test", view="all")
    tv = render("tv.html", cards=[], query="test", view="all")
    for html, kind, action, field, scope in [
        (library, "anime", "/plus/library", "query", "Filter your saved collection"),
        (movies, "movies", "/plus/movies", "q", "Search the movie catalog"),
        (tv, "tv", "/plus/tv", "q", "Search the TV catalog"),
    ]:
        forms = Forms()
        forms.feed(html)
        search = next(
            f for f in forms.forms if f.get("role") == "search" and f.get("action") == action
        )
        assert search["class"] == "collection-search"
        assert field in search["inputs"]
        assert f'href="/plus/search?kind={kind}"' in html
        assert scope in html
    assert 'name="lifecycle"' in library
    assert 'name="state"' in library and 'name="provider"' in library


def test_missing_anime_is_never_ready_zero():
    context = library_context()
    context["titles"] = [{**title_context(), "required": 27, "usable": 0, "plex_visible": 0}]
    html = render("library.html", **context)
    assert "Missing 27 episodes" in html
    assert "Ready 0/27" not in html
    # Status follows the linked title and progress rather than occupying the poster.
    assert html.index("Missing 27 episodes</span>") > html.index('<h2 title="Example anime"')
