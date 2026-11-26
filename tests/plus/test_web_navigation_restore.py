"""Web navigation restore: a list keeps its context across a detail visit.

The runtime behavior is proved in a real browser; these tests bind the shipped
script contract and the markup that routes every title-detail Back control
through it, including the plain URL kept for direct links and no-JS use.
"""

from pathlib import Path

import pytest

PLUS = Path(__file__).resolve().parents[2] / "src/omakase/plus"
TEMPLATES = PLUS / "templates"
SCRIPT = PLUS / "static/navigation-restore.js"

# Detail template -> the list destination kept when there is nothing to restore.
DETAIL_TEMPLATES = [
    ("title.html", "/plus/library"),
    ("movie_detail.html", "/plus/movies"),
    ("tv_detail.html", "/plus/tv"),
    ("catalog_anime.html", "/plus/search?kind=anime"),
    ("household_media.html", "/plus/libraries/"),
    ("recommendation.html", "/plus/discover?run="),
]


def test_restore_script_is_linked_on_every_page_after_the_shelf_enhancer():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert base.count("/plus/static/navigation-restore.js") == 1
    assert base.index("shelves.js") < base.index("navigation-restore.js")
    assert SCRIPT.is_file()


def test_restore_script_is_registered_for_static_delivery():
    routes = (PLUS / "routes.py").read_text(encoding="utf-8")
    assert '"navigation-restore.js": "application/javascript"' in routes


@pytest.mark.parametrize(("template", "fallback"), DETAIL_TEMPLATES)
def test_detail_templates_route_back_through_the_shared_restore(template, fallback):
    html = (TEMPLATES / template).read_text(encoding="utf-8")
    controls = [line for line in html.splitlines() if "data-nav-back" in line]
    macro_calls = [line for line in html.splitlines() if "detail_back(" in line]
    assert controls or macro_calls, f"{template} has no shared restore Back control"
    for line in controls:
        assert f'href="{fallback}' in line, line
    for line in macro_calls:
        assert f"detail_back('{fallback}'" in line, line
    if macro_calls:
        macro = (TEMPLATES / "_title_detail.html").read_text(encoding="utf-8")
        assert 'href="{{ href }}"' in macro and "data-nav-back" in macro


def test_record_captures_the_context_needed_to_return():
    script = SCRIPT.read_text(encoding="utf-8")
    for token in (
        "'omakase.nav.origin'",
        "sessionStorage",
        "path: here()",
        "scrollY",
        "rows",
        "focusHref",
        "tab: activeTab()",
        "[data-nav-scroll], .media-rail",
        "omakase:rows-ready",
        "back_forward",
        "referrerPath",
    ):
        assert token in script, token


@pytest.mark.parametrize(
    "token",
    [
        "scrollTo(0, target)",
        "rail.scrollLeft = target",
        "focus({ preventScroll: true })",
        "history.back()",
        "location.assign(record.path)",
    ],
)
def test_restore_applies_scroll_carousel_and_in_page_back(token):
    assert token in SCRIPT.read_text(encoding="utf-8")


def test_direct_entry_has_no_restore_and_the_script_stays_quiet_and_bounded():
    script = SCRIPT.read_text(encoding="utf-8")
    # No record or no return evidence leaves the page at its normal list top.
    assert "if (!record || record.path !== here()) return false;" in script
    assert "if (!returningTo(record)) return false;" in script
    # The Back control never overwrites the record it is about to restore.
    assert "anchor.closest('[data-nav-back]')" in script
    assert "console." not in script
    assert "setInterval" not in script
