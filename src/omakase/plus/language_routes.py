"""Library-scoped language settings; preferences never constitute track evidence."""

from __future__ import annotations

import os
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Request

from omakase.plus import households, language_publication
from omakase.plus import language_store as store
from omakase.plus.deps import get_db
from omakase.plus.language_policy import LANGUAGES, DomainLanguagePolicies, LanguageRule
from omakase.plus.language_preferences import (
    effective_language,
    member_preference,
    set_member_preference,
)
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_security import csrf_token, require_csrf

HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
}
LISTS = (
    "audio_required",
    "audio_preferred",
    "audio_fallback",
    "subtitle_required",
    "subtitle_preferred",
    "subtitle_fallback",
)
CHOICES = ("audio_requirement", "subtitle_requirement", "subtitles_mode", "subtitle_kind")


def parse_rule(form):
    allowed = set(
        LISTS
        + CHOICES
        + (
            "allow_sdh",
            "csrf_token",
            "library_id",
            "membership_revision",
            "policy_revision",
            "domain",
            "target",
            "action",
            "allow_member_preferences",
            "allow_request_overrides",
        )
    )
    if set(form) - allowed or any(not isinstance(v, str) for _, v in form.multi_items()):
        raise ValueError("Use the supported language fields.")
    for key in form:
        if key not in LISTS and len(form.getlist(key)) != 1:
            raise ValueError("A language setting was submitted more than once.")
    for key in ("allow_sdh", "allow_member_preferences", "allow_request_overrides"):
        if form.get(key, "") not in {"", "1"}:
            raise ValueError("Choose a valid language permission.")
    return LanguageRule(
        **{key: tuple(form.getlist(key)) for key in LISTS},
        **{key: form.get(key, "") for key in CHOICES},
        allow_sdh=form.get("allow_sdh") == "1",
    )


def feature_enabled():
    return all(
        os.getenv(key, "").strip().casefold() in {"1", "true", "yes", "on"}
        for key in ("OMAKASE_PLUS_PRIVATE", "OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED")
    )


