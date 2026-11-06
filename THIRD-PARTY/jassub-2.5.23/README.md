# Derived JASSUB 2.5.23 private candidate

This is an explicitly modified build, not unmodified JASSUB and not an MIT-only
aggregate. The original npm package and exact dependency metadata are in
`npm-source-lock.json`; locally retained `.tgz` archives verify against its SHA-512
integrity fields. No package lifecycle scripts run.

The delivered JavaScript graph is JASSUB 2.5.23, abslink 1.3.1 (Apache-2.0),
throughput 1.0.2 (MIT), and lfa-ponyfill 1.1.1 (MIT). Build-only esbuild and its
Windows x64 executable are pinned to 0.28.2. The original package dependency
manifest includes rvfc-polyfill, but that package is neither downloaded nor
bundled. No GPL polyfill implementation is copied or reimplemented.

The original declared aggregate license is
`LGPL-2.1-or-later AND (FTL OR GPL-2.0-or-later) AND MIT AND MIT-Modern-Variant AND ISC AND NTP AND Zlib AND BSL-1.0`.
The selected FreeType alternative is FTL. The unchanged bundled Liberation Sans
Regular 2.00.5 font declares SIL OFL 1.1 in its embedded name table. Matching
copyright and full OFL text are retained. Source-container fonts remain private
authenticated assets and are never part of this package.

`THIRD-PARTY-NOTICES.txt` preserves all provided captured license/copyright files,
upstream's aggregate full-license text, full Apache 2.0 and MIT text, and the font
OFL. It is also deployed as `static/ass/LICENSES.txt`. Exact SPDX declarations in
the source lock are upstream license evidence. Three JS dependency archives and
upstream trees provide no separate LICENSE/NOTICE file; no additional copyright
or NOTICE content has been invented. This absence alone is not a demand for an
owner rights grant.

## Changes and reproduction

Run `python scripts/vendor_ass.py` from the repository with network access. It
uses the existing lock. `--capture-lock` is an explicit first-time operation and
refuses to overwrite an existing lock. The script verifies the official JASSUB
integrity against an additional hard-coded expected value.

1. Remove exactly `import 'rvfc-polyfill';` from the entry module. The adapter
   requires native video requestVideoFrameCallback and fails closed otherwise.
2. Replace the worker `THREAD_COUNT` expression with constant 1. Upstream passes
   a pthread pool size of zero for that value. There is one renderer worker.
3. Change the loader's shared-memory maximum from 32768 to 8192 pages. This
   bounds that WASM linear memory to 512 MiB; initial allocation remains 32 MiB.
   It does not cap browser/process memory or guarantee allocation success.
4. Bundle browser ES modules with esbuild. Add a worker fetch guard requiring
   same-origin HTTP(S), same-origin credentials and no redirects. The wrapper
   validates bounded ASS/font responses before passing text/bytes to libass.

Two consecutive builds produced identical complete asset manifests. No external
runtime CDN is used. The serial derivative still requires shared WASM memory,
so the adapter requires crossOriginIsolated. Scope COOP/COEP to the player
document; authentication/OAuth routes remain the integrating owner's scope.

## Corresponding source evidence and precise remaining uncertainty

`native-source-access.json` records immutable repository and complete archive URLs
for JASSUB commit `482a2fed6c8976550f09383e90d92b6ac3360fc9` and its five native
submodule revisions. It also records complete preferred-source archive names,
sizes and SHA-256 hashes. Those complete source archives include
the original native code, build recipes, patches and licensing machinery. The
original Makefile, licensing recipe and default/full-notice files are retained
here separately. They are distributed locally in asset-adjacent `corresponding-source.tar.gz`, discovered through `static/ass/SOURCE.json`, together with all six complete preferred-source archives, captured npm inputs, local derivative scripts and [assembly/build/relink instructions](REBUILD.md). The immutable URLs remain additional provenance references.

All 11 npm-published `src/` files are byte-identical to that immutable JASSUB
commit (`npm-to-upstream-source-comparison.json`). The npm package omits gitHead
and native object/build provenance. Native WASM binary reproducibility and exact
source-to-binary correspondence have not been proved. This is a precise
provenance gap, not proof that the source is wrong or that an upstream license
grant is absent. A distribution reviewer must assess corresponding-source and
relinking obligations using the captured full sources/build recipe; if exact
binary provenance is required, rebuild the native WASM from these pinned sources
with a pinned Emscripten toolchain and retain the complete recipe and artifacts.
Do not label current native compilation reproducible or publication-approved.

The official npm advisory endpoint returned `{}` for all six exact runtime/build
packages. `npm-advisories.json` retains that result; it is not a parser safety
guarantee or an advisory check for native source vulnerabilities.

## Runtime verification

`tests/browser/web_ass_client.test.cjs` runs the real renderer in isolated headless
Edge on loopback port 8130. Supply the existing private qualification baseline
through `OMAKASE_ASS_BASELINE` and Playwright modules through
`OMAKASE_QA_NODE_MODULES`. It uses synthetic dialogue, outline and movement,
seeks while paused, native playback, Off/on, quick replacement, access denial,
off-origin rejection, font-count admission and the 8-second timeout. It checks
one worker at a time, no pthread workers, no off-origin requests, no canvas after
Off, and zero workers after destroy. It does not qualify actual episode font
fidelity, phone behavior, native fullscreen/PiP or representative performance.


## Offline distribution repair

Source-bearing native copyright/license comment blocks are collected directly
from the verified six preferred-source archives, conservatively within upstream's
source scopes. This includes FreeType `src/base/fthash.c`'s complete Computing
Research Labs/New Mexico State University and Francesco Zappa Nardelli permission
notice, which name-only license-file discovery missed. The full blocks are in
`notices/source-headers-*.txt`, with paths and hashes recorded in
`native-source-access.json`; they are included in the aggregate and deployed
`LICENSES.txt`. See `REBUILD.md` for the local source offer and the `.git`-free
assembly route. No native rebuild or bit-identical correspondence is claimed.
