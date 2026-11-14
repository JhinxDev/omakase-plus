# Playback acceptance receipts

Run the offline gate with independent reference and observed receipts.

```sh
python ops/accept_playback.py --verify reference.json observed.json
```

Use synthetic or separately authorized measurements. The JSON objects share a
safe target identifier and a 64-character lowercase hex generation fingerprint.
Keep signed links, playback tokens, source paths, cue text, and personal watch
history out of receipts. Select the tested generation independently of the
candidate's response. Both receipts name the expected local `master_variant` and
`subtitle_variant` playlist filenames.

| Field | Reference | Observed |
| --- | --- | --- |
| `segments` | Every segment in order, with zero-based `index`, `start`, `end`, and SHA-256 `first_frame`, `last_frame`, `content` | The same fields measured from response bytes |
| `cues` | Ordered, stable opaque cue `id` and expected `start` and `end` | The exposed cues and timestamps on the same playback clock |
| Calibration | `calibration_offset` in seconds from a known target/generation | `calibration` with its own measured `target`, `generation`, `status` and `offset`. The gate requires a readback with status `hit`. |
| Timings | Fixed gate ceilings | `timings` in elapsed seconds for `open`, `playlist`, and the slowest exercised `seek` |
| Seeks | `seek_targets` with at least three distinct positions in seconds | `seeks`, in the same order, each with `target` and measured `landed` |
| Completion | `completion_position` in seconds | `completion` with its own `target`, `generation`, submitted `position`, independently queried `readback_position`, and counts `credits_before`, `credits_after`, `credits_after_retry`. Query persistence after commit using a fresh connection. |

Use independently accepted media with equivalent decoded frames and the same
segmentation for the reference. For the observed data, the timeline probe emits
`acceptance_segment` measurements. It SHA-256 hashes the first and last decoded
frame MD5 strings and the newline-joined in-order MD5 strings for the full
segment. Match the decoding and frame-hash method on both sides. Container bytes
may differ. Use a common timeline origin so an unexpected constant shift fails
the comparison. Relative continuity alone cannot prove that clock.

`OMAKASE_PROBE_FAIL_ON_DEFECT=1` makes the timeline probe fail on a reported
defect or incomplete sample. For a controlled local HLS fixture, set
`OMAKASE_PROBE_CONTROL` to its directory and select every segment with
`OMAKASE_PROBE_SEGMENTS` to produce segment observations for the strict gate.
The probe's default sample and the legacy 14-check live smoke each cover only
part of the evidence. Cue, seek, calibration, and completion observations
require collection for the exact candidate. The verifier validates those
observations when supplied. The server-side gate does not measure tvOS rendering.

Exit 0 means the submitted receipts passed all checks, exit 1 means the evidence
failed a check, and exit 2 means an input could not be used. The report includes
the gate name and failing check names without echoing receipt contents.
