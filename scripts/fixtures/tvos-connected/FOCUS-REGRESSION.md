# White focus rendering regression

The dedicated `RemoteFocusRenderingTests` runs the actual `LiveLibraryScreen`
navigation and `HomeScreen` against the existing synthetic connected fixture.
It uses production controls and focus styles without duplicating them. The
fixture has no artwork, so bright art cannot mask or trigger the pixel check.

It drives `XCUIRemote` to Home, then the Continue Watching card, selects the
card, verifies an authenticated playback pass, and uses Menu to require focus
return to the same card. Screenshots remain in the xcresult attachments.
The test measures near-white RGB pixels in blank regions outside the Home tab
and beside the card subtitle. Purple app borders and centered white text are
outside the samples or excluded by the RGB threshold. Every region must have
more than 50 pixels and under 8 percent near-white area. Sample rectangles and
white counts are emitted as `FOCUS_PIXELS` in the test log.

## Launch on a hosted macOS runner

Use an isolated source checkout containing both the production fix and these
tests. The existing build pipeline's exact Xcode path and TVVLCKit archive/hash
are reused. No credentials, production server, paid resource or device is used.

```sh
export DEVELOPER_DIR=/Applications/Xcode_26.3.app/Contents/Developer
# Optional exact replay pin copied from a prior runtime-id.txt:
export TVOS_RUNTIME=com.apple.CoreSimulator.SimRuntime.tvOS-26-2
bash scripts/fixtures/tvos-connected/run_focus_regression.sh "$RUNNER_TEMP/focus-proof"
```

The runtime above is an example identifier, not a claim it is installed. Omit
`TVOS_RUNTIME` to select the newest installed available tvOS runtime; the script
records its exact identifier, inventory, Xcode version, device type, source
commit and binary diff. A missing requested runtime fails with its inventory.
The script creates, boots, shuts down and deletes only its own simulator.
Its loopback fixture and temporary TLS material are stopped and removed on exit.
Use a fresh checkout each run: an existing Vendor directory is rejected rather
than silently trusting an unverified dependency.

The manual-only `.github/workflows/tvos-focus.yml` performs this same launch
and retains artifacts even on failure. Preparing this workflow does not run it
or authorize a push. Once separately authorized and present on the selected
remote ref, dispatch `tvOS focus rendering regression` on that exact ref.

## Baseline and candidate evidence

Run the test commit with pre-fix production files, then the same test commit
with fixed production files, on the same exact recorded runtime. The baseline
must fail the white-area assertion on the offending surface, and the candidate
must pass it together with Select/Menu assertions. Keep both xcresults and
`FOCUS_PIXELS` logs. An unchanged screenshot or focus-state-only pass cannot
prove this regression is detected. If the baseline passes, the simulator did
not reproduce the photographed defect or the sample needs adjustment; report
that limit and keep physical Apple TV acceptance open.

The separate library gesture test holds Select on the production Movies
Continue Watching tile. It requires details without a playback pass, Menu focus
return, and a working short Select afterwards. This exercises the production
tile style's exclusive long/short gesture handling.

This is simulator rendering and remote interaction proof only. It does not
prove physical Siri Remote gestures, device OS rendering, Top Shelf rendering,
signed installation or owner visual acceptance. The Home card has no long
select action; long select is checked on the actual library tile instead.
