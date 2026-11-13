"""Orchestrator: runs the full recommendation pipeline."""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from urllib.parse import quote_plus

from omakase.adapters.base import get_adapter
from omakase.llm import get_llm
from omakase.prompt import build_prompt
from omakase.types import MediaItem, OmakaseConfig, Recommendation, SourceData


class EmptyHistoryError(Exception):
    """The source adapter returned no history for this username.

    Raised when the username is wrong / the list is empty / the list is private —
    cases the visitor can fix by changing input, not retrying.
    """


class LLMOutputParseError(Exception):
    """The LLM's response couldn't be parsed into recommendations.

    Almost always a max-tokens truncation that cut the JSON mid-string;
    less often, a valid-JSON-but-wrong-shape response. Either way the
    caller surfaces an explicit error so the user sees what happened
    instead of a silent zero-pick run landing in the dashboard.
    """


_EXCLUSION_FEEDBACK = frozenset({"already_watched", "not_for_me", "wrong_sequel"})


def _feedback_exclusions(cfg: OmakaseConfig) -> tuple[set[int], set[str]]:
    media_ids = {
        item.media_id
        for item in cfg.feedback
        if item.feedback_type in _EXCLUSION_FEEDBACK and item.media_id is not None
    }
    titles = {
        _normalize_title(item.title)
        for item in cfg.feedback
        if item.feedback_type in _EXCLUSION_FEEDBACK and item.title.strip()
    }
    return media_ids, titles


def _normalize_title(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _candidate_is_excluded(item: MediaItem, ids: set[int], titles: set[str]) -> bool:
    return item.id in ids or any(
        _normalize_title(title) in titles
        for title in (item.title_english, item.title_romaji)
        if title
    )


def _recommendation_is_excluded(
    item: Recommendation,
    ids: set[int],
    titles: set[str],
) -> bool:
    return (
        (item.media_id is not None and item.media_id in ids)
        or (item.anilist_id is not None and item.anilist_id in ids)
        or _normalize_title(item.title) in titles
    )


def _parse_recommendations(raw: str) -> list[Recommendation]:
    """Parse LLM JSON output into Recommendation objects.

    Raises ``LLMOutputParseError`` on a malformed response. Stderr lines
    stay for CLI users and `docker logs` — the exception is additional
    signal for the web layer, not a replacement for log diagnostics.
    """
    cleaned = raw.strip()
    if not cleaned:
        print("[!] LLM returned empty final content", file=sys.stderr)
        raise LLMOutputParseError(
            "LLM returned an empty final answer. With reasoning models this usually "
            "means the model spent the output budget on reasoning_content and never "
            "reached the JSON answer; try again, use Fast mode, or use a smaller Count."
        )

    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        end = cleaned.find("\n")
        if end != -1:
            cleaned = cleaned[end + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

    # Try to find a JSON object in the response
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        if start != -1:
            cleaned = cleaned[start:]
    if not cleaned.endswith("}"):
        end = cleaned.rfind("}")
        if end != -1:
            cleaned = cleaned[: end + 1]

    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict) or not isinstance(data.get("recommendations"), list):
            raise ValueError("Expected a recommendations array")
        recs = data["recommendations"]
        for item in recs:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                raise ValueError("Each recommendation needs a text title")
            score = item.get("predicted_score", 0)
            if isinstance(score, bool) or not math.isfinite(float(score)):
                raise ValueError("Predicted scores must be finite numbers")
            if not 0 <= float(score) <= 10:
                raise ValueError("Predicted scores must be between 1 and 10")
            for field in ("reasoning", "best_match_from_history"):
                if field in item and not isinstance(item[field], str):
                    raise ValueError(f"{field} must be text")
        return [
            Recommendation(
                title=r.get("title", "Unknown"),
                predicted_score=float(r.get("predicted_score", 0)),
                reasoning=r.get("reasoning", ""),
                best_match_from_history=r.get("best_match_from_history", ""),
                anilist_id=r.get("anilist_id"),
                media_id=r.get("media_id"),
                airing_status=r.get("airing_status"),
                franchise_note=r.get("franchise_note"),
                sequence_warning=r.get("sequence_warning"),
                lane_reason=r.get("lane_reason"),
            )
            for r in recs
        ]
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError) as e:
        print(f"[!] Failed to parse LLM output: {e}", file=sys.stderr)
        print(f"Raw output: {raw[:500]}", file=sys.stderr)
        raise LLMOutputParseError(
            f"LLM output couldn't be parsed ({e}). Most often this means the "
            "response was truncated by a max-tokens cap — try a smaller Count "
            "or a less verbose model."
        ) from e


