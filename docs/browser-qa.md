# Optional browser QA

The app uses Python dependencies. The optional scripts/check_*.cjs browser
regressions additionally need Node.js and Playwright; some need @axe-core/playwright.
Install these QA tools in your own development environment and install a supported
Playwright Chromium browser. They are not application runtime dependencies.

Scripts resolve modules from the checkout's node_modules by default. Set
OMAKASE_QA_NODE_MODULES to an existing module directory when using a separate QA
environment. OMAKASE_QA_PYTHON can name a Python interpreter with the locked app
dependencies; otherwise the script detects .venv for the current platform.

Chromium is the default. Set OMAKASE_QA_BROWSER_CHANNEL to msedge or chrome only
when that browser is installed, or OMAKASE_QA_BROWSER_EXECUTABLE to an explicit
browser executable. Existing per-script output variables remain supported;
default output is a task-specific subdirectory of the OS temporary directory.

Use scripts/showcase_preview.py for a read-only loopback preview with explicit
catalog facts and cached artwork; see [Showcase](showcase.md). Network and form
mutations are blocked by the isolated fixtures where documented. Do not point
safe-preview checks at a production database or infer live provider acceptance.

Example after preparing the Python and QA environments:

```sh
node scripts/check_title_details.cjs
```
