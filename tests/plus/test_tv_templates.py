"""Actual TV templates keep catalog, viewing history and Plex presence separate."""

import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            assert self.current is None
            self.current = {**attrs, "inputs": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"][attrs.get("name")] = attrs.get("value")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def render(template, **context):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template(template).render(
        csrf_token="fixture-csrf",
        email="fixture@example.test",
        active_page="tv",
        asset_version="test",
        **context,
    )


def show(**overrides):
    return {
        "tmdb_id": 321,
        "title": "A Series",
        "year": "2026",
        "overview": "A quiet catalog synopsis.",
        "status": "unwatched",
        "status_source": "none",
        "rating": None,
        "watched_episodes": 1,
        "in_plex_episodes": 1,
        "available_episodes": 0,
        "detail": "Matched in Plex. Playback has not been verified.",
        "seasons": [
            {
                "season_number": 1,
                "name": "Season 1",
                "released": True,
                "episodes_loaded": True,
                "episodes": [
                    {
                        "episode_number": 1,
                        "title": "Arrival",
                        "watched": True,
                        "in_plex": True,
                        "available": None,
                        "released": True,
                    },
                    {
                        "episode_number": 2,
                        "title": "Tomorrow",
                        "watched": False,
                        "in_plex": False,
                        "available": None,
                        "released": False,
                    },
                ],
            }
        ],
        **overrides,
    }


def test_collection_exact_identity_and_honest_catalog_state():
    html = render("tv.html", cards=[show(in_plex_episodes=0)], query="", view="all")
    assert 'href="/plus/tv/321"' in html
    assert "1 episode watched" in html
    assert "Not watched" not in html
    assert "Availability unconfirmed" in html and 'data-status-tone="neutral"' in html
    assert "Ready" not in html
    assert "Sync collection from Plex" in html


def test_tv_search_disclosure_has_progressive_native_form_and_preserves_query():
    html = render("tv.html", cards=[show()], query="", view="all")
    assert 'class="collection-search-disclosure" open' not in html
    assert "<summary>Search &amp; filters</summary>" in html
    assert 'action="/plus/tv" role="search"' in html
    assert 'href="/plus/search?kind=tv"' in html
    filtered = render("tv.html", cards=[show()], query="A Series", view="watching")
    assert 'class="collection-search-disclosure" open' in filtered
    assert 'name="q" value="A Series"' in filtered
    assert 'name="view" value="watching"' in filtered
    assert 'href="/plus/tv/321"' in filtered


def test_detail_separates_history_inventory_and_no_playback_claim():
    # Inventory wording ("N episodes indexed", "Season coverage unknown") belongs to the
    # collection surfaces, where test_legacy_raw_inventory_never_claims_a_season_count and
    # test_collection_inventory_and_sync_share_one_closed_disclosure_above_browse_cards pin
    # it. This detail surface states watched history and Plex availability, and makes no
    # claim it cannot support: no playback claim, no inventory count, no air date it lacks.
    html = render("tv_detail.html", card=show(), selected_season=1)
    assert "1 episode watched" in html
    assert "Watch status &amp; rating" in html
    assert "Plex availability" in html
    assert "Matched in Plex. Playback has not been verified." in html
    assert "Check in Plex" in html
    assert "episode confirmed" not in html
    assert "1 episode indexed" not in html
    assert "Season coverage unknown" not in html
    assert "In Plex" not in html
    assert "Air date unknown" not in html
    assert "Ready" not in html
    assert "Open in Plex" not in html
    parser = Forms()
    parser.feed(html)
    posts = [form for form in parser.forms if form.get("method") == "post"]
    assert all(form["inputs"]["csrf_token"] == "fixture-csrf" for form in posts)
    episodes = [form for form in posts if form["action"] == "/plus/tv/episode"]
    assert [form["inputs"]["watched"] for form in episodes] == ["false", "true"]
    assert all(form["inputs"]["tmdb_id"] == "321" for form in episodes)


def test_overview_expands_and_escapes_catalog_markup():
    html = render("tv_detail.html", card=show(overview="<script>bad()</script> " * 30))
    assert "Read full synopsis" in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
    assert "<script>bad()" not in html


def test_empty_states_and_explicit_metadata_load():
    html = render("tv.html", cards=[], query="Missing", view="all")
    assert "No shows found" in html
    html = render(
        "tv_detail.html",
        card=show(seasons=[{"season_number": 2, "name": "Season 2", "episodes": []}]),
        selected_season=2,
    )
    assert 'action="/plus/tv/321/season/2"' in html
    assert "Load season episodes" in html
    assert "Mark watched" not in html


