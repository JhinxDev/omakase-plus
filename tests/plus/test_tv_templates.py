"""Actual TV templates keep catalog, viewing history and Plex presence separate."""

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
    assert "Not watched" in html
    assert "Availability unknown" in html
    assert "Ready" not in html
    assert "Sync collection from Plex" in html


def test_detail_separates_history_inventory_and_no_playback_claim():
    html = render("tv_detail.html", card=show(), selected_season=1)
    assert "1 episode watched" in html
    assert "1 episode indexed" in html
    assert "Season coverage unknown" in html
    assert "episode confirmed" not in html
    assert "In Plex" in html
    assert "Air date unknown" in html
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


def test_future_episode_shows_air_date_and_disabled_watched_action():
    card = show()
    card["seasons"][0]["episodes"][1]["air_date"] = "2099-01-01"
    html = render("tv_detail.html", card=card, selected_season=1)
    assert "Not yet aired" in html
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
    assert "In Plex" in html and "38 episodes indexed" in html
    assert "Season coverage unknown" in html
    assert "38 in Plex" not in html and "38 seasons" not in html


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