def create_language_router(templates, context_factory=None, *, enabled=feature_enabled):
    def gate():
        if not feature_enabled() or not (enabled() if callable(enabled) else enabled):
            raise HTTPException(404, "Not found", headers=HEADERS)

    bounded = bounded_form_route(16384, "Language form is too large", guard=gate)

    class LanguageRoute(bounded):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def protected(request):
                try:
                    return await handler(request)
                except households.HouseholdError as exc:
                    raise HTTPException(exc.status, str(exc), headers=HEADERS) from None

            return protected

    router = APIRouter(route_class=LanguageRoute)

    def render(
        request,
        user,
        db,
        *,
        domain="anime",
        target="personal",
        proposed=None,
        notice="",
        error="",
        code=200,
    ):
        if domain not in {"anime", "movie", "tv"} or target not in {"library", "personal"}:
            raise HTTPException(422, "Choose a media type and settings scope", headers=HEADERS)
        scope = households.selected_scope(db, viewer_id=user.id)
        defaults, member_allowed, override_allowed = store.library_defaults(db, scope)
        if target == "library" and scope.role != "owner":
            raise HTTPException(403, "Only the library owner can edit defaults", headers=HEADERS)
        personal = member_preference(db, scope, domain)
        permitted = scope.role == "owner" or member_allowed
        saved_rule = (
            defaults.for_domain(domain)
            if target == "library"
            else effective_language(db, scope, domain).rule
        )
        rule = proposed or saved_rule
        summary = rule.summary()
        summary["subtitle_language"] = summary["subtitle_language"].replace(
            "full-dialogue selectable subtitles", "full-dialogue subtitles"
        )
        summary["subtitle_format"] = {
            "selectable_required": "Selectable required",
            "selectable_preferred": "Selectable preferred",
            "burned_in_fallback": "Verified burned-in fallback allowed",
        }[rule.subtitle_kind]
        summary["sdh"] = (
            "Full-dialogue SDH allowed"
            if rule.allow_sdh
            else "SDH does not satisfy the subtitle requirement"
        )
        name = db.execute("SELECT name FROM libraries WHERE id=?", (scope.library_id,)).fetchone()[
            0
        ]
        context = (
            context_factory(request, user, active_page="settings")
            if context_factory
            else {"email": user.email}
        )
        return templates.TemplateResponse(
            request,
            "settings_language.html",
            {
                **context,
                "csrf_token": csrf_token(request),
                "scope": scope,
                "library_name": name,
                "domain": domain,
                "target": target,
                "language_rule": rule,
                "language_summary": summary,
                "policy_source": "Preview"
                if proposed
                else (
                    "Your saved preference"
                    if target == "personal" and personal and permitted
                    else "Library default"
                ),
                "language_options": LANGUAGES,
                "delivery_qualified": language_publication.LANGUAGE_PUBLICATION_QUALIFIED,
                "member_allowed": member_allowed,
                "override_allowed": override_allowed,
                "can_edit": target == "library" or permitted,
                "has_preference": personal is not None,
                "language_notice": notice,
                "language_error": error,
            },
            status_code=code,
            headers=HEADERS,
        )

    @router.get("/settings/languages")
    async def page(
        request: Request,
        domain: str = "anime",
        target: str = "personal",
        db=Depends(get_db),
        user=Depends(require_user),
    ):
        return render(request, user, db, domain=domain, target=target)

    @router.post("/settings/languages")
    async def save(request: Request, db=Depends(get_db), user=Depends(require_user)):
        if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "Use the language settings form", headers=HEADERS)
        form = await request.form()
        require_csrf(request, form.get("csrf_token", ""))
        domain, target = form.get("domain", ""), form.get("target", "")
        if domain not in {"anime", "movie", "tv"} or target not in {"library", "personal"}:
            raise HTTPException(422, "Choose a media type and settings scope", headers=HEADERS)
        scope = households.selected_scope(db, viewer_id=user.id)
        if (
            form.get("library_id"),
            form.get("membership_revision"),
            form.get("policy_revision"),
        ) != (scope.library_id, str(scope.membership_revision), str(scope.policy_revision)):
            raise HTTPException(
                409, "Library or permissions changed. Reload before saving.", headers=HEADERS
            )
        defaults, member_allowed, override_allowed = store.library_defaults(db, scope)
        if (
            target == "library"
            and scope.role != "owner"
            or target == "personal"
            and scope.role != "owner"
            and not member_allowed
        ):
            raise HTTPException(
                403, "These language settings are controlled by the library owner", headers=HEADERS
            )
        try:
            rule = parse_rule(form)
            action = form.get("action")
            if target == "personal" and any(
                k in form for k in ("allow_member_preferences", "allow_request_overrides")
            ):
                raise ValueError("Personal preferences cannot change library permissions.")
            if action not in {"preview", "save", "reset"}:
                raise ValueError("Choose preview, save or reset.")
            if action == "preview":
                return render(
                    request,
                    user,
                    db,
                    domain=domain,
                    target=target,
                    proposed=rule,
                    notice="Preview only. Nothing has been saved.",
                )
            if target == "library":
                chosen = (
                    DomainLanguagePolicies.legacy_defaults().for_domain(domain)
                    if action == "reset"
                    else rule
                )
                store.set_library_defaults(
                    db,
                    scope,
                    replace(defaults, **{domain: chosen}),
                    allow_member_preferences=form.get("allow_member_preferences") == "1",
                    allow_request_overrides=form.get("allow_request_overrides") == "1",
                )
            else:
                set_member_preference(db, scope, domain, None if action == "reset" else rule)
            return render(
                request,
                user,
                db,
                domain=domain,
                target=target,
                notice="Library defaults restored for this media type."
                if action == "reset" and target == "library"
                else "Using the library default again."
                if action == "reset"
                else "Language settings saved. Existing media and pending requests were not changed.",
            )
        except households.HouseholdError as exc:
            raise HTTPException(exc.status, str(exc), headers=HEADERS) from None
        except ValueError as exc:
            return render(request, user, db, domain=domain, target=target, error=str(exc), code=422)

    return router
