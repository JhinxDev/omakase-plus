"""Render actual language settings with synthetic library data, no app DB."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from omakase.plus.language_policy import LANGUAGES, DomainLanguagePolicies, LanguageRule


def render(root, output):
    env = Environment(
        loader=FileSystemLoader(root / "src/omakase/plus/templates"), autoescape=select_autoescape()
    )
    rule = DomainLanguagePolicies.legacy_defaults().anime
    context = dict(
        email="member@example.test",
        asset_version="synthetic-language",
        active_page="settings",
        csrf_token="synthetic-csrf",
        scope=SimpleNamespace(
            library_id="synthetic-library", role="owner", membership_revision=2, policy_revision=3
        ),
        library_name="A synthetic household with a long library name that should wrap clearly on a narrow phone",
        domain="anime",
        target="library",
        language_rule=rule,
        language_summary=rule.summary(),
        policy_source="Library default",
        language_options=LANGUAGES,
        delivery_qualified=False,
        member_allowed=True,
        override_allowed=True,
        can_edit=True,
        has_preference=False,
    )
    states = {"owner": copy.deepcopy(context)}
    context.update(
        target="personal",
        scope=SimpleNamespace(
            library_id="synthetic-library", role="member", membership_revision=2, policy_revision=3
        ),
        policy_source="Your saved preference",
    )
    states["member"] = copy.deepcopy(context)
    context.update(can_edit=False, member_allowed=False)
    states["locked"] = copy.deepcopy(context)
    context.update(
        can_edit=True,
        member_allowed=True,
        domain="movie",
        language_notice="Preview only. Nothing has been saved.",
        policy_source="Preview",
    )
    rule = LanguageRule(
        audio_required=("fr", "en"),
        audio_requirement="all_of",
        subtitles_mode="preferred",
        subtitle_preferred=("fr-CA",),
        allow_sdh=True,
    )
    context.update(language_rule=rule, language_summary=rule.summary())
    states["preview"] = copy.deepcopy(context)
    context.update(
        language_notice="", language_error="Fallback languages must differ from required languages."
    )
    states["error"] = copy.deepcopy(context)
    context.update(
        language_error="",
        domain="tv",
        language_rule=LanguageRule(),
        language_summary=LanguageRule().summary(),
    )
    states["tv"] = copy.deepcopy(context)
    output.mkdir(parents=True, exist_ok=True)
    for name, ctx in states.items():
        rule = ctx["language_rule"]
        ctx["language_summary"]["subtitle_language"] = ctx["language_summary"][
            "subtitle_language"
        ].replace("full-dialogue selectable subtitles", "full-dialogue subtitles")
        ctx["language_summary"]["subtitle_format"] = {
            "selectable_required": "Selectable required",
            "selectable_preferred": "Selectable preferred",
            "burned_in_fallback": "Verified burned-in fallback allowed",
        }[rule.subtitle_kind]
        ctx["language_summary"]["sdh"] = (
            "Full-dialogue SDH allowed"
            if rule.allow_sdh
            else "SDH does not satisfy the subtitle requirement"
        )
        (output / (name + ".html")).write_text(
            env.get_template("settings_language.html").render(**ctx), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.root, args.output)
