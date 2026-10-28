"""AniList OAuth (Authorization Code with PKCE) and GraphQL write operations."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlencode

import httpx

API_URL = "https://graphql.anilist.co"
OAUTH_URL = "https://anilist.co/api/v2/oauth"
USER_AGENT = "Omakase/0.1 (+https://github.com/HeyiTzSenpai/omakase)"


class AniListWriteError(ValueError):
    """AniList accepted the request transport but refused the list mutation."""


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge.

    Returns ``(code_verifier, code_challenge)`` where *code_challenge* is
    ``SHA256(code_verifier)`` base64url-encoded without padding.
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> str:
    """Build the AniList OAuth authorization URL with PKCE parameters."""
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{OAUTH_URL}/authorize?{params}"


def exchange_code(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> str:
    """Exchange an authorization code for an access token.

    POSTs ``application/x-www-form-urlencoded`` data to the AniList OAuth
    token endpoint.  Returns the ``access_token`` string.
    """
    with httpx.Client() as client:
        resp = client.post(
            f"{OAUTH_URL}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def add_to_planning(access_token: str, anilist_id: int, status: str = "PLANNING") -> dict:
    """Add an anime to the authenticated user's AniList planning list.

    Uses the ``SaveMediaListEntry`` GraphQL mutation.  Returns the full
    JSON response as a dict.
    """
    mutation = """
    mutation ($mediaId: Int, $status: MediaListStatus) {
      SaveMediaListEntry(mediaId: $mediaId, status: $status) {
        id
        mediaId
        status
      }
    }
    """
    with httpx.Client() as client:
        resp = client.post(
            API_URL,
            json={
                "query": mutation,
                "variables": {"mediaId": anilist_id, "status": status},
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        resp.raise_for_status()
        return resp.json()


def ensure_in_planning(access_token: str, anilist_id: int) -> dict:
    """Create a PLANNING entry only when the authenticated user has no entry.

    AniList's save mutation updates an existing row in place. Reading the
    owner-scoped row first prevents acquisition from downgrading CURRENT,
    COMPLETED, PAUSED, DROPPED, or REPEATING user state.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client() as client:
        viewer_response = client.post(
            API_URL,
            json={"query": "query { Viewer { id } }"},
            headers=headers,
        )
        viewer_response.raise_for_status()
        viewer_payload = viewer_response.json()
        viewer_data = viewer_payload.get("data") if isinstance(viewer_payload, dict) else None
        viewer = viewer_data.get("Viewer") if isinstance(viewer_data, dict) else None
        if not isinstance(viewer, dict) or not isinstance(viewer.get("id"), int):
            raise AniListWriteError("AniList did not identify the authenticated user")

        list_response = client.post(
            API_URL,
            json={
                "query": """
                query ($mediaId: Int, $userId: Int) {
                  MediaList(mediaId: $mediaId, userId: $userId) {
                    id
                    mediaId
                    status
                    score
                    progress
                  }
                }
                """,
                "variables": {"mediaId": anilist_id, "userId": viewer["id"]},
            },
            headers=headers,
        )
        list_response.raise_for_status()
        list_payload = list_response.json()
        if not isinstance(list_payload, dict):
            raise AniListWriteError("AniList returned an invalid list response")
        errors = list_payload.get("errors")
        if errors:
            message = "AniList could not verify the existing list entry"
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                candidate = errors[0].get("message")
                if isinstance(candidate, str) and candidate.strip():
                    message = candidate.strip()[:200]
            raise AniListWriteError(message)
        list_data = list_payload.get("data")
        if not isinstance(list_data, dict) or "MediaList" not in list_data:
            raise AniListWriteError("AniList returned an invalid list response")
        entry = list_data["MediaList"]
        if entry is not None and not isinstance(entry, dict):
            raise AniListWriteError("AniList returned an invalid list response")
    known_statuses = {
        "CURRENT",
        "PLANNING",
        "COMPLETED",
        "DROPPED",
        "PAUSED",
        "REPEATING",
    }
    if isinstance(entry, dict):
        if entry.get("mediaId") != anilist_id or entry.get("status") not in known_statuses:
            raise AniListWriteError("AniList returned an invalid list response")
        return {"created": False, "entry": entry}

    payload = add_to_planning(access_token, anilist_id, "PLANNING")
    payload_data = payload.get("data") if isinstance(payload, dict) else None
    created = payload_data.get("SaveMediaListEntry") if isinstance(payload_data, dict) else None
    if (
        not isinstance(created, dict)
        or created.get("mediaId") != anilist_id
        or created.get("status") != "PLANNING"
    ):
        raise AniListWriteError("AniList refused the planning entry")
    return {"created": True, "entry": created}