def test_partial_plex_season_offers_explicit_full_listing():
    card = show(plex_snapshot_current=True)
    card["seasons"][0]["episodes_loaded"] = False
    card["seasons"][0]["episodes"] = [e for e in card["seasons"][0]["episodes"] if e["in_plex"]]
    card["seasons"][0]["episodes"][0]["watched"] = False
    html = render("tv_detail.html", card=card, selected_season=1)
    assert "Load full season listing" in html
    assert "Showing episodes observed in Plex" in html
    assert "Arrival" in html
    assert "Not watched" in html


def test_complete_season_listing_does_not_invite_a_reload():
    # Negative case for the partial-listing notice: once the catalog listing is loaded the
    # page keeps quiet about it, so the invitation stays a signal that what is shown may be
    # partial rather than boilerplate every season carries.
    season = {
        "season_number": 1,
        "name": "Season 1",
        "released": True,
        "episodes_loaded": True,
        "episodes": [
            {
                "episode_number": 1,
                "title": "Arrival",
                "watched": False,
                "in_plex": True,
                "available": None,
                "released": True,
            }
        ],
    }
    html = render("tv_detail.html", card=show(seasons=[season]), selected_season=1)
    assert "Arrival" in html
    assert "Load full season listing" not in html
    assert "Showing cached episode details" not in html
    assert "Showing episodes observed in Plex" not in html


def test_future_episode_shows_air_date_and_disabled_watched_action():
    card = show()
    card["seasons"][0]["episodes"][1]["air_date"] = "2099-01-01"
    html = render("tv_detail.html", card=card, selected_season=1)
    assert "Upcoming" in html
    assert "Not yet aired" not in html
    assert 'disabled title="Air date is not confirmed as released"' in html


def test_default_season_skips_specials_but_explicit_zero_is_preserved():
    card = show()
    card["seasons"].insert(0, {"season_number": 0, "name": "Specials", "episodes": []})
    normal = render("tv_detail.html", card=card, selected_season=None)
    assert '<option value="1" selected>' in normal
    special = render("tv_detail.html", card=card, selected_season=0)
    assert '<option value="0" selected>' in special


def test_legacy_raw_inventory_never_claims_a_season_count():
    html = render("tv.html", cards=[show(in_plex_episodes=38)], query="", view="all")
    assert "Available" in html and "38 episodes indexed" in html
    assert "Season coverage unknown" in html
    assert "38 in Plex" not in html and "38 seasons" not in html


def test_collection_inventory_and_sync_share_one_closed_disclosure_above_browse_cards():
    html = render(
        "tv.html",
        cards=[
            show(
                in_plex_episodes=38,
                plex_observed_at="2026-11-14T20:00:00Z",
                season_coverage_known=True,
                plex_season_coverage=[
                    {"season_number": 1, "in_plex": 24, "released_total": 25},
                    {"season_number": 2, "in_plex": 14, "released_total": 16},
                ],
            ),
            show(tmdb_id=654, in_plex_episodes=0, watched_episodes=0),
            show(
                tmdb_id=987,
                title="Previously matched",
                in_plex_episodes=0,
                plex_snapshot_current=False,
                plex_observed_at="2026-11-12T20:00:00Z",
            ),
        ],
        query="",
        view="all",
    )
    details, cards = html.split('<div class="tv-grid catalog-poster-grid">', 1)
    assert 'class="tv-library-tools" id="tv-library-tools">' in details
    assert "Library tools" in details and "Sync collection from Plex" in details
    assert "38 episodes indexed" in details and "38/41 released episodes in Plex" in details
    assert '<time datetime="2026-11-14T20:00:00Z"' in details
    assert "Index unconfirmed" in details and "Season coverage unknown" in details
    assert "38 episodes indexed" not in cards and "Season coverage unknown" not in cards
    assert 'href="/plus/tv/654"' not in details
    assert html.count('class="tv-library-inventory"') == 1
    assert "Open a show to check playback." in details
    assert 'href="/plus/tv/321"' in cards and 'href="/plus/tv/654"' in cards
    forms = Forms()
    forms.feed(html)
    sync = [form for form in forms.forms if form.get("action") == "/plus/tv/sync"]
    assert len(sync) == 1 and sync[0]["method"] == "post"
    assert sync[0]["inputs"] == {"csrf_token": "fixture-csrf", "offset": "0"}
    empty = render(
        "tv.html", cards=[show(in_plex_episodes=0, watched_episodes=0)], query="", view="all"
    )
    assert "No Plex matches recorded for shows on this page." in empty
    assert 'class="tv-library-inventory"' not in empty and "Not watched" not in empty


