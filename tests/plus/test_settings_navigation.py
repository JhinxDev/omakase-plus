"""Settings grouping and navigation affordances remain clear and accessible."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


class Navigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = []
        self.headings = []
        self.links = []
        self.heading = None
        self.link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.sections.append(attrs)
        elif tag == "h2":
            self.heading = {"attrs": attrs, "text": ""}
            self.headings.append(self.heading)
        elif tag == "a":
            self.link = {"attrs": attrs, "text": "", "icons": []}
            self.links.append(self.link)
        elif tag == "svg" and self.link is not None:
            self.link["icons"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "h2":
            self.heading = None
        elif tag == "a":
            self.link = None

    def handle_data(self, data):
        if self.heading is not None:
            self.heading["text"] += data
        if self.link is not None:
            self.link["text"] += data


def navigation(active="preferences", household=False):
    root = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(root), autoescape=select_autoescape(["html"]))
    markup = str(env.get_template("_settings_nav.html").module.settings_nav(active, household))
    parsed = Navigation()
    parsed.feed(markup)
    return parsed


@pytest.mark.parametrize("household", [False, True])
def test_static_group_headings_name_groups_without_becoming_actions(household):
    nav = navigation(household=household)
    assert [heading["text"].strip() for heading in nav.headings] == [
        "Account",
        "Your experience",
        "Your services",
    ]
    ids = [heading["attrs"].get("id") for heading in nav.headings]
    assert all(ids) and len(set(ids)) == 3
    assert [section.get("aria-labelledby") for section in nav.sections] == ids
    for heading in nav.headings:
        assert not {"href", "tabindex", "onclick", "role"} & heading["attrs"].keys()


@pytest.mark.parametrize(
    "active,path",
    [
        ("profile", "/plus/settings/profile"),
        ("signin", "/plus/auth/google/account"),
        ("household", "/plus/settings/household"),
        ("preferences", "/plus/settings/preferences"),
        ("tracking", "/plus/settings/tracking"),
        ("downloads", "/plus/settings/downloads"),
        ("connections", "/plus/settings/connections"),
        ("storage", "/plus/settings/storage"),
        ("sources", "/plus/settings/sources"),
        ("ai", "/plus/settings/ai"),
        ("agents", "/plus/settings/agents"),
        ("updates", "/plus/settings/updates"),
    ],
)
def test_current_page_is_unique_and_navigation_cues_are_decorative(active, path):
    nav = navigation(active, household=True)
    assert [
        link["attrs"]["href"] for link in nav.links if link["attrs"].get("aria-current") == "page"
    ] == [path]
    for link in nav.links:
        assert link["text"].strip()
        assert "tabindex" not in link["attrs"]
        assert len(link["icons"]) == 1
        assert link["icons"][0].get("aria-hidden") == "true"
        assert link["icons"][0].get("focusable") == "false"


def test_household_permission_and_existing_health_setup_destinations_remain_exact():
    absent = [link["attrs"]["href"] for link in navigation(household=False).links]
    present = [link["attrs"]["href"] for link in navigation(household=True).links]
    assert "/plus/settings/household" not in absent
    assert set(present) - set(absent) == {"/plus/settings/household"}
    assert len(absent) == 13 and len(present) == 14
    assert "/plus/health/overview" in absent and "/plus/setup" in absent