def save_completed_entry(
    access_token: str,
    anilist_id: int,
    *,
    score_ten: float | None = None,
) -> dict:
    """Create or update a COMPLETED AniList entry with an optional 1–10 score.

    ``scoreRaw`` is used so Omakase's ten-point control remains consistent
    regardless of the user's chosen AniList score display format.
    """
    if isinstance(anilist_id, bool) or not isinstance(anilist_id, int) or anilist_id <= 0:
        raise ValueError("anilist_id must be a positive integer")
    score_raw = None
    if score_ten is not None:
        if isinstance(score_ten, bool) or not isinstance(score_ten, (int, float)):
            raise ValueError("score must be a number from 1 to 10")
        normalized = float(score_ten)
        if not 1 <= normalized <= 10:
            raise ValueError("score must be a number from 1 to 10")
        score_raw = int(round(normalized * 10))
    variables = {"mediaId": anilist_id, "status": "COMPLETED"}
    score_parameter = ", $scoreRaw: Int" if score_raw is not None else ""
    score_argument = ", scoreRaw: $scoreRaw" if score_raw is not None else ""
    if score_raw is not None:
        variables["scoreRaw"] = score_raw
    mutation = f"""
    mutation ($mediaId: Int, $status: MediaListStatus{score_parameter}) {{
      SaveMediaListEntry(mediaId: $mediaId, status: $status{score_argument}) {{
        id
        mediaId
        status
      }}
    }}
    """
    with httpx.Client() as client:
        response = client.post(
            API_URL,
            json={
                "query": mutation,
                "variables": variables,
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        response.raise_for_status()
        payload = response.json()
    errors = payload.get("errors") if isinstance(payload, dict) else None
    entry = (
        (payload.get("data") or {}).get("SaveMediaListEntry") if isinstance(payload, dict) else None
    )
    if errors or not isinstance(entry, dict):
        message = "AniList refused the list update"
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            candidate = errors[0].get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()[:200]
        raise AniListWriteError(message)
    if (
        entry.get("mediaId") != anilist_id
        or entry.get("status") != "COMPLETED"
        or isinstance(entry.get("id"), bool)
        or not isinstance(entry.get("id"), int)
        or entry["id"] <= 0
    ):
        raise AniListWriteError("AniList returned an invalid completion receipt")
    return entry


def save_progress_entry(
    access_token: str,
    anilist_id: int,
    *,
    progress: int,
) -> dict:
    """Set exact episode progress for the authenticated user's AniList entry."""
    if isinstance(anilist_id, bool) or not isinstance(anilist_id, int) or anilist_id <= 0:
        raise ValueError("anilist_id must be a positive integer")
    if isinstance(progress, bool) or not isinstance(progress, int) or progress < 0:
        raise ValueError("progress must be a non-negative integer")
    mutation = """
    mutation ($mediaId: Int, $status: MediaListStatus, $progress: Int) {
      SaveMediaListEntry(mediaId: $mediaId, status: $status, progress: $progress) {
        id
        mediaId
        status
        progress
      }
    }
    """
    with httpx.Client() as client:
        response = client.post(
            API_URL,
            json={
                "query": mutation,
                "variables": {
                    "mediaId": anilist_id,
                    "status": "CURRENT",
                    "progress": progress,
                },
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        response.raise_for_status()
        payload = response.json()
    errors = payload.get("errors") if isinstance(payload, dict) else None
    entry = (
        (payload.get("data") or {}).get("SaveMediaListEntry") if isinstance(payload, dict) else None
    )
    if errors or not isinstance(entry, dict):
        message = "AniList refused the progress update"
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            candidate = errors[0].get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()[:200]
        raise AniListWriteError(message)
    if (
        isinstance(entry.get("id"), bool)
        or not isinstance(entry.get("id"), int)
        or entry["id"] <= 0
        or entry.get("mediaId") != anilist_id
        or entry.get("status") != "CURRENT"
        or entry.get("progress") != progress
    ):
        raise AniListWriteError("AniList returned an invalid progress receipt")
    return entry


def get_authenticated_media_entry(access_token: str, anilist_id: int) -> dict | None:
    """Read one media-list row scoped to the bearer token's Viewer identity."""
    if isinstance(anilist_id, bool) or not isinstance(anilist_id, int) or anilist_id <= 0:
        raise ValueError("anilist_id must be a positive integer")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    with httpx.Client() as client:
        viewer_response = client.post(
            API_URL,
            json={"query": "query { Viewer { id } }"},
            headers=headers,
        )
        viewer_response.raise_for_status()
        viewer_payload = viewer_response.json()
        viewer_data = viewer_payload.get("data") if isinstance(viewer_payload, dict) else None
        viewer = viewer_data.get("Viewer") if isinstance(viewer_data, dict) else None
        if (
            not isinstance(viewer, dict)
            or isinstance(viewer.get("id"), bool)
            or not isinstance(viewer.get("id"), int)
            or viewer["id"] <= 0
        ):
            raise AniListWriteError("AniList did not identify the authenticated user")

        list_response = client.post(
            API_URL,
            json={
                "query": """
                query ($mediaId: Int, $userId: Int) {
                  MediaList(mediaId: $mediaId, userId: $userId) {
                    id
                    mediaId
                    status
                    progress
                  }
                }
                """,
                "variables": {"mediaId": anilist_id, "userId": viewer["id"]},
            },
            headers=headers,
        )
        list_response.raise_for_status()
        payload = list_response.json()
    if not isinstance(payload, dict):
        raise AniListWriteError("AniList returned an invalid list response")
    errors = payload.get("errors")
    if errors:
        message = "AniList could not read the existing list entry"
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            candidate = errors[0].get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()[:200]
        raise AniListWriteError(message)
    data = payload.get("data")
    if not isinstance(data, dict) or "MediaList" not in data:
        raise AniListWriteError("AniList returned an invalid list response")
    entry = data["MediaList"]
    if entry is None:
        return None
    known_statuses = {
        "CURRENT",
        "PLANNING",
        "COMPLETED",
        "DROPPED",
        "PAUSED",
        "REPEATING",
    }
    if (
        not isinstance(entry, dict)
        or isinstance(entry.get("id"), bool)
        or not isinstance(entry.get("id"), int)
        or entry["id"] <= 0
        or entry.get("mediaId") != anilist_id
        or entry.get("status") not in known_statuses
        or isinstance(entry.get("progress"), bool)
        or not isinstance(entry.get("progress"), int)
        or entry["progress"] < 0
    ):
        raise AniListWriteError("AniList returned an invalid list response")
    return entry


def is_token_valid(access_token: str) -> bool:
    """Quickly check whether the AniList access token is still valid.

    Queries the ``Viewer`` endpoint.  Returns True on HTTP 200, False on
    401 or any transport error.
    """
    query = "query { Viewer { id } }"
    try:
        with httpx.Client() as client:
            resp = client.post(
                API_URL,
                json={"query": query},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def remove_from_planning(access_token: str, anilist_id: int) -> bool:
    """Remove an anime from the authenticated user's AniList list.

    Finds the list entry for the given media ID, then deletes it.
    Returns True if the entry was found and deleted, False if not found.
    """
    # Resolve the authenticated identity first. ``MediaList(mediaId: ...)`` is
    # not implicitly scoped to the bearer token and can return another user's
    # public entry.
    viewer_query = "query { Viewer { id } }"
    with httpx.Client() as client:
        resp = client.post(
            API_URL,
            json={"query": viewer_query},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        if resp.status_code != 200:
            return False
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        viewer = data.get("Viewer") if isinstance(data, dict) else None
        if not isinstance(viewer, dict) or not isinstance(viewer.get("id"), int):
            return False
        viewer_id = viewer["id"]

    # Then find only that user's list entry ID.
    find_query = """
    query ($mediaId: Int, $userId: Int) {
      MediaList(mediaId: $mediaId, userId: $userId) {
        id
        status
      }
    }
    """
    with httpx.Client() as client:
        resp = client.post(
            API_URL,
            json={
                "query": find_query,
                "variables": {"mediaId": anilist_id, "userId": viewer_id},
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        payload = data.get("data") if isinstance(data, dict) else None
        entry = payload.get("MediaList") if isinstance(payload, dict) else None
        if (
            not isinstance(entry, dict)
            or entry.get("status") != "PLANNING"
            or not isinstance(entry.get("id"), int)
        ):
            return False
        entry_id = entry["id"]

    # Delete the entry
    delete_mutation = """
    mutation ($id: Int) {
      DeleteMediaListEntry(id: $id) {
        deleted
      }
    }
    """
    with httpx.Client() as client:
        resp = client.post(
            API_URL,
            json={"query": delete_mutation, "variables": {"id": entry_id}},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return (result.get("data") or {}).get("DeleteMediaListEntry", {}).get("deleted", False)


@contextmanager
def with_valid_token(
    db,
    user_id: int,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Iterator[str]:
    """Context manager that yields the user's valid AniList OAuth token.

    Reads the token from the secrets store and runs a live validity check
    against AniList's ``Viewer`` endpoint.  Raises ``ValueError`` if the
    token is missing or invalid.

    *client_id*, *client_secret*, and *redirect_uri* are accepted for
    forward compatibility.  AniList OAuth tokens are long-lived and do not
    support refresh tokens, so these parameters are unused today.
    """
    from omakase.plus.secrets import read_secret

    token = read_secret(db, user_id, "anilist_oauth_token")
    if token is None:
        raise ValueError("No AniList OAuth token found. Please connect AniList first.")
    if not is_token_valid(token):
        raise ValueError(
            "AniList OAuth token is invalid or expired. Please disconnect and reconnect AniList."
        )
    yield token
