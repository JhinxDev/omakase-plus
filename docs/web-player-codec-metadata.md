# HEVC DASH codec metadata

FFmpeg 7.1.5's DASH `set_codec_str` writes the four-character sample-entry tag
and has suffix handlers for AVC, AV1 and MPEG-4, but none for HEVC. Its bare
`hev1` output is therefore insufficient for Chromium's qualified HEVC parser.
This is metadata construction, not evidence that COPY changed the video.
Sources: [FFmpeg 7.1.5 dashenc.c](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavformat/dashenc.c#L326),
[Chromium HEVC codec parser](https://github.com/chromium/chromium/blob/main/media/base/video_codec_string_parsers.cc#L520),
[W3C HEVC registration](https://www.w3.org/TR/webcodecs-hevc-codec-registration/#fully-qualified-codec-strings).

`web_playback_codecs.py` derives the exact suffix from that representation's
`hvcC` configuration record: profile space/id, bit-reversed 32-bit compatibility
flags, tier/level, and ordered constraint bytes with trailing zero bytes omitted.
The `hev1` or `hvc1` prefix comes from the actual visual sample entry. It is
never changed to a different entry. Correct equivalent qualified strings remain
byte-for-byte intact; contradictory qualified strings reject publication.
The derivation follows the same fields as
[Shaka 5.3.3 parseHVCC](https://github.com/shaka-project/shaka-player/blob/v5.3.3/lib/util/mp4_box_parsers.js#L377)
and the configuration layout in
[FFmpeg's HEVC writer](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavformat/hevc.c).
V13's retained header `01022000000090000000000096...` yields
`hev1.2.4.L150.90`; browser decoding still requires separate actual acceptance.

The parser follows bounded ISO-BMFF box structure, not signature searches.
Files are at most 1 MiB, box traversal at most 128 boxes/eight levels, and init
must have one video track and one HEVC entry/configuration. It validates complete
configuration/NAL array lengths, unique bounded arrays, and VPS/SPS/PPS presence.
Malformed, ambiguous, encrypted or truncated structures fail closed. MPD parsing
has independent byte/node/depth bounds and rejects entities/external BaseURL or
xlink metadata. Representation IDs, fixed init filename and qualified track
indexes/IDs must agree with the known COPY recipe.

Production adds only the import and normalization call immediately before the
existing complete artifact validator. The helper requires the current process's
exact job token, actor/media version, RUNNING state, validating phase, nonsymlink
job directory and ownership marker. It changes only `manifest.mpd`, using a
bounded sibling temporary file and atomic replacement. Init, chunks, ASS, fonts,
packet evidence and original input are unchanged. Existing READY jobs are
ineligible. The complete timeline/resource validator, fresh authorization,
deadline/CPU checks and atomic READY transition still run afterwards.

An old READY artifact remains immutable and is not silently repaired on cache
hit. A separately authorized metadata-only qualification may copy its retained
bytes into a distinct owned partial, verify every byte hash/provenance and fresh
source authorization, run this helper and the complete validator, then qualify
that distinct composite. It must report this seam and zero new source/COPY jobs;
it is not a new full-input preparation proof or permission to mutate old READY.

Tests cover exact header derivation, compatibility order, profile spaces/tiers,
constraint zero handling, prefix preservation, malformed/ambiguous/bounded files,
owner/READY/phase rejection, conflicting metadata and track mapping. Integration
tests prove bare metadata is corrected before full validation and truncated DASH
still cannot become READY. Existing fake-worker fixtures now use a small
structurally valid hvcC init instead of the previous literal `b"init"` placeholder.
