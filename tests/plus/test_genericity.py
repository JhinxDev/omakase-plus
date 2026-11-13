"""Genericity gates: the shipped source must not encode one household's data.

The product is a general library front end. Three kinds of hardcoding have caused
real defects here, and each has its own gate below.

1. Title specific strings. A release-numbering contract for one title leaks into
   catalogue, identity, storage and publication code, and then a different title
   is silently given the wrong episode numbers. Today the shipping tree already
   contains some of this debt, recorded exactly in ``TITLE_DEBT_CEILING``. The
   gate is a ratchet: existing mentions are allowed at their measured count and
   one new mention anywhere fails the build, including inside a file that is
   already listed. Remove the debt and delete the entry.
2. Provider hostnames. A provider domain belongs in the module that owns that
   provider. Today 15 module and domain pairs predate the rule and are
   grandfathered in ``DECLARED_PROVIDER_DOMAINS`` with the reason for each. A new
   hardcoded hostname in any other module fails.
3. Provider ids. Playback and storage branching is allowed only in the modules
   listed in ``PROVIDER_ID_MODULES``, and only for ids that omakase declares in
   ``media_store._PROVIDER_KINDS`` or ``debrid_library.PLAYBACK_PROVIDERS``. A
   comparison against a provider that the app has not declared fails.

The detectors are pure functions over a mapping of relative path to text, so the
positive control tests at the bottom of this module prove that each rule still
fires on a synthetic violation. A gate that cannot fail is not a gate.

Scope: ``src`` only, and only shipping source. Third party assets are excluded by
``EXCLUDED_PATHS``. Tests and documentation may name titles and hosts freely.
"""

from __future__ import annotations

import ast
import functools
import io
import re
import tokenize
from collections.abc import Iterable, Mapping
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

SOURCE_SUFFIXES = frozenset({".py", ".html", ".css", ".js", ".md"})

EXCLUDED_PATHS = ("omakase/plus/static/ass/",)
EXCLUDED_NAMES = re.compile(r"shaka-player-[0-9.]+\.js$")

TITLE_PHRASES = (
    "bleach",
    "tybw",
    "sennen kessen",
    "thousand-year blood war",
    "slime",
    "tensei shitara",
    "reincarnated as a slime",
    "rimuru",
    "my hero academia",
    "boku no hero",
    "mha",
    "frieren",
    "food wars",
    "shokugeki",
)

TITLE_IDS = (
    "185874",
    "182205",
    "104276",
)

# Measured on 2026-11-12 against this tree. A count may fall, never rise.
# These are the release-numbering contracts and template examples that ship today.
# They are recorded so the gate can pass while the product still carries the debt;
# delete an entry when its mentions are gone, and never raise one without evidence.
TITLE_DEBT_CEILING: Mapping[tuple[str, str], int] = {
    ("omakase/plus/catalog.py", "bleach"): 4,
    ("omakase/plus/catalog.py", "sennen kessen"): 1,
    ("omakase/plus/catalog.py", "slime"): 2,
    ("omakase/plus/coverage.py", "bleach"): 9,
    ("omakase/plus/coverage.py", "mha"): 10,
    ("omakase/plus/coverage.py", "reincarnated as a slime"): 1,
    ("omakase/plus/coverage.py", "sennen kessen"): 1,
    ("omakase/plus/coverage.py", "slime"): 8,
    ("omakase/plus/coverage.py", "tensei shitara"): 1,
    ("omakase/plus/coverage.py", "tybw"): 7,
    ("omakase/plus/identity.py", "bleach"): 4,
    ("omakase/plus/identity.py", "slime"): 4,
    ("omakase/plus/manifest.py", "bleach"): 1,
    ("omakase/plus/manifest.py", "mha"): 1,
    ("omakase/plus/manifest.py", "slime"): 1,
    ("omakase/plus/manifest.py", "tybw"): 1,
    ("omakase/plus/media_store.py", "bleach"): 1,
    ("omakase/plus/media_store.py", "mha"): 1,
    ("omakase/plus/media_store.py", "slime"): 1,
    ("omakase/plus/media_store.py", "tybw"): 1,
    ("omakase/plus/publication_episodes.py", "bleach"): 9,
    ("omakase/plus/publication_episodes.py", "mha"): 2,
    ("omakase/plus/publication_episodes.py", "reincarnated as a slime"): 1,
    ("omakase/plus/publication_episodes.py", "sennen kessen"): 1,
    ("omakase/plus/publication_episodes.py", "slime"): 8,
    ("omakase/plus/publication_episodes.py", "tensei shitara"): 1,
    ("omakase/plus/publication_episodes.py", "tybw"): 6,
    ("omakase/plus/templates/dashboard.html", "my hero academia"): 1,
    ("omakase/plus/templates/discover.html", "frieren"): 1,
}

