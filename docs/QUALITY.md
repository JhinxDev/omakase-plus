# Quality gates

One page for every check that protects this repository: what runs automatically,
what a contributor runs by hand, and what still needs a live server or the
physical Apple TV. Use it before a release, and update it whenever a gate is added,
wired into CI or retired.

The rule behind the page is simple. A check that nobody runs is not a gate. A check
that is excluded from CI must have a written reason, and the reason must name what
the check needs, not that it was inconvenient.

## Enforced in CI

`.github/workflows/ci.yml` is the only workflow this page describes. Every job
below runs on every push to `main` and `codex/**` and on every pull request to
`main`.

| Job | Gate | Command |
| --- | --- | --- |
| `lint` | Ruff lint and formatting | `ruff check .` and `ruff format --check .` |
| `docker-build` | Production image still builds | `docker build -t omakase-plus:ci .` |
| `test` | Full suite on Python 3.11, 3.12 and 3.13 with at least 80 percent coverage | `pytest -ra --cov=omakase --cov-fail-under=80` |
| `agent-protocol` | Real MCP subprocess and local boundaries | `pytest -q tests/plus/test_agent_tools.py` |
| `quality-gates` | Offline budgets, genericity rules and infrastructure contracts | `pytest -q tests/infra tests/plus/test_performance_budgets.py tests/plus/test_genericity.py` |
| `node-unit` | Web player and ASS reader unit tests, no browser | `node --test tests/browser/web_ass_reader.test.cjs tests/browser/web_player_client.test.cjs tests/browser/web_player_menus.test.cjs` |
| `browser-qa` | Seven headless Edge interface checks | `node scripts/check_*.cjs`, see the inventory below |

The `quality-gates` files also run inside `test`. The separate job exists for a
fast, isolated failure: a broken budget or a hardcoded title fails in about a
minute instead of waiting for the coverage run.

## Performance budgets

`tests/plus/test_performance_budgets.py` gates only work that happens in this
process. Each test builds its own fixture, makes no network call, reads no
database and spawns no process. Every measurement is the best of several timed
runs with `time.perf_counter`, so scheduler noise affects the discarded runs.

| Target | Measured local | Budget | Ratio |
| --- | --- | --- | --- |
| Provider playlist parse, 712 segments | 9.1 ms | 70 ms | 7.7x |
| Master playlist, 16 subtitle renditions (2000 calls) | 8.5 ms | 60 ms | 7.1x |
| Subtitle media playlist, 700 segments (500 calls) | 91 ms | 600 ms | 6.6x |
| WebVTT serialization, 700 cues | 1.3 ms | 10 ms | 7.7x |
| Subtitle readiness, pending job | 404 ms | 1500 ms | 3.7x |
| Subtitle readiness, ready job | 1.1 ms | 250 ms | 227x |

A budget sits above the measured local value so a loaded CI runner does not fail,
and below ten times that value so an order of magnitude regression does fail. A
budget of exactly ten times the baseline would be blind to the regression it is
supposed to catch.

To change a budget, measure first, then update the table in the module docstring
and the constant in the same commit, and say why in the commit message. Never
raise a budget to make a red build green: that is the failure mode these tests
exist to prevent.

These costs are provider or device bound and are deliberately not gated. They are
recorded so nobody mistakes an architectural cost for a code regression:

| Provider or device cost | Measured live |
| --- | --- |
| Cold Real-Debrid first segment | about 9.8 s |
| OCR of one 60 minute episode | about 272.5 s |
| ASR of one minute of audio | about 4.1 s |
| Warm Apple TV play reply | 1.2 s to 2.0 s |
| Cached first segment | milliseconds |

The warm play reply is the number the in process budgets protect. The reply has to
stay in the low seconds while the cold first segment is warmed in the background,
so the cheap in process work on that path is what CI can defend.

## Genericity gates

`tests/plus/test_genericity.py` keeps one household's data out of the shipped
source. It scans `src` only, and it skips third party assets.

