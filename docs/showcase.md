# Real-media showcase with a sample account

This runner serves the actual Omakase Plus interface using real anime, TV and
movie metadata plus explicitly supplied, locally cached artwork. The account,
watch progress, ratings and availability badges are demonstration fixtures.
No owner account, production database, private library snapshot or configuration
file is loaded. The runner never fetches metadata or artwork from the internet.

## Run

Use a clean process with the project's Python dependencies installed:

```sh
uv run --frozen python scripts/showcase_preview.py --manifest ./demo/catalog.json --art-dir ./demo/art --check
uv run --frozen python scripts/showcase_preview.py --manifest ./demo/catalog.json --art-dir ./demo/art --port 4416
```

Review [Home](http://127.0.0.1:4416/plus/home),
[TV](http://127.0.0.1:4416/plus/tv),
[Movies](http://127.0.0.1:4416/plus/movies),
[History](http://127.0.0.1:4416/plus/activity),
[Settings](http://127.0.0.1:4416/plus/settings), or
[Setup](http://127.0.0.1:4416/plus/onboarding?step=1).
Setup steps 1 through 4 are directly addressable with `step`. All saving,
continuation and external-action forms return 405. The runner always binds to
`127.0.0.1`; there is no host/bind override. Ctrl+C normally removes the run's
temporary database. Forced termination may leave an unused temporary directory;
no existing directory is reused on the next run.

## Explicit artwork/metadata contract

The caller supplies a JSON manifest and artwork directory through arguments,
not environment variables. Each of `anime`, `tv` and `movies` requires 1 to 100
entries. IDs, titles, years, descriptions and any episode names must come from
verified catalog facts. The runner does not invent episode names or descriptions.

```json
{
  "anime": [{
    "anilist_id": 1,
    "title": "Cowboy Bebop",
    "episodes": 26,
    "overview": "Caller-supplied verified catalog description",
    "poster": "anime-1.poster.jpg",
    "banner": "anime-1.banner.jpg"
  }],
  "tv": [{
    "tmdb_id": 1396,
    "title": "Breaking Bad",
    "year": "2008",
    "overview": "Caller-supplied verified catalog description",
    "poster": "tv-1396.poster.jpg",
    "banner": "tv-1396.banner.jpg",
    "episodes": [{"title": "Pilot", "air_date": "2008-01-20"}]
  }],
  "movies": [{
    "imdb_id": "tt0482571",
    "tmdb_id": 1124,
    "title": "The Prestige",
    "year": "2006",
    "overview": "Caller-supplied verified catalog description",
    "poster": "tt0482571.poster.jpg",
    "banner": "tt0482571.banner.jpg"
  }]
}
```

Only relative JPEG/PNG/WebP paths resolving beneath the explicit artwork
directory are accepted. Absolute paths, URLs, drive paths, traversal escapes,
missing files and unknown media IDs are rejected. `banner` may be omitted to use
the poster. TV `episodes` is optional; omit it when exact names are unavailable.
The application serves images from same-origin artwork routes. It never falls
back to remote poster services. Keep provenance for the actual media and cached
artwork alongside the capture evidence. The runner and sample account do not
supply a license or new publication rights for third-party media artwork.

## Account and disclosure

The sample account email is `alex.morgan@example.com`, using the reserved example
domain. Its profile picture is the first supplied anime poster, processed through
the application's native bounded PNG avatar pipeline. The native display-name
preference is seeded as `Alex Morgan`; the demo never replaces the UI DOM.
Settings and the account menu show the same native account and picture.

The actual application routes and templates render migrated monitor, TV, movie,
episode and history tables. A preview-only view wrapper supplies Home readiness
counts. These are explicitly demo fixtures, not fabricated provider or media
verification receipts. TV Plex availability remains unconfirmed. The banner says:

> Demo · Sample account and history · External actions disabled

Its subline identifies real media/cached artwork and sample account, history,
ratings and availability. Keep this disclosure visible in screenshots or state
the sample-account nature immediately alongside a cropped image. Do not present
sample watch history or availability as verified user/provider outcomes.

## Configuration and networking isolation

The runner refuses inherited `OMAKASE_*` variables. It sets only five owned
application variables for its fresh data directory, random key/invite, private
mode and loopback host list. Before the first `omakase` import, it suppresses the
package's custom repository `.env` existence check without reading that file.
`PYTHON_DOTENV_DISABLED=1` is also set and `dotenv.load_dotenv` is guarded before
application imports. A process-local `.env` text-read guard remains active. The
application environment must match the prepared environment after imports and
before/after every request; unexpected configuration drift stops the preview.
Do not remove these guards to reuse a production-configured shell.

The middleware permits only GET/HEAD on the showcase interface, local artwork
and static routes. Writes return 405, OAuth and other out-of-scope routes return
404, and untrusted hosts are rejected. Browser CSP uses same-origin resources
and `connect-src 'none'`. External DNS and socket connections are denied. Only
loopback socket connects remain for Windows asyncio's wake-up socketpair. The
runner contains no provider credentials and never calls a remote catalog.
These scoped safeguards are not a general sandbox for untrusted Python code.
Run this script in its own process, never import it into a production worker.

## Maintained checks

`--check` exercises the actual ASGI app: Home, TV and movie collections/details,
history, local art, the native avatar, four setup steps and settings pages. It
checks write rejection, OAuth rejection, hostile-host rejection, outbound denial,
zero dotenv calls/reads, exactly one suppressed package `.env` check and the
unchanged application environment allowlist. Ruff checks the runner. The existing
Starlette/httpx TestClient deprecation warning does not trigger any installation.
Browser captures and physical-device acceptance are separate proof. A caller
must supply valid real-media fixtures before these checks can run.

Verified October27,2026: the supplied mixed real-media manifest/cache passed the maintained boundary checks. Capture provenance remains with the owner''s review evidence; no artwork is bundled into the source by this runner.

## Included capture catalog

`docs/showcase-media.json` records the real title identities used for the checked
screenshots. Its artwork filenames refer to a separately prepared cache; it
contains no credentials or account history. Supply that file and a matching
approved artwork directory when reproducing the capture. Titles are recognizable
examples, not a promise that any particular release is available to download.