def _resolve_rec_urls(
    recs: list[Recommendation],
    candidates: list[MediaItem],
    source_name: str,
) -> None:
    """Set each Recommendation's `url` + `source` so the UI can link out.

    Resolve a unique normalized English/romaji alias or a verified pool ID.
    Canonical source metadata overrides all model-supplied identity claims.
    Ambiguous titles require a matching real ID. Search fallback is retained for
    standalone callers; run() rejects those unresolved recommendations.
    """
    if not recs:
        return

    # Titles are aliases only when unambiguous. Never fuzzy-match sequel names.
    lookup: dict[str, list[MediaItem]] = {}
    by_id = {item.id: item for item in candidates}
    for candidate in candidates:
        for title in (candidate.title_english, candidate.title_romaji):
            if title:
                matches = lookup.setdefault(_normalize_title(title), [])
                if all(item.id != candidate.id for item in matches):
                    matches.append(candidate)

    def permalink(item: MediaItem) -> str | None:
        if source_name == "anilist":
            return f"https://anilist.co/anime/{item.id}/"
        if source_name == "myanimelist":
            return f"https://myanimelist.net/anime/{item.id}/"
        return None

    def search_url(title: str) -> str:
        q = quote_plus(title.strip())
        if source_name == "myanimelist":
            return f"https://myanimelist.net/anime.php?q={q}&cat=anime"
        # Default to AniList search — covers the "anilist" name and any
        # future sources that don't ship a dedicated search URL.
        return f"https://anilist.co/search/anime?search={q}"

    for r in recs:
        r.source = source_name
        title_matches = lookup.get(_normalize_title(r.title), [])
        supplied_id = (
            r.media_id
            if r.media_id is not None
            else (r.anilist_id if source_name == "anilist" else None)
        )
        id_match = by_id.get(supplied_id) if type(supplied_id) is int else None
        match = title_matches[0] if len(title_matches) == 1 else None
        if len(title_matches) > 1:
            match = id_match if id_match in title_matches else None
        elif not title_matches:
            match = id_match
        if match:
            r.title = match.title_english or match.title_romaji
            r.media_id = match.id
            r.anilist_id = match.id if source_name == "anilist" else None
            r.airing_status = match.status
            r.episodes = match.episodes
            r.description = match.description
            r.genres = list(match.genres)
            r.format = match.format
            r.studio = match.studio
            r.franchise_note = match.franchise_note or None
            r.sequence_warning = match.sequence_warning or None
        else:
            r.media_id = None
            r.anilist_id = None
            r.airing_status = None
            r.episodes = None
            r.description = None
            r.genres = []
            r.format = None
            r.studio = None
            r.franchise_note = None
            r.sequence_warning = None
        link = permalink(match) if match else None
        r.url = link or search_url(r.title)


def _fits_discovery_constraints(item: MediaItem, cfg: OmakaseConfig) -> bool:
    if cfg.completed_only and item.status != "FINISHED":
        return False
    if cfg.max_episodes > 0 and (
        item.episodes is None or item.episodes <= 0 or item.episodes > cfg.max_episodes
    ):
        return False
    return True