1. Title specific strings. The gate fails when a banned title phrase or a known
   title specific AniList id appears more often than the count recorded in
   `TITLE_DEBT_CEILING` and `TITLE_ID_DEBT_CEILING`. Today the source carries
   numbering contracts for three titles (in `coverage.py`,
   `publication_episodes.py`, `identity.py`, `catalog.py`, `manifest.py`,
   `media_store.py`, `media_sync.py`) and two template placeholders
   (`templates/dashboard.html`, `templates/discover.html`). Those are recorded as
   measured ceilings. A count may fall, never rise, and one new mention anywhere
   fails, including inside a file that is already listed. When the debt is
   removed, delete the entry.
2. Provider hostnames. Every provider domain in `src` must appear only in the
   modules registered in `DECLARED_PROVIDER_DOMAINS`, each with a written reason.
   Domains of providers the app does not declare, such as TorBox or AllDebrid, are
   detected too, so naming one fails even though no constant exists for it.
3. Provider ids. Branching on a provider id is allowed only in the modules listed
   in `PROVIDER_ID_MODULES`, and only for ids the app declares in
   `media_store._PROVIDER_KINDS` or `debrid_library.PLAYBACK_PROVIDERS`. A
   comparison against an undeclared provider fails.

The module also holds positive control tests that inject a synthetic title,
hostname and provider id and assert that each rule fires. If someone breaks a
detector, that test fails before the detector can pass the tree by accident.

To allow a new exception, add the narrowest entry that works, with a reason, in the
same commit as the code that needs it. Do not widen a rule to make the tree pass.

## Browser QA inventory

The optional `scripts/check_*.cjs` checks render real templates and CSS with
synthetic data and intercepted requests. They need Node.js, Playwright and a
browser; they are not application dependencies. `docs/browser-qa.md` describes the
environment variables.

Wired into the `browser-qa` job on `windows-latest`, all verified headless with
Microsoft Edge at this revision:

| Script | What it proves |
| --- | --- |
| `check_collection_layout.cjs` | Collection layout and template composition, 21 checks |
| `check_home_progress_layout.cjs` | Home progress semantics, aligned card footers and remote safe actions, 7 checks |
| `check_library_context.cjs` | Shared shell, synthetic scope data and native controls, 12 checks |
| `check_search_result_edges.cjs` | Result edge scrolling at both ends with mixed lengths, 6 cases |
| `check_title_details.cjs` | Title detail layout, playable states, keyboard and touch targets |
| `check_web_consistency.cjs` | Shared style and spacing consistency, 4 checks |
| `check_interface_scrollbars.cjs` | Native Windows scrollbars and footer at three viewports |

Excluded from CI, with the reason:

| Script | Why it is not in CI |
| --- | --- |
| `check_discovery.cjs` | Needs the read only loopback preview server (default 8876, `OMAKASE_REVIEW_URL`) and `@axe-core/playwright` |
| `check_episode_rechecks.cjs` | Needs the read only loopback preview server (default 4419, `OMAKASE_REVIEW_URL`) |
| `check_google_redirect.cjs` | Needs `@axe-core/playwright`; spawns the Python route fixture over stdin |
| `check_hero_edge_palette.cjs` | Needs the read only loopback preview server (default 4419, `OMAKASE_REVIEW_URL`) |
| `check_hero_layout.cjs` | Needs the read only loopback preview server (default 4418, `OMAKASE_REVIEW_URL`) |
| `check_hero_quality.cjs` | Fails at this revision: it expects `object-fit: scale-down` while `hero-quality.css` ships `contain`. Reconcile the script with the shipped CSS, do not weaken the assertion |
| `check_movie_feedback.cjs` | Needs the movie feedback fixture app on loopback port 4423 (`OMAKASE_MOVIE_QA_BASE`) |
| `check_recommendation_feedback.cjs` | Needs the read only loopback preview server (default 4418, `OMAKASE_REVIEW_URL`) |
| `check_settings_navigation.cjs` | Needs the read only loopback showcase (default 4419, `OMAKASE_QA_ORIGIN`) |
| `check_settings_surfaces.cjs` | Needs the read only loopback preview server (default 4418, `OMAKASE_REVIEW_URL`) |
| `check_text_action_spacing.cjs` | Needs the read only loopback preview server (default 4418, `OMAKASE_REVIEW_URL`) |

Also excluded, outside `scripts/check_*.cjs`:

