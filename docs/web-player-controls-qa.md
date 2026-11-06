# Focused player controls check

`tests/browser/web_player_controls_browser.cjs` maintains the accepted supplied
Shaka controls regression using a synthetic, silent 30-second H264/AAC preview.
It starts no media-preparation job and does not run full-watch, resume, identity,
provider or deployment acceptance suites.

Use the existing [browser QA environment](browser-qa.md). The test imports
qaRuntime, qaBrowser and qaOutput from `scripts/qa-environment.cjs`, so an existing
Playwright install and configured browser channel/executable work without new
runtime dependencies. Start your own loopback preview with the synthetic clip:

```sh
python scripts/player_preview.py --media /path/to/synthetic-baseline.mp4 --port 8136
```

Run in a separate shell, with optional output/runtime/browser settings:

```sh
OMAKASE_PLAYER_REVIEW_URL=http://127.0.0.1:8136 \
OMAKASE_PLAYER_QA_OUTPUT=/tmp/player-controls-current \
OMAKASE_QA_BROWSER_CHANNEL=msedge \
node tests/browser/web_player_controls_browser.cjs
```

PowerShell users can set the same environment variables with `$env:NAME='value'`
before the Node command. `QA_OUTPUT` is an output alias; the default is the
task-specific temporary QA directory. The review URL must be loopback. Use a
fresh fixture database when checking watched/coverage counts; do not point this
test at a real account or reuse another agent's preview. Stop the preview you
started after the test. Browser contexts close in finally, with process-level
audio muting throughout (including volume adjustment).

The desktop check exercises speed End/Home/arrows without seeking, independent
volume Home/End, seek Home/End, accessible names, 44px range hitboxes, no preset
overlap and Escape focus return. The 390px emulated-touch check uses the normal
Seek input tap, requires a stored 15-second paused position without jump coverage
or completion, checks viewport fit, and uses visible fullscreen entry/exit controls.
Both actual served JS/CSS hashes must match the unchanged source.

Outputs: `result.json`, `speed-desktop.png`, `touch-paused.png`, and `failure.png`
when needed. Choose a new output directory for a new candidate to retain earlier
failures and identities. Emulated Windows Edge touch/document fullscreen does
not establish physical phone/Safari, native video fullscreen, ASS fidelity or
subjective audio acceptance. Emulated Escape-to-exit is deliberately not repeated
as a device check; the accepted touch proof uses the visible Exit control.