TITLE_ID_DEBT_CEILING: Mapping[tuple[str, str], int] = {
    ("omakase/plus/coverage.py", "104276"): 1,
    ("omakase/plus/coverage.py", "182205"): 1,
    ("omakase/plus/coverage.py", "185874"): 1,
    ("omakase/plus/identity.py", "104276"): 1,
    ("omakase/plus/media_sync.py", "182205"): 1,
    ("omakase/plus/media_sync.py", "185874"): 1,
    ("omakase/plus/publication_episodes.py", "182205"): 1,
    ("omakase/plus/publication_episodes.py", "185874"): 1,
}

# Providers whose domains the app may name, and the modules that own the name.
# Every entry below predates this gate. A new module needs a declared constant or
# an explicit entry with a reason; a new provider needs a declared constant first.
DECLARED_PROVIDER_DOMAINS: Mapping[str, Mapping[str, str]] = {
    "anilist.co": {
        "omakase/adapters/anilist.py": "GraphQL endpoint constant",
        "omakase/engine.py": "user facing AniList links",
        "omakase/plus/anilist.py": "GraphQL and OAuth endpoint constants",
        "omakase/plus/catalog_search.py": "GraphQL endpoint constant",
        "omakase/plus/direct.py": "GraphQL endpoint constant",
        "omakase/plus/mal.py": "GraphQL endpoint constant shared with the MAL bridge",
        "omakase/plus/posters.py": "poster CDN host allowlist",
        "omakase/plus/product_views.py": "user facing AniList link",
        "omakase/plus/templates/_discover_rows.html": "user facing AniList link",
        "omakase/plus/templates/catalog_anime.html": "user facing AniList links",
        "omakase/plus/templates/dashboard.html": "user facing AniList link",
        "omakase/plus/templates/discover.html": "user facing AniList link",
        "omakase/plus/templates/library.html": "user facing AniList link",
        "omakase/plus/templates/recommendation.html": "user facing AniList link",
        "omakase/plus/templates/title.html": "user facing AniList links",
    },
    "plex.tv": {
        "omakase/plus/household_routes.py": "return URL host allowlist for app.plex.tv",
        "omakase/plus/movies.py": "user facing Plex web link builder",
        "omakase/plus/playback_proof.py": "plex.tv account API base",
        "omakase/plus/static/movie-plex-review.js": "navigation origin allowlist for app.plex.tv",
        "omakase/plus/web_playback_source.py": "user facing Plex link",
    },
    "real-debrid.com": {
        "omakase/plus/connections.py": "default Real-Debrid API endpoint",
        "omakase/plus/realdebrid.py": "API endpoint constant plus API and token help text",
        "omakase/plus/routes.py": "credential help text pointing at the token page",
        "omakase/plus/templates/connections.html": "credential help link",
    },
}

# Providers the app does not declare. Their domains are still detected, so a
# hardcoded TorBox or AllDebrid endpoint fails even though no constant exists yet.
UNAPPROVED_PROVIDER_DOMAINS = frozenset(
    {
        "torbox.app",
        "alldebrid.com",
        "premiumize.me",
        "put.io",
        "pikpak.com",
        "easybytez.com",
        "offcloud.com",
        "debrid-link.com",
        "stremio.com",
        "torrentio.strem.fun",
    }
)

PROVIDER_DOMAINS = frozenset(DECLARED_PROVIDER_DOMAINS) | UNAPPROVED_PROVIDER_DOMAINS