| Check | Why it is not in CI |
| --- | --- |
| `scripts/qa-environment.test.cjs` | Fails at this revision: it asserts every check script uses `qaBrowser()`, and `check_interface_scrollbars.cjs` launches an explicit Edge channel instead. Fix the helper use or the assertion first |
| `tests/browser/web_ass_client.test.cjs` | Needs a private qualification MP4 and a browser |
| `tests/browser/web_player_startup_browser.test.cjs` | Needs Playwright and a synthetic loopback clip |
| `tests/js/language_settings_qa.cjs` | Needs Python, Playwright and the template renderer |
| `tests/browser/*_browser.cjs` | Interactive/browser QA driven per feature, see `docs/web-player-controls-qa.md` |

The seven wired scripts were run headless at this revision before being wired. Run
the excluded ones the same way when you touch the code they cover. Start the read
only preview from `docs/showcase.md` with a manifest and art directory, then point
the check at that port:

```sh
uv run --frozen python scripts/showcase_preview.py --manifest CATALOG.json --art-dir ART_DIR --port 4418
OMAKASE_REVIEW_URL=http://127.0.0.1:4418 node scripts/check_settings_surfaces.cjs
```

`check_hero_quality.cjs` is a known red check. It is listed rather than silently
skipped so that the next person to touch the hero has a concrete starting point.

## Gates that need a live server, a provider or the physical TV

No CI job can prove these. They remain part of release verification.

| Gate | Needs | Where |
| --- | --- | --- |
| Read only preview checks above | `scripts/showcase_preview.py`, loopback only | `docs/browser-qa.md`, `docs/showcase.md` |
| Real Plex, Real-Debrid, Seerr, Riven, Prowlarr connections | Live accounts and endpoints | `docs/media-library-and-connections.md` |
| Cold start and warm play reply | A live Real-Debrid account | `docs/real-debrid.md` |
| Subtitle extraction, OCR and ASR on a real file | `ffmpeg`, `mkvmerge`, `pgsrip`, `faster-whisper` | `docs/apple-tv.md` |
| Apple TV pairing, HLS playback, subtitle picker, resume sync, next episode, 85 percent completion | The physical Apple TV and the tvOS build | `docs/apple-tv.md` |
| Far forward seek and HEVC segment preparation | The physical device and a live provider | `docs/apple-tv.md` |
| Plex watch flags and TV inventory checks | A live Plex library | `docs/television.md` |
| Installation, upgrade, rollback and data recovery | The operator environment | `docs/release-checklist.md` |
| Interface acceptance by eye | A human | `DESIGN.md` |

Navigation performance is a judged measurement rather than a test: five paired
runs, separate warm and cold catalog runs, and the budgets recorded in
`docs/browser-qa.md`.

## Running the gates locally

The repository standard is the frozen uv environment. A hash pinned virtual
environment reproduces what CI installs.

```sh
uv sync --frozen --extra dev
uv run --frozen pytest -q tests/infra tests/plus/test_performance_budgets.py tests/plus/test_genericity.py
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

Equivalent CI style environment:

```sh
python -m venv .venv
.venv/Scripts/python.exe -m pip install --require-hashes -r requirements-dev.txt -r requirements-build.txt
.venv/Scripts/python.exe -m pip install --no-build-isolation --no-deps -e .
.venv/Scripts/python.exe -m pytest -q tests/infra
```

Node checks need no install:

```sh
node --test tests/browser/web_ass_reader.test.cjs tests/browser/web_player_client.test.cjs tests/browser/web_player_menus.test.cjs
```

Browser checks need Playwright and a browser once:

```sh
npm install --no-save playwright
node scripts/check_title_details.cjs
```

On Windows with Edge installed, use the configuration CI uses. In PowerShell:

```powershell
$env:OMAKASE_QA_BROWSER_CHANNEL = "msedge"
$env:OMAKASE_QA_PYTHON = ".venv/Scripts/python.exe"
node scripts/check_title_details.cjs
```

On a machine without Edge, set `OMAKASE_QA_BROWSER_EXECUTABLE` to a Chromium
executable instead. The two scrollbar checks hardcode the Edge channel and stay
Windows only.

## What these gates cannot prove

They cannot prove provider transcode quality, OCR accuracy, a real login, a
physical device, installation safety or human creative acceptance. A green build
means the enforced contracts still hold. It is not a release approval; use
`docs/release-checklist.md` for that.
