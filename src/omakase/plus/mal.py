"""Qualified MAL v2 / AniList bridge transports; no automatic retries or stored credentials.

MAL contract: official embedded OpenAPI3, captured2026-10-28. Date writes and
score clearing are deliberately unsupported. All calls need an explicit caller.
"""

from __future__ import annotations

import json
import math
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from omakase.plus.tracker_reconciliation import (
    Entry,
    ExactMapping,
    FuzzyDate,
    Inventory,
    Provider,
    State,
    Status,
)

MAL_API = "https://api.myanimelist.net/v2"
MAL_OAUTH = "https://myanimelist.net/v1/oauth2"
ANILIST_API = "https://graphql.anilist.co"
MAL_FIELDS = "list_status{status,score,num_episodes_watched,is_rewatching,num_times_rewatched,start_date,finish_date,updated_at}"
_STATUS = {
    "watching": Status.CURRENT,
    "completed": Status.COMPLETED,
    "on_hold": Status.PAUSED,
    "dropped": Status.DROPPED,
    "plan_to_watch": Status.PLANNING,
}
_AL_STATUS = {
    "CURRENT": Status.CURRENT,
    "COMPLETED": Status.COMPLETED,
    "PAUSED": Status.PAUSED,
    "DROPPED": Status.DROPPED,
    "PLANNING": Status.PLANNING,
    "REPEATING": Status.CURRENT,
}


class TrackerHTTPError(ValueError):
    def __init__(self, code, *, retry_after=None, ambiguous=False, provider=None):
        self.code, self.retry_after, self.ambiguous = code, retry_after, ambiguous
        self.provider = provider
        super().__init__(code)


def _check(ok, code="invalid_provider_payload"):
    if not ok:
        raise TrackerHTTPError(code)


def _int(value, minimum=0, maximum=2**31 - 1):
    _check(type(value) is int and minimum <= value <= maximum)
    return value


def _text(value, maximum=8192):
    _check(
        isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)
    )
    return value


def pkce_verifier():
    return secrets.token_urlsafe(64)


def authorize_url(client_id, redirect_uri, verifier, state):
    _text(client_id)
    _text(state)
    _text(redirect_uri)
    _check(
        43 <= len(verifier) <= 128
        and verifier.isascii()
        and all(c.isalnum() or c in "-._~" for c in verifier),
        "invalid_pkce",
    )
    return (
        MAL_OAUTH
        + "/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": verifier,
                "code_challenge_method": "plain",
                "state": state,
            }
        )
    )


@dataclass(frozen=True)
class TokenBundle:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: int

    def __post_init__(self):
        _text(self.access_token)
        _check(self.access_token.isascii(), "invalid_saved_token")
        if self.refresh_token is not None:
            _text(self.refresh_token)
        _int(self.expires_at, 1, 2**63 - 1)

    def to_json(self):
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value):
        try:
            return cls(**json.loads(value))
        except (TypeError, ValueError, KeyError) as exc:
            raise TrackerHTTPError("invalid_saved_token") from exc


def _retry_after(response, now):
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(1, min(2**31 - 1, int(value)))
    except ValueError:
        try:
            return max(1, min(2**31 - 1, int(parsedate_to_datetime(value).timestamp() - now)))
        except (ValueError, TypeError, OverflowError):
            return None


def _request(http, method, url, *, mutating=False, now=time.time, deadline=None, **kwargs):
    def error(code, **kwargs):
        return TrackerHTTPError(
            code, provider=Provider.ANILIST if url == ANILIST_API else Provider.MAL, **kwargs
        )

    remaining = 10 if deadline is None else min(10, deadline - time.monotonic())
    if remaining <= 0:
        raise error("job_budget_exhausted", retry_after=1)
    try:
        response = http.request(method, url, timeout=remaining, follow_redirects=False, **kwargs)
    except httpx.RequestError as exc:
        raise error(
            "ambiguous_write" if mutating else "transport_unavailable", ambiguous=mutating
        ) from exc
    if response.status_code == 429:
        raise error("rate_limited", retry_after=_retry_after(response, now()) or 60)
    if response.status_code == 401:
        raise error("reconnect_required")
    if not 200 <= response.status_code < 300:
        raise error("provider_rejected", ambiguous=mutating and response.status_code >= 500)
    _check(len(response.content) <= 2 * 1024 * 1024, "response_too_large")
    try:
        data = response.json()
    except ValueError as exc:
        raise error("invalid_provider_payload", ambiguous=mutating) from exc
    _check(isinstance(data, dict))
    return data