# Provider ids the app may branch on, and why each module is allowed to branch.
# Everything listed here predates the gate. Code added outside these modules must
# stay provider agnostic and read the provider from settings instead.
PROVIDER_ID_MODULES: Mapping[str, str] = {
    "omakase/plus/connections.py": "connection specs and per-integration credentials",
    "omakase/plus/connections_routes.py": "credential routes that validate an integration id",
    "omakase/plus/debrid_routes.py": "playback provider preference routing",
    "omakase/plus/doctor.py": "provider health diagnostics",
    "omakase/plus/health.py": "per-provider health checks",
    "omakase/plus/history_artwork.py": "per-service artwork source",
    "omakase/plus/households.py": "household sharing rules per integration",
    "omakase/plus/language_source.py": "language source selection per integration",
    "omakase/plus/media_adoption.py": "Real-Debrid adoption path",
    "omakase/plus/media_sync.py": "storage backend reporting per provider",
    "omakase/plus/movie_history.py": "watched status source",
    "omakase/plus/movies.py": "per-service movie catalogue reads",
    "omakase/plus/native_tv_library.py": "Apple TV source mode branching",
    "omakase/plus/native_tv_test.py": "Apple TV acceptance probes per mode",
    "omakase/plus/product_views.py": "provider filter and provider kind projection",
    "omakase/plus/series_adoption.py": "Real-Debrid adoption path",
    "omakase/plus/templates/connections.html": "connection state branch in the settings template",
    "omakase/plus/templates/setup.html": "setup guidance branch per integration",
    "omakase/plus/title_removal.py": "removal safety check for Real-Debrid assets",
    "omakase/plus/viewing_history.py": "watched status source",
    "omakase/plus/web_playback_navigation.py": "personal scope branch on the playback provider",
    "omakase/plus/web_playback_routes.py": "playback provider branches for scope and monitors",
}

# Concrete provider ids the app may branch on. ``auto`` is deliberately absent:
# it is also a materialisation strategy and a navigation sentinel, so treating it
# as a provider id would flag unrelated code.
PROVIDER_ID_VOCABULARY = frozenset({"realdebrid", "plex", "local_import", "qbittorrent"})

# Providers that omakase does not declare. Naming any of these in a comparison
# means someone hardcoded a provider the app cannot configure.
UNAPPROVED_PROVIDER_IDS = frozenset(
    {
        "torbox",
        "alldebrid",
        "premiumize",
        "putio",
        "pikpak",
        "easybytez",
        "offcloud",
        "debridlink",
        "linksnappy",
    }
)

_COMPARISON_OPERATORS = frozenset({"==", "!="})
_OPEN_BRACKETS = frozenset("{([")
_CLOSE_BRACKETS = frozenset("})]")
_DOMAIN = re.compile(
    r"(?i)(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|co|tv|app|me|io|fun|net|org|dev)(?![\w-])"
)
_NON_PYTHON_COMPARISON = re.compile(
    r"""(?:==|!=|not\s+in\b|\bin\b)[^;{}\n]{0,80}?['"]([a-z_]+)['"]"""
    r"""|['"]([a-z_]+)['"][^\n]{0,80}?(?:==|!=|\bnot\s+in\b|\bin\b)"""
)
_BARE_ID = r"(?<![0-9]){}(?![0-9])"


def relative(path: Path) -> str:
    return path.relative_to(SOURCE_ROOT).as_posix()


def source_paths() -> list[Path]:
    """Every shipping source file in scope, sorted for stable output."""
    paths = []
    for path in SOURCE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        name = relative(path)
        if name.startswith(EXCLUDED_PATHS) or EXCLUDED_NAMES.search(name):
            continue
        paths.append(path)
    return sorted(paths)


@functools.cache
def read_sources() -> Mapping[str, str]:
    return {
        relative(path): path.read_text(encoding="utf-8", errors="replace")
        for path in source_paths()
    }


@functools.cache
def tree_comparisons() -> Mapping[str, frozenset[str]]:
    """Provider id comparisons in the real tree, scanned once per test session."""
    return provider_id_comparisons(read_sources())


