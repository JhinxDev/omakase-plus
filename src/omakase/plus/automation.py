"""Download automation — search nyaa.si and add to Real-Debrid.

Orchestrates the pipeline:
  Plan on AniList → Search nyaa.si for best magnet → Add to Real-Debrid

Called from the ``POST /plus/api/plan-and-download`` route.
"""

from __future__ import annotations

import re
import uuid

from omakase.plus.acquisition_policy import load_user_acquisition_policy
from omakase.plus.nyaa import NyaaTorrent, rank_best, search
from omakase.plus.realdebrid import RealDebridClient, RealDebridProviderBlock
from omakase.plus.secrets import read_secret

MAX_RD_CANDIDATES = 5
MAX_ATTEMPT_DETAIL_CHARS = 240

_BTIH_RE = re.compile(r"urn:btih:([^&]+)", re.IGNORECASE)


def _clean_attempt_detail(detail: str | None) -> str:
    cleaned = " ".join(str(detail or "").split())
    if len(cleaned) <= MAX_ATTEMPT_DETAIL_CHARS:
        return cleaned
    return cleaned[: MAX_ATTEMPT_DETAIL_CHARS - 3] + "..."


def _torrent_hash(magnet: str) -> str:
    match = _BTIH_RE.search(magnet)
    if not match:
        return ""
    return match.group(1).upper()


