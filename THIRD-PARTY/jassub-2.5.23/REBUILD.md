# Offline source assembly and replacement

This directory and the asset-adjacent `SOURCE.json` offer complete preferred
source archives for JASSUB and its five pinned native libraries. The local
`corresponding-source.tar.gz` contains those exact archives, original native
recipes/patches, captured npm packages and integrity lock, local derivative
scripts, licenses and this document. It does not depend on a future Git commit,
a hosted source upload, or access to a private media installation.

The bundled npm WASM files have not been rebuilt here. Exact native binary/source
correspondence and bit-identical native reproducibility are **not proved**.
Complete source permits a modified native library to be rebuilt and relinked;
extra proprietary object files are not required by this source-build route.
This package does not claim that a rebuild has run or that all loader/ABI variants
have passed runtime acceptance.

## Verify and assemble without network access

Use Python 3.12 or later. Download `SOURCE.json` and the adjacent
`corresponding-source.tar.gz`; verify its byte count and SHA-256 from the index.
Extract the bundle into a new directory using a safe archive extractor. In that
extracted directory:

```sh
python3 scripts/build_ass_source_bundle.py assemble --bundle-root . --output work/jassub
```

The helper verifies **all six** archives against `native-source-access.json`,
extracts the preferred JASSUB tree and populates `lib/brotli`, `lib/freetype`,
`lib/fribidi`, `lib/harfbuzz` and `lib/libass` with their exact source revisions.
Their configure/autogen files and the original JASSUB `build/patches` remain
available. Refusing an existing destination preserves previous modifications.

GitHub source archives intentionally omit `.git`. The ordinary upstream `make`
path does not need `git submodule` when the five source directories are populated.
Do **not** run `git-checkout`, `git-smreset`, or `clean-jassub`: those are checkout
maintenance targets. `Makefile_licence` separately depends on `.git/HEAD` and
`.git/modules/lib/*/HEAD` and still lists the obsolete expat project. Do not
fabricate Git metadata or invent a sixth native submodule to satisfy it. The
provided offline notice collector preserves the rights-bearing native comment
blocks directly, alongside the full provided named license/copyright files.

To regenerate those notices in this extracted package:

```sh
python3 scripts/vendor_ass_notices.py --source-dir sources/native
```

## Build both native variants after your changes

Edit the assembled source/library you want to modify. Use the retained upstream
Dockerfile, whose base remains exactly `docker.io/emscripten/emsdk:6.0.12`.
Docker, its image and ordinary system build tools are external prerequisites;
the original Dockerfile documents/installs the required build utilities. Obtaining
that image or OS packages may require network access. No native source download,
submodule checkout or npm installation is required for the assembly/relink flow.
From a Linux shell with Docker:

```sh
docker build -f work/jassub/Dockerfile -t omakase-ass-user-build work/jassub
docker run --rm -v "$PWD/work/jassub:/code" omakase-ass-user-build \
  bash -lc 'make && env MODERN=1 make'
```

The Makefile applies the retained FreeType/Harfbuzz patches, builds the five
libraries statically, and links JASSUB's C++ binding. Legacy and modern build
caches are separate. The expected outputs are all four files:

```
src/wasm/jassub-worker.js
src/wasm/jassub-worker.wasm
src/wasm/jassub-worker-modern.js
src/wasm/jassub-worker-modern.wasm
```

Do not mix an old WASM with a newly generated incompatible loader or rebuild just
one variant. The original runtime uses the legacy generated loader with the
legacy/modern WASM choice, so ABI and browser compatibility must be checked for
**both** variants after a toolchain/library change. Preserve both generated
loaders with your rebuild evidence; the rebundling step integrates the generated
legacy loader just as the original derivative does.

## Rebundle the compatible loader and replace the renderer together

The captured build-only esbuild executable is Windows x64, matching the original
local derivative build. Copy/access the native build tree from Windows and run
Python 3.12+ in the extracted bundle directory (no npm lifecycle scripts run):

```powershell
python scripts/build_ass_source_bundle.py relink --bundle-root . `
  --native-tree work/jassub --output rebuilt-ass
```

This helper verifies all captured npm archives against the SHA-512 lock, replaces
both WASM variants and the generated loader in a temporary package tree, then
applies the same derivative changes: no rvfc polyfill, one renderer thread,
512-MiB shared-memory maximum, same-origin credentialed/no-redirect worker fetch,
and esbuild ESM bundling. A changed generated-loader memory expression fails
closed for explicit review instead of silently dropping the memory bound.
It copies the unchanged fallback font and full licenses. It does not install or
publish anything. The original `scripts/vendor_ass.py` is also retained as the
exact original derivative recipe; it downloads locked npm payloads, whereas this
relink helper uses the offered local archives and your newly built native output.

Review/qualify the resulting `jassub.js`, `worker.js`, **both** WASM files,
`default.woff2` and `LICENSES.txt` as one replacement. A user-controlled installation
can replace that renderer asset set together and update its asset hashes, source
offer and trusted delivery whitelist. Recheck legacy and SIMD-capable clients,
shared-memory isolation, renderer lifecycle, ASS/font fidelity and ABI. These
instructions authorize no deployment and make no native-build success claim.