def title_mentions(
    sources: Mapping[str, str], phrases: Iterable[str] = TITLE_PHRASES
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for name, text in sources.items():
        lowered = text.lower()
        for phrase in phrases:
            count = lowered.count(phrase)
            if count:
                counts[(name, phrase)] = count
    return counts


def title_id_mentions(
    sources: Mapping[str, str], ids: Iterable[str] = TITLE_IDS
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for name, text in sources.items():
        for value in ids:
            count = len(re.findall(_BARE_ID.format(re.escape(value)), text))
            if count:
                counts[(name, value)] = count
    return counts


def provider_domain_sites(
    sources: Mapping[str, str],
    domains: Iterable[str] = PROVIDER_DOMAINS,
) -> dict[tuple[str, str], int]:
    watched = tuple(domains)
    counts: dict[tuple[str, str], int] = {}
    for name, text in sources.items():
        for match in _DOMAIN.finditer(text):
            found = match.group(0).lower()
            owner = next(
                (domain for domain in watched if found == domain or found.endswith("." + domain)),
                None,
            )
            if owner is not None:
                counts[(name, owner)] = counts.get((name, owner), 0) + 1
    return counts


def _string_value(token) -> str | None:
    if token.type != tokenize.STRING:
        return None
    try:
        value = ast.literal_eval(token.string)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _literals_after(tokens, index: int) -> list:
    position = index + 1
    if position >= len(tokens):
        return []
    token = tokens[position]
    if token.type == tokenize.STRING:
        return [token]
    if token.type == tokenize.OP and token.string in _OPEN_BRACKETS:
        found, depth = [], 0
        for token in tokens[position:]:
            if token.type == tokenize.OP and token.string in _OPEN_BRACKETS:
                depth += 1
            elif token.type == tokenize.OP and token.string in _CLOSE_BRACKETS:
                depth -= 1
                if depth == 0:
                    break
            elif token.type == tokenize.STRING:
                found.append(token)
        return found
    return []


def _literals_before(tokens, index: int) -> list:
    position = index - 1
    if position < 0:
        return []
    token = tokens[position]
    if token.type == tokenize.STRING:
        return [token]
    if token.type == tokenize.OP and token.string in _CLOSE_BRACKETS:
        found, depth = [], 0
        for token in reversed(tokens[: position + 1]):
            if token.type == tokenize.OP and token.string in _CLOSE_BRACKETS:
                depth += 1
            elif token.type == tokenize.OP and token.string in _OPEN_BRACKETS:
                depth -= 1
                if depth == 0:
                    break
            elif token.type == tokenize.STRING:
                found.append(token)
        return found
    return []


def _python_comparisons(text: str) -> set[str]:
    try:
        tokens = [
            token
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type
            not in {
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
            }
        ]
    except (tokenize.TokenError, IndentationError) as error:
        raise RuntimeError(f"cannot tokenize source for the provider id gate: {error}") from error
    literals: set[str] = set()
    for index, token in enumerate(tokens):
        compared = (token.type == tokenize.OP and token.string in _COMPARISON_OPERATORS) or (
            token.type == tokenize.NAME and token.string == "in"
        )
        if not compared:
            continue
        for candidate in [*_literals_after(tokens, index), *_literals_before(tokens, index)]:
            value = _string_value(candidate)
            if value is not None:
                literals.add(value)
    return literals


def _template_comparisons(text: str) -> set[str]:
    literals: set[str] = set()
    for match in _NON_PYTHON_COMPARISON.finditer(text):
        literals.add(match.group(1) or match.group(2))
    return literals


def provider_id_comparisons(
    sources: Mapping[str, str],
    vocabulary: Iterable[str] = PROVIDER_ID_VOCABULARY | UNAPPROVED_PROVIDER_IDS,
) -> dict[str, frozenset[str]]:
    watched = frozenset(vocabulary)
    found: dict[str, frozenset[str]] = {}
    for name, text in sources.items():
        if name.endswith(".py"):
            literals = _python_comparisons(text)
        elif name.endswith((".html", ".js")):
            literals = _template_comparisons(text)
        else:
            continue
        hits = literals & watched
        if hits:
            found[name] = frozenset(hits)
    return found


def declared_provider_ids() -> frozenset[str]:
    from omakase.plus.debrid_library import PLAYBACK_PROVIDERS
    from omakase.plus.media_store import _PROVIDER_KINDS

    declared = frozenset(PLAYBACK_PROVIDERS) | frozenset(_PROVIDER_KINDS)
    assert declared, "the app must declare at least one provider id"
    return declared


def test_no_new_title_specific_strings():
    found = title_mentions(read_sources())
    grown = {
        key: (count, TITLE_DEBT_CEILING[key])
        for key, count in found.items()
        if count > TITLE_DEBT_CEILING.get(key, 0)
    }
    assert not grown, (
        "title specific string hardcoded in src: "
        + ", ".join(
            f"{name}: {phrase!r} ({count} > {allowed})"
            for (name, phrase), (count, allowed) in sorted(grown.items())
        )
        + ". Branch on facts from the catalogue, not on one title. If this is an intentional "
        "release contract, add it to TITLE_DEBT_CEILING with the measured count and a reason."
    )


def test_no_new_title_specific_anilist_ids():
    found = title_id_mentions(read_sources())
    grown = {
        key: (count, TITLE_ID_DEBT_CEILING[key])
        for key, count in found.items()
        if count > TITLE_ID_DEBT_CEILING.get(key, 0)
    }
    assert not grown, (
        "title specific AniList id hardcoded in src: "
        + ", ".join(
            f"{name}: {value} ({count} > {allowed})"
            for (name, value), (count, allowed) in sorted(grown.items())
        )
        + ". Resolve the id from the catalogue instead of naming one title."
    )


def test_provider_hostnames_stay_in_declared_modules():
    found = provider_domain_sites(read_sources())
    allowed = {
        (name, domain) for domain, owners in DECLARED_PROVIDER_DOMAINS.items() for name in owners
    }
    stray = {key: count for key, count in found.items() if key not in allowed}
    assert not stray, (
        "provider hostname hardcoded outside a declared module: "
        + ", ".join(
            f"{name}: {domain} ({count})" for (name, domain), count in sorted(stray.items())
        )
        + ". Import the endpoint constant from the module that owns this provider, or declare "
        "the module in DECLARED_PROVIDER_DOMAINS with a reason."
    )


def test_provider_ids_are_declared():
    declared = declared_provider_ids()
    found = tree_comparisons()
    unapproved = {
        name: sorted(ids & UNAPPROVED_PROVIDER_IDS)
        for name, ids in found.items()
        if ids & UNAPPROVED_PROVIDER_IDS
    }
    assert not unapproved, (
        "comparison against a provider id the app does not declare: "
        + ", ".join(f"{name}: {ids}" for name, ids in sorted(unapproved.items()))
        + f". Declared ids are {sorted(declared)}."
    )


def test_provider_id_comparisons_stay_in_declared_modules():
    found = tree_comparisons()
    stray = {name: sorted(ids) for name, ids in found.items() if name not in PROVIDER_ID_MODULES}
    assert not stray, (
        "provider id comparison outside a declared provider-aware module: "
        + ", ".join(f"{name}: {ids}" for name, ids in sorted(stray.items()))
        + ". Keep rendering, routing and presentation code provider agnostic; if this module "
        "must branch on a provider, declare it in PROVIDER_ID_MODULES with a reason."
    )


def test_provider_id_vocabulary_is_actually_observed():
    """Positive control on the real tree: the scanner must see declared ids."""
    observed = set()
    for ids in tree_comparisons().values():
        observed.update(ids)
    missing = PROVIDER_ID_VOCABULARY - observed
    assert not missing, (
        f"the provider id scanner found no comparison for {sorted(missing)}; the scanner is "
        "broken or every one of these providers was removed from the tree. Fix the scanner "
        "before trusting the gate."
    )


def test_detectors_fire_on_synthetic_violations():
    """The rules must fail on a hardcoded title, hostname or provider id."""
    sources = {
        "omakase/plus/routes.py": 'title = "Frieren Beyond Journeys End"\nif provider == "torbox": pass\n',
        "omakase/plus/templates/title.html": '<a href="https://api.real-debrid.com/rest">x</a>\n',
    }
    assert title_mentions(sources)[("omakase/plus/routes.py", "frieren")] == 1
    comparisons = provider_id_comparisons(sources)
    assert comparisons["omakase/plus/routes.py"] == frozenset({"torbox"})
    domains = provider_domain_sites(sources)
    assert ("omakase/plus/templates/title.html", "real-debrid.com") in domains
    assert (
        title_id_mentions({"omakase/plus/routes.py": "if anilist_id == 185874:"})[
            ("omakase/plus/routes.py", "185874")
        ]
        == 1
    )