def _record_download_attempt(
    db,
    *,
    user_id: int,
    planning_id: int | None,
    request_id: str,
    candidate_rank: int,
    total_candidates: int,
    candidate: NyaaTorrent,
    status: str,
    http_status: int | None = None,
    error_code: str = "",
    detail: str = "",
    rd_torrent_id: str = "",
) -> None:
    if db is None or planning_id is None:
        return

    db.execute(
        """INSERT INTO download_attempts
           (user_id, anilist_planning_id, request_id, candidate_rank, total_candidates,
            torrent_title, torrent_hash, seeders, size_display, is_batch, status,
            http_status, error_code, detail, rd_torrent_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            planning_id,
            request_id,
            candidate_rank,
            total_candidates,
            candidate.title,
            _torrent_hash(candidate.magnet),
            candidate.seeders,
            candidate.size_display,
            1 if candidate.is_batch else 0,
            status,
            http_status,
            error_code,
            _clean_attempt_detail(detail),
            rd_torrent_id,
        ),
    )
    db.commit()


async def search_and_download(
    db,
    user_id: int,
    title: str,
    planning_id: int | None = None,
    search_titles: list[str] | None = None,
) -> dict:
    """Search nyaa.si for an anime title and add the best torrent to Real-Debrid.

    Returns a status dict suitable for JSON response:
      {"status": "ok", "rd_id": "...", "magnet": "...", "torrent_title": "..."}
      {"status": "not_found", "detail": "No torrents found on nyaa.si"}
      {"status": "rd_error", "detail": "..."}
      {"status": "no_rd_key", "detail": "Real-Debrid API key not configured"}
    """
    # 1. Read Real-Debrid API key from stored secrets
    rd_key = read_secret(db, user_id, "realdebrid_api_key")
    if not rd_key:
        return {"status": "no_rd_key", "detail": "Real-Debrid API key not configured"}

    # 2. Search nyaa.si. Direct requests may provide AniList title aliases
    # (English/Romaji/native); try them in order before giving up.
    aliases: list[str] = []
    for value in search_titles or [title]:
        alias = str(value or "").strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    if title not in aliases:
        aliases.append(title)

    candidates = []
    saw_results = False
    policy = load_user_acquisition_policy(db, user_id)
    for alias in aliases:
        results = await search(alias, trusted_only=False)
        if not results:
            continue
        saw_results = True
        ranked = rank_best(results, expected_title=alias, policy=policy)
        if ranked:
            candidates = ranked
            break

    if not saw_results:
        return {"status": "not_found", "detail": f'No torrents found for "{title}" on nyaa.si'}

    if not candidates:
        return {"status": "not_found", "detail": f'No seedable torrents found for "{title}"'}
    batch_candidates = [candidate for candidate in candidates if candidate.is_batch]
    if batch_candidates:
        candidates = batch_candidates

    # 3. Add magnets to Real-Debrid, falling back when a specific hash is rejected.
    client = RealDebridClient(rd_key)
    request_id = uuid.uuid4().hex
    last_failed_rd_id: str | None = None
    last_failure_detail: str | None = None
    provider_blocks: list[RealDebridProviderBlock] = []
    non_provider_failures = 0
    attempted = 0

    total_candidates = len(candidates)
    for candidate_rank, candidate in enumerate(candidates[:MAX_RD_CANDIDATES], start=1):
        attempted += 1
        try:
            rd_id = await client.add_magnet(candidate.magnet)
        except RealDebridProviderBlock as exc:
            provider_blocks.append(exc)
            last_failure_detail = (
                f"Real-Debrid provider blocked a candidate "
                f"({exc.http_status} {exc.error_code}): {exc.detail}"
            )
            _record_download_attempt(
                db,
                user_id=user_id,
                planning_id=planning_id,
                request_id=request_id,
                candidate_rank=candidate_rank,
                total_candidates=total_candidates,
                candidate=candidate,
                status="provider_block",
                http_status=exc.http_status,
                error_code=exc.error_code,
                detail=exc.detail,
            )
            continue
        if rd_id is None:
            non_provider_failures += 1
            add_failure_detail = "Real-Debrid add_magnet returned no torrent id"
            _record_download_attempt(
                db,
                user_id=user_id,
                planning_id=planning_id,
                request_id=request_id,
                candidate_rank=candidate_rank,
                total_candidates=total_candidates,
                candidate=candidate,
                status="rd_add_failed",
                detail=add_failure_detail,
            )
            continue

        # 4. Select all files to start download
        files_selected = await client.select_files(rd_id)
        if not files_selected:
            non_provider_failures += 1
            last_failed_rd_id = rd_id
            last_failure_detail = "Real-Debrid failed to select files for download"
            _record_download_attempt(
                db,
                user_id=user_id,
                planning_id=planning_id,
                request_id=request_id,
                candidate_rank=candidate_rank,
                total_candidates=total_candidates,
                candidate=candidate,
                status="select_failed",
                detail=last_failure_detail,
                rd_torrent_id=rd_id,
            )
            # The media-orchestrator safety contract forbids eager RD cleanup.
            # This created item remains until two stalled observations and a
            # usable recorded replacement authorize the lease-held executor.
            continue

        _record_download_attempt(
            db,
            user_id=user_id,
            planning_id=planning_id,
            request_id=request_id,
            candidate_rank=candidate_rank,
            total_candidates=total_candidates,
            candidate=candidate,
            status="selected",
            detail="Real-Debrid selected all files",
            rd_torrent_id=rd_id,
        )
        return {
            "status": "ok",
            "rd_id": rd_id,
            "request_id": request_id,
            "magnet": candidate.magnet,
            "torrent_title": candidate.title,
            "seeders": candidate.seeders,
            "size": candidate.size_display,
        }

    if provider_blocks and non_provider_failures == 0:
        last_block = provider_blocks[-1]
        plural = "candidate" if len(provider_blocks) == 1 else "candidates"
        return {
            "status": "rd_provider_block",
            "http_status": last_block.http_status,
            "error_code": last_block.error_code,
            "request_id": request_id,
            "detail": (
                f'Real-Debrid provider blocked {len(provider_blocks)} {plural} for "{title}" '
                f"({attempted} of {len(candidates)} ranked candidates attempted); "
                f"last RD response: {last_block.http_status} {last_block.error_code} - "
                f"{last_block.detail}"
            ),
        }

    response = {
        "status": "rd_error",
        "request_id": request_id,
        "detail": (
            f'Real-Debrid rejected or failed all candidate torrents tried for "{title}" '
            f"({attempted} of {len(candidates)} ranked candidates attempted)"
        ),
    }
    if last_failure_detail is not None:
        response["detail"] = f"{response['detail']}; last failure: {last_failure_detail}"
    elif provider_blocks:
        last_block = provider_blocks[-1]
        response["detail"] = (
            f"{response['detail']}; provider blocked {len(provider_blocks)} candidate"
            f"{'' if len(provider_blocks) == 1 else 's'}; "
            f"last RD response: {last_block.http_status} {last_block.error_code} - "
            f"{last_block.detail}"
        )
    if last_failed_rd_id is not None:
        response["rd_id"] = last_failed_rd_id
    return response