def _date(value):
    if value is None:
        return None
    _check(isinstance(value, str) and len(value) in (4, 7, 10))
    try:
        parts = [int(x) for x in value.split("-")]
        return FuzzyDate(*parts)
    except (ValueError, TypeError) as exc:
        raise TrackerHTTPError("invalid_provider_date") from exc


def parse_mal_entry(media_id, payload):
    _int(media_id, 1)
    _check(isinstance(payload, dict))
    required = {
        "status",
        "score",
        "num_episodes_watched",
        "is_rewatching",
        "num_times_rewatched",
        "start_date",
        "finish_date",
        "updated_at",
    }
    _check(required <= payload.keys(), "incomplete_entry")
    _check(isinstance(payload["status"], str) and payload["status"] in _STATUS)
    score = _int(payload["score"], 0, 10)
    progress = _int(payload["num_episodes_watched"], 0, 1_000_000)
    repeat = _int(payload["num_times_rewatched"], 0, 1000)
    _check(type(payload["is_rewatching"]) is bool)
    revision = _text(payload["updated_at"], 40)
    try:
        datetime.fromisoformat(revision.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrackerHTTPError("invalid_provider_revision") from exc
    status = _STATUS[payload["status"]]
    _check(not payload["is_rewatching"] or status == Status.CURRENT, "inconsistent_rewatch")
    return Entry(
        media_id,
        State(
            status,
            progress,
            None if score == 0 else score * 10,
            repeat,
            payload["is_rewatching"],
            _date(payload["start_date"]),
            _date(payload["finish_date"]),
        ),
        revision,
    )


class MALClient:
    def __init__(self, http, client_id="", client_secret="", *, now=time.time):
        self.http, self.client_id, self.client_secret, self.now = (
            http,
            client_id,
            client_secret,
            now,
        )
        self.titles = {}
        self.deadline = None

    def _token(self, form):
        _check(bool(self.client_id), "operator_configuration_required")
        form = {**form, "client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        data = _request(self.http, "POST", MAL_OAUTH + "/token", data=form, now=self.now)
        _check(
            isinstance(data.get("token_type"), str) and data["token_type"].casefold() == "bearer"
        )
        expires = _int(data.get("expires_in"), 1, 366 * 86400)
        return TokenBundle(
            _text(data.get("access_token")),
            _text(data.get("refresh_token")),
            int(self.now()) + expires,
        )

    def exchange(self, code, verifier, redirect_uri):
        authorize_url(self.client_id, redirect_uri, verifier, "validation-only")
        return self._token(
            {
                "grant_type": "authorization_code",
                "code": _text(code),
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )

    def refresh(self, bundle):
        _check(bundle.refresh_token is not None, "reconnect_required")
        return self._token({"grant_type": "refresh_token", "refresh_token": bundle.refresh_token})

    def _get(self, path, token, params=None):
        return _request(
            self.http,
            "GET",
            MAL_API + path,
            headers={"Authorization": "Bearer " + _text(token)},
            params=params,
            now=self.now,
            deadline=self.deadline,
        )

    def viewer(self, token):
        data = self._get("/users/@me", token)
        return _int(data.get("id"), 1), _text(data.get("name"), 100)

    def scan_page(self, token, identity, cursor):
        _check(identity.provider == Provider.MAL and identity.verified, "unverified_identity")
        offset = _int(cursor, 0)
        data = self._get(
            "/users/@me/animelist",
            token,
            {"fields": MAL_FIELDS, "limit": 100, "offset": offset, "nsfw": "true"},
        )
        rows = data.get("data")
        paging = data.get("paging")
        _check(isinstance(rows, list) and len(rows) <= 100 and isinstance(paging, dict))
        entries = []
        for row in rows:
            _check(isinstance(row, dict) and isinstance(row.get("node"), dict))
            mid = _int(row["node"].get("id"), 1)
            entries.append(parse_mal_entry(mid, row.get("list_status")))
            if isinstance(row["node"].get("title"), str):
                self.titles[mid] = row["node"]["title"][:300]
        next_cursor = None
        if paging.get("next"):
            nxt = paging["next"]
            _check(isinstance(nxt, str) and len(nxt) < 4096, "invalid_pagination")
            u = urlsplit(nxt)
            query = parse_qs(u.query)
            _check(
                u.scheme == "https"
                and u.netloc == "api.myanimelist.net"
                and u.path == "/v2/users/@me/animelist"
                and not u.fragment,
                "invalid_pagination",
            )
            try:
                next_cursor = int(query["offset"][0])
            except (ValueError, KeyError, IndexError) as exc:
                raise TrackerHTTPError("invalid_pagination") from exc
            _check(bool(rows) and next_cursor == offset + len(rows), "invalid_pagination")
        return tuple(entries), (), next_cursor

    def inventory(self, token, identity, *, max_pages=100):
        _check(identity.provider == Provider.MAL and identity.verified, "unverified_identity")
        _int(max_pages, 1, 100)
        entries = []
        seen = set()
        offset = 0
        for _ in range(max_pages):
            data = self._get(
                "/users/@me/animelist",
                token,
                {"fields": MAL_FIELDS, "limit": 100, "offset": offset, "nsfw": "true"},
            )
            rows = data.get("data")
            paging = data.get("paging")
            _check(isinstance(rows, list) and len(rows) <= 100 and isinstance(paging, dict))
            for row in rows:
                _check(isinstance(row, dict) and isinstance(row.get("node"), dict))
                mid = _int(row["node"].get("id"), 1)
                _check(mid not in seen, "duplicate_inventory_entry")
                seen.add(mid)
                entries.append(parse_mal_entry(mid, row.get("list_status")))
                if isinstance(row["node"].get("title"), str):
                    self.titles[mid] = row["node"]["title"][:300]
            nxt = paging.get("next")
            if not nxt:
                return Inventory(identity, tuple(entries), complete=True)
            _check(isinstance(nxt, str) and len(nxt) < 4096, "invalid_pagination")
            u = urlsplit(nxt)
            query = parse_qs(u.query)
            _check(
                u.scheme == "https"
                and u.netloc == "api.myanimelist.net"
                and u.path == "/v2/users/@me/animelist"
                and not u.fragment,
                "invalid_pagination",
            )
            try:
                new_offset = int(query["offset"][0])
            except (ValueError, KeyError, IndexError) as exc:
                raise TrackerHTTPError("invalid_pagination") from exc
            _check(new_offset > offset and new_offset == offset + len(rows), "invalid_pagination")
            offset = new_offset
        raise TrackerHTTPError("inventory_incomplete")

    def entry(self, token, media_id):
        _int(media_id, 1)
        data = self._get(
            "/anime/" + str(media_id),
            token,
            {"fields": MAL_FIELDS.replace("list_status", "my_list_status", 1)},
        )
        _check(data.get("id") == media_id and "my_list_status" in data, "incomplete_entry")
        return (
            None
            if data["my_list_status"] is None
            else parse_mal_entry(media_id, data["my_list_status"])
        )

    def patch(self, token, media_id, changes):
        _int(media_id, 1)
        allowed = {"status", "progress", "score", "repeat", "rewatching"}
        _check(
            isinstance(changes, dict) and changes and set(changes) <= allowed, "unsupported_field"
        )
        form = {}
        for name, value in changes.items():
            if name == "status":
                _check(isinstance(value, Status), "invalid_patch")
                form["status"] = {v: k for k, v in _STATUS.items()}[value]
            elif name == "score":
                _check(
                    type(value) is int and 10 <= value <= 100 and value % 10 == 0,
                    "unsupported_score_clear_or_precision",
                )
                form["score"] = str(value // 10)
            elif name == "rewatching":
                _check(type(value) is bool, "invalid_patch")
                form["is_rewatching"] = "true" if value else "false"
            else:
                form[
                    {"progress": "num_watched_episodes", "repeat": "num_times_rewatched"}[name]
                ] = str(_int(value, 0, 1000 if name == "repeat" else 1_000_000))
        payload = _request(
            self.http,
            "PATCH",
            MAL_API + "/anime/" + str(media_id) + "/my_list_status",
            headers={"Authorization": "Bearer " + _text(token)},
            data=form,
            mutating=True,
            now=self.now,
            deadline=self.deadline,
        )
        return parse_mal_entry(media_id, payload)


_AL_FIELDS = "id mediaId status score(format:POINT_100) progress repeat startedAt{year month day} completedAt{year month day} updatedAt media{id idMal type title{userPreferred}}"


def _al_date(payload):
    _check(isinstance(payload, dict))
    parts = [payload.get(k) for k in ("year", "month", "day")]
    if all(v is None for v in parts):
        return None
    try:
        return FuzzyDate(*parts)
    except ValueError as exc:
        raise TrackerHTTPError("invalid_provider_date") from exc


def parse_anilist_entry(payload):
    _check(
        isinstance(payload, dict)
        and isinstance(payload.get("status"), str)
        and payload["status"] in _AL_STATUS
    )
    score = payload.get("score")
    _check(
        type(score) in (int, float)
        and math.isfinite(score)
        and float(score).is_integer()
        and 0 <= score <= 100
    )
    status = payload["status"]
    return Entry(
        _int(payload.get("mediaId"), 1),
        State(
            _AL_STATUS[status],
            _int(payload.get("progress"), 0, 1_000_000),
            None if score == 0 else int(score),
            _int(payload.get("repeat"), 0, 1000),
            status == "REPEATING",
            _al_date(payload.get("startedAt")),
            _al_date(payload.get("completedAt")),
        ),
        str(_int(payload.get("updatedAt"), 0)),
    )


class AniListBridgeClient:
    def __init__(self, http, *, now=time.time):
        self.http, self.now = http, now
        self.titles = {}
        self.deadline = None
        self.entry_ids = {}

    def _graphql(self, token, query, variables=None, *, mutating=False):
        payload = _request(
            self.http,
            "POST",
            ANILIST_API,
            headers={"Authorization": "Bearer " + _text(token)},
            json={"query": query, "variables": variables or {}},
            mutating=mutating,
            now=self.now,
            deadline=self.deadline,
        )
        errors = payload.get("errors")
        if errors:
            rate = (
                any(isinstance(e, dict) and e.get("status") == 429 for e in errors)
                if isinstance(errors, list)
                else False
            )
            raise TrackerHTTPError(
                "rate_limited" if rate else "provider_rejected",
                retry_after=60 if rate else None,
                ambiguous=mutating,
            )
        _check(isinstance(payload.get("data"), dict))
        return payload["data"]

    def viewer(self, token):
        data = self._graphql(token, "query { Viewer { id name } }").get("Viewer")
        _check(isinstance(data, dict))
        return _int(data.get("id"), 1), _text(data.get("name"), 100)

    def _remember(self, row):
        entry = parse_anilist_entry(row)
        self.entry_ids[entry.media_id] = _int(row.get("id"), 1)
        media = row.get("media")
        _check(
            isinstance(media, dict)
            and media.get("id") == entry.media_id
            and media.get("type") == "ANIME"
        )
        title = media.get("title")
        title = title.get("userPreferred") if isinstance(title, dict) else None
        if isinstance(title, str):
            self.titles[entry.media_id] = title[:300]
        return entry

    def scan_page(self, token, identity, cursor):
        _check(identity.provider == Provider.ANILIST and identity.verified, "unverified_identity")
        page = _int(cursor, 1, 200)
        data = self._graphql(
            token,
            "query($user:Int,$page:Int){Page(page:$page,perPage:50){pageInfo{hasNextPage} mediaList(userId:$user,type:ANIME){"
            + _AL_FIELDS
            + "}}}",
            {"user": identity.remote_user_id, "page": page},
        ).get("Page")
        _check(
            isinstance(data, dict)
            and isinstance(data.get("mediaList"), list)
            and isinstance(data.get("pageInfo"), dict)
        )
        rows = data["mediaList"]
        _check(len(rows) <= 50)
        entries = []
        mappings = []
        for row in rows:
            e = self._remember(row)
            entries.append(e)
            mid = row["media"].get("idMal")
            if mid is not None:
                mappings.append(ExactMapping(e.media_id, _int(mid, 1), mid))
        more = data["pageInfo"].get("hasNextPage")
        _check(type(more) is bool)
        _check(not more or bool(rows), "invalid_pagination")
        return tuple(entries), tuple(mappings), page + 1 if more else None

    def inventory(self, token, identity, *, max_pages=200):
        _check(identity.provider == Provider.ANILIST and identity.verified, "unverified_identity")
        _int(max_pages, 1, 200)
        entries = []
        mappings = []
        seen = set()
        for page in range(1, max_pages + 1):
            data = self._graphql(
                token,
                "query($user:Int,$page:Int){Page(page:$page,perPage:50){pageInfo{hasNextPage} mediaList(userId:$user,type:ANIME){"
                + _AL_FIELDS
                + "}}}",
                {"user": identity.remote_user_id, "page": page},
            ).get("Page")
            _check(
                isinstance(data, dict)
                and isinstance(data.get("mediaList"), list)
                and isinstance(data.get("pageInfo"), dict)
            )
            rows = data["mediaList"]
            _check(len(rows) <= 50)
            for row in rows:
                entry = self._remember(row)
                _check(entry.media_id not in seen, "duplicate_inventory_entry")
                seen.add(entry.media_id)
                entries.append(entry)
                mid = row["media"].get("idMal")
                if mid is not None:
                    mappings.append(ExactMapping(entry.media_id, _int(mid, 1), mid))
            more = data["pageInfo"].get("hasNextPage")
            _check(type(more) is bool)
            if not more:
                return Inventory(identity, tuple(entries), complete=True), tuple(mappings)
            _check(bool(rows), "invalid_pagination")
        raise TrackerHTTPError("inventory_incomplete")

    def mapping(self, token, mal_id):
        _int(mal_id, 1)
        row = self._graphql(
            token, "query($mal:Int){Media(idMal:$mal,type:ANIME){id idMal type}}", {"mal": mal_id}
        ).get("Media")
        if row is None:
            return None
        _check(
            isinstance(row, dict) and row.get("type") == "ANIME" and row.get("idMal") == mal_id,
            "mapping_mismatch",
        )
        return ExactMapping(_int(row.get("id"), 1), mal_id, mal_id)

    def entry(self, token, media_id, identity=None):
        _int(media_id, 1)
        _check(
            identity is not None and identity.verified and identity.provider == Provider.ANILIST,
            "unverified_identity",
        )
        row = self._graphql(
            token,
            "query($media:Int,$user:Int){MediaList(mediaId:$media,userId:$user){"
            + _AL_FIELDS
            + "}}",
            {"media": media_id, "user": identity.remote_user_id},
        ).get("MediaList")
        if row is None:
            self.entry_ids.pop(media_id, None)
            return None
        entry = self._remember(row)
        _check(entry.media_id == media_id, "mapping_mismatch")
        return entry

    def patch(self, token, media_id, changes):
        _int(media_id, 1)
        _check(
            isinstance(changes, dict)
            and changes
            and set(changes) <= {"status", "progress", "score", "repeat", "rewatching"},
            "unsupported_field",
        )
        variables = {"mediaId": media_id}
        if media_id in self.entry_ids:
            variables["id"] = self.entry_ids[media_id]
        if "status" in changes:
            _check(isinstance(changes["status"], Status), "invalid_patch")
            variables["status"] = {v: k for k, v in _AL_STATUS.items() if k != "REPEATING"}[
                changes["status"]
            ]
        if "rewatching" in changes:
            _check(type(changes["rewatching"]) is bool, "invalid_patch")
            if changes["rewatching"]:
                variables["status"] = "REPEATING"
            else:
                _check("status" in variables, "rewatch_end_requires_status")
        for k in ("progress", "repeat", "score"):
            if k in changes:
                value = _int(
                    changes[k],
                    1 if k == "score" else 0,
                    100 if k == "score" else 1000 if k == "repeat" else 1_000_000,
                )
                variables["scoreRaw" if k == "score" else k] = value
        types = {
            "id": "Int",
            "mediaId": "Int",
            "status": "MediaListStatus",
            "progress": "Int",
            "repeat": "Int",
            "scoreRaw": "Int",
        }
        declarations = ",".join("$" + k + ":" + types[k] for k in variables)
        arguments = ",".join(k + ":$" + k for k in variables)
        row = self._graphql(
            token,
            "mutation("
            + declarations
            + "){SaveMediaListEntry("
            + arguments
            + "){"
            + _AL_FIELDS
            + "}}",
            variables,
            mutating=True,
        ).get("SaveMediaListEntry")
        entry = self._remember(row)
        _check(entry.media_id == media_id, "mapping_mismatch")
        return entry