def test_detail_does_not_count_specials_as_a_fifth_regular_season():
    card = show(
        seasons=[
            {
                "season_number": number,
                "name": "Specials" if number == 0 else f"Season {number}",
                "episodes": [],
                "released": True,
            }
            for number in range(5)
        ]
    )
    html = render("tv_detail.html", card=card, selected_season=1)
    assert "4 seasons" in html
    assert "5 seasons" not in html
    assert "Specials" in html


def test_cached_catalog_subset_does_not_claim_a_complete_show_or_plex_observation():
    card = show(catalog_subset=True, plex_snapshot_current=False)
    card["seasons"][0]["episodes_loaded"] = False
    for episode in card["seasons"][0]["episodes"]:
        episode["in_plex"] = False
    html = render("tv_detail.html", card=card, selected_season=1)
    assert "1 cached season" in html
    assert "Showing cached episode details" in html
    assert "Showing episodes observed in Plex" not in html


def two_seasons():
    return show(
        seasons=[
            {
                "season_number": 1,
                "name": "Season 1",
                "episode_count": 8,
                "released": True,
                "episodes": [],
            },
            {
                "season_number": 2,
                "name": "Season 2",
                "episode_count": 10,
                "released": True,
                "episodes": [],
            },
        ]
    )


def active_picker_form(html, tmdb_id=321):
    """The season picker exactly as a scripting browser accounts for it.

    A browser with scripting enabled parses <noscript> content as inert text and never
    renders it, so removing those blocks leaves the markup the live control actually
    contributes to the page and to the submitted request.
    """
    active = re.sub(r"<noscript>.*?</noscript>", "", html, flags=re.DOTALL)
    match = re.search(
        r'<form class="tv-season-picker product-form"[^>]*'
        rf'action="/plus/tv/{tmdb_id}#tv-seasons"[^>]*>.*?</form>',
        active,
        flags=re.DOTALL,
    )
    assert match is not None, "the season picker form is missing from the active markup"
    return match.group(0)


def test_season_picker_is_one_app_owned_listbox_that_names_the_season_it_shows():
    # Owner report: MobLand season 2 episodes looked missing. The episodes were already
    # served; the old control did nothing until a second "View season" button was pressed.
    # Owner decision #6: one app-owned listbox, styled by the app, updates the episodes.
    html = render("tv_detail.html", card=two_seasons(), selected_season=2)
    picker = active_picker_form(html)
    assert "<select" not in picker
    assert 'name="season"' not in picker
    assert 'type="submit"' not in picker
    assert 'data-listbox-navigate="/plus/tv/321?season={value}#tv-seasons"' in picker
    assert re.findall(r'<li role="option"[^>]*data-value="(\d+)"', picker) == ["1", "2"]
    assert re.findall(
        r'<li role="option"[^>]*aria-selected="true"[^>]*>(.*?)</li>', picker, re.DOTALL
    ) == ["Season 2 · 10 episodes"]
    assert "<span data-listbox-text>Season 2 · 10 episodes</span>" in picker
    assert 'aria-expanded="false"' in picker and " hidden" in picker
    # The behaviour is one shared component, so the page has to ship it.
    assert "/plus/static/listbox.js?v=test" in html
    assert "watch-status.js" not in html


def test_partial_cloud_scan_keeps_confirmed_play_action_and_explains_unknowns():
    html = render(
        "tv_detail.html",
        card=show(),
        selected_season=1,
        watch_in_app=True,
        cloud_playback=True,
        cloud_episodes={(1, 1)},
        cloud_partial=True,
    )
    assert "Some Real-Debrid files are still unchecked." in html
    assert 'href="/plus/watch/title/tv/321/1/1?play=1"' in html
    assert 'href="/plus/watch/title/tv/321/1/2?play=1"' not in html


def test_season_picker_keeps_a_native_fallback_for_a_browser_without_scripting():
    # listbox.js is what turns the markup into a live control, so the same season choice
    # must still be possible, and still submit exactly one "season" value, without it.
    html = render("tv_detail.html", card=two_seasons(), selected_season=2)
    fallback = next(
        (
            block
            for block in re.findall(r"<noscript>(.*?)</noscript>", html, flags=re.DOTALL)
            if 'name="season"' in block
        ),
        None,
    )
    assert fallback is not None, "the season picker has no scripting-free fallback"
    assert '<select name="season">' in fallback
    assert '<option value="2" selected>' in fallback
    assert 'type="submit">View season</button>' in fallback
    # No scripting browser renders the fallback, so the request can never carry two
    # "season" values: one from the listbox and one from the native select.
    assert html.count('name="season"') == 1