def _rank_candidates(
    history: list[MediaItem], candidates: list[MediaItem], lane: str = "best_match"
) -> list[MediaItem]:
    """Balance positive and negative evidence, with a modest diversity tie-breaker.

    Sparse genre matches are evidence, not predictions of a personal score. Keep
    the complete eligible pool for the model rather than excluding whole genres.
    """
    evidence: dict[str, list[float]] = {}

    def traits(item: MediaItem) -> set[str]:
        return {*(f"genre:{g}" for g in item.genres), *(f"tag:{t}" for t in item.tags)}

    for item in history:
        weight = 0.0
        if item.score is not None and math.isfinite(item.score) and item.score > 0:
            weight = (item.score - 6.5) / 3.5
        if item.status == "DROPPED":
            weight = min(weight, -0.7)
        # Paused is often a time constraint, not a reliable dislike signal.
        if weight:
            for trait in traits(item):
                evidence.setdefault(trait, []).append(weight)
    affinities = {key: sum(values) / (len(values) + 2) for key, values in evidence.items()}

    def relevance(item: MediaItem) -> float:
        values = [affinities.get(trait, 0) for trait in traits(item)]
        fit = sum(values) / max(1, len(values))
        quality = (item.mean_score or 70) / 100 * 0.15
        continuation = (
            (0.5 if lane == "new_seasons" else 0.25) if item.franchise_policy == "boosted" else 0
        )
        if lane == "hidden_gems":
            continuation = 0
        if lane == "new_seasons" and item.status == "RELEASING":
            continuation += 0.1
        return fit + quality + continuation

    remaining = list({item.id: item for item in candidates}.values())
    result: list[MediaItem] = []
    represented: dict[str, int] = {}
    while remaining:
        item = max(
            remaining,
            key=lambda candidate: (
                relevance(candidate) - 0.035 * sum(represented.get(g, 0) for g in candidate.genres)
            ),
        )
        remaining.remove(item)
        result.append(item)
        for genre in item.genres:
            represented[genre] = represented.get(genre, 0) + 1
    return result


def _load_taste_profile(path: str) -> str:
    """Load taste profile from a markdown file.

    An empty `path` means "no profile" — caller has opted into broader,
    score-only recommendations. Returns an empty string in that case so
    the prompt builder takes its no-profile branch.
    """
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"[!] Taste profile not found at: {path}", file=sys.stderr)
        print("   Create one or use --profile to point to your profile file.", file=sys.stderr)
        sys.exit(1)


def run(cfg: OmakaseConfig) -> list[Recommendation]:
    """Run the full recommendation pipeline and return recommendations."""
    # 1. Fetch data from source
    src_desc = (
        f"uploaded export ({len(cfg.export_data)} bytes)"
        if cfg.export_data
        else f"'{cfg.username}'"
    )
    print(f"  [1/5] Fetching data from {cfg.source} for {src_desc}...")
    adapter = get_adapter(cfg.source)
    data: SourceData = adapter.fetch(
        cfg.username,
        cfg.candidate_pool_size,
        use_planning=cfg.use_planning,
        export_data=cfg.export_data,
        recommendation_lane=cfg.recommendation_lane,
        mal_client_id=cfg.mal_client_id,
    )

    if not data.history:
        if cfg.export_data:
            raise EmptyHistoryError(
                "The uploaded export had no scored anime entries. "
                "Make sure you exported the Anime list (not Manga) and that it "
                "contains rated titles."
            )
        raise EmptyHistoryError(
            f"No anime history found for '{cfg.username}' on {cfg.source}. "
            "Double-check the username (it's case-sensitive on some sources) "
            "and make sure the list is public."
        )
    excluded_ids, excluded_titles = _feedback_exclusions(cfg)
    excluded_ids.update(item.id for item in data.history if item.status != "PLANNING")
    excluded_titles.update(
        _normalize_title(title)
        for item in data.history
        if item.status != "PLANNING"
        for title in (item.title_english, item.title_romaji)
        if title
    )
    data.candidates = [
        item
        for item in data.candidates
        if not _candidate_is_excluded(item, excluded_ids, excluded_titles)
        and _fits_discovery_constraints(item, cfg)
    ]
    data.candidates = _rank_candidates(data.history, data.candidates, cfg.recommendation_lane)
    if not data.candidates:
        if excluded_ids or excluded_titles or cfg.max_episodes > 0 or cfg.completed_only:
            raise EmptyHistoryError(
                "No unseen candidates remain after applying your local watched "
                "and recommendation feedback or discovery constraints. Try relaxing the episode "
                "limit or finished-only filter."
            )
        if cfg.use_planning:
            raise EmptyHistoryError(
                f"Your {cfg.source} Plan-to-Watch list is empty. "
                "Add some titles to it, or uncheck 'Recommend from my Plan to Watch'."
            )
        raise EmptyHistoryError(
            "Couldn't find candidate anime to recommend from. "
            "This usually means the source is down — try again in a moment."
        )

    loved = sum(1 for m in data.history if m.score and m.score >= 9)
    liked = sum(1 for m in data.history if m.score and 7 <= m.score < 9)
    print(f"        History: {len(data.history)} entries ({loved} loved, {liked} liked)")
    source_label = "plan-to-watch" if cfg.use_planning else "popular"
    print(f"        Candidates: {len(data.candidates)} ({source_label})")

    # 2. Load taste profile (may be intentionally empty for broader recs)
    if cfg.profile_path:
        print(f"  [2/5] Loading taste profile from {cfg.profile_path}...")
    else:
        print("  [2/5] Skipping taste profile (broader recs from scores alone)...")
    taste_profile = _load_taste_profile(cfg.profile_path)
    if taste_profile:
        print(f"        Profile: {len(taste_profile.split())} words")
    else:
        print("        Profile: (none — inferring taste from scoring history)")

    # 3. Build prompt
    print(f"  [3/5] Building prompt for {cfg.model}...")
    prompt = build_prompt(
        taste_profile,
        [item for item in data.history if item.status != "PLANNING"],
        data.candidates,
        n_recs=cfg.num_recommendations,
        lane=cfg.recommendation_lane,
        feedback=cfg.feedback,
        mood=cfg.mood,
        max_episodes=cfg.max_episodes,
        completed_only=cfg.completed_only,
    )
    print(f"        Prompt: ~{len(prompt.split())} tokens")

    # 4. Send to LLM
    mode_label = "PRO" if cfg.mode == "pro" else "fast"
    print(f"  [4/5] Querying LLM ({cfg.llm_type}: {cfg.model}) [{mode_label}]...")
    llm = (
        get_llm(cfg.llm_type, cfg.llm_url, cfg.model, api_key=cfg.api_key)
        if cfg.api_key
        else get_llm(cfg.llm_type, cfg.llm_url, cfg.model)
    )
    raw = llm.generate(
        prompt,
        temperature=cfg.temperature,
        num_ctx=cfg.num_ctx,
        supports_json=cfg.supports_json_mode,
    )

    # 5. Parse
    print("  [5/5] Parsing response...")
    recs = _parse_recommendations(raw)
    _resolve_rec_urls(recs, data.candidates, data.source_name)
    if excluded_ids or excluded_titles:
        recs = [
            item
            for item in recs
            if not _recommendation_is_excluded(item, excluded_ids, excluded_titles)
        ]
    # Grounding is a hard gate: model claims never expand the eligible pool.
    eligible = {item.id: item for item in data.candidates}
    seen: set[int] = set()
    grounded: list[Recommendation] = []
    for item in recs:
        candidate = eligible.get(item.media_id)
        if (
            candidate is None
            or candidate.id in seen
            or not _fits_discovery_constraints(candidate, cfg)
            or not math.isfinite(item.predicted_score)
            or not 1 <= item.predicted_score <= 10
        ):
            continue
        seen.add(candidate.id)
        grounded.append(item)
    recs = grounded[: cfg.num_recommendations]
    if not recs:
        raise LLMOutputParseError(
            "The model returned no valid picks from the eligible candidate pool. "
            "Retry the recommendation or adjust your discovery filters."
        )
    print(f"        Got {len(recs)} recommendations\n")
    return recs
