# Canonical episode numbering

Updated October 12, 2026. Release titles and selected provider filenames share
`parse_episode_coverage` in `src/omakase/plus/coverage.py`. The contract maps
source numbering to title-local coverage. Parsing a claim proves neither that a
provider can deliver a file nor that Plex can read it.

| Exact AniList identity | Accepted target context | Absolute episodes | Local episodes | Recorded contract |
| --- | --- | --- | --- | --- |
| BLEACH: Thousand-Year Blood War, The Calamity, 185874 | Canonical cour 4; stored monitor season 2 or canonical season 4 | 41 through 50 | 1 through 10 | `bleach-tybw-cour4-absolute-v1` |
| That Time I Got Reincarnated as a Slime, season 4, 182205 | Target season 4 | 73 through 96 | 1 through 24 | `slime-s4-absolute-v1` |

BLEACH's source title must identify BLEACH and TYBW, Thousand-Year Blood War or
Sennen Kessen. Absolute 41 through 50 identifies cour 4. A local-only episode
requires an explicit cour 4, Calamity or Kashin-tan title claim. Thus `Bleach TYBW
41` maps to 1, and the canonical Kashin-tan `[07]` maps to 7. Bare `Bleach TYBW
[07]` remains ambiguous and contributes no coverage. Explicit competing season
or cour claims are rejected. Stored monitor season 2 is retained, not rewritten
into a franchise-wide numbering policy.

The October 12 extension accepts global S01E41 through S01E50 and S17E41
through S17E50 only inside the exact 185874 contract. It also accepts complete
parenthesized global ranges and versioned tokens such as E44v2 and S01E44v2.
Named older cours, fractional numbers, conflicting local/global annotations,
repeated episode claims and incomplete or unconsumed numeric suffixes fail
closed in both acquisition and standalone curator parsers. The first bounded
missing-gap search uses the romaji series title and global number. None of these
claims bypasses provider inventory or subtitle verification.

Slime's source title must identify the main series by its English or romaji
name. Absolute 73 through 96 maps with an offset of 72. Local-only numbering
requires an explicit season 4 claim. `4th Season - 04 ... | Episode 76` maps to
local 4. Matching local and absolute annotations agree; conflicting annotations
are rejected. This does not apply to Slime Diaries, specials or other seasons.

These contracts reject out-of-range values and ranges rather than clipping a
wrong cour into the target. Unknown, contradictory, fractional, preview and
extra claims cannot earn regular episode coverage. Other titles retain the
existing generic parser and the frozen MHA season 4 override.

## Primary evidence

- [Aniplex BLEACH announcement](https://www.aniplex.co.jp/lineup/bleach-anime/news/detail/?id=71195)
  identifies final global episodes 49 and 50, scheduled October 19 and 26. The
  exact AniList 185874 entry declares ten title-local episodes, with local 9
  next in the October 11 read-only audit. These establish the cour 4 offset 40.
- [Official Slime Blu-ray contents](https://www.ten-sura.com/anime/tensura/bd_dvd)
  explicitly lists season 4 volume 1 episodes 73 through 78 and volume 4 episodes
  91 through 96. [Official Slime episode guide](https://www.ten-sura.com/anime/tensura/story)
  identifies finale 96. Exact AniList 182205 declares 24 episodes. The offset 72
  is supported directly by official numbering, not only a sum of prior seasons.

## Sanitized mismatch evidence and repair boundary

The October 11 read-only audit found BLEACH monitor 974 with AniList 185874,
`RELEASING`, eight aired episodes, ten expected episodes and stored season 2.
Downloaded assets 47773 and 47772 advertised TYBW 41/Cour 4 Episode 1 and TYBW
42/Cour 4 Episode 2. Their planned coverage was local 1 and 2, but usable coverage
was empty, inventory verification was absent and errors were `inventory_mismatch`.
The latest inspected run also rejected a TYBW 41 release and canonical
Kashin-tan `[07]` and `[08]` releases as `NO_EPISODE_COVERAGE`.

Slime asset 271 advertised `4th Season - 04 ... | Episode 76`, with old planned
coverage 76 and verified usable coverage 4. Existing local-number precedence is
preserved and the shared canonical contract now resolves both annotations to 4.

Raw provider inventory filenames were not fetched in that audit. The filename
regressions are synthetic fixtures, not proof of the actual files. Existing
stored planned or usable values must not be reinterpreted as freshly verified
inventory merely because this parser changed.

For real-target readback, inspect only selected media filenames through the
normal authenticated provider inventory path, then run the ordinary reversible
rescan. Confirm exact monitor identity, selected file paths, normalized local
coverage, contract tag, inventory fingerprint/time, usable coverage and error
state. Provider access blocks, release identity, quality/subtitle checks and Plex
readability remain separate gates. Reconciliation still requires only aired
local 1 through 8 for this BLEACH audit; future local 9 and 10 do not become
required until the normal airing metadata says they aired. Do not claim those
future files are currently obtainable.

## Standalone Plex curator

`deploy/plex-curator/episode_parser.py` implements the same two bounded contracts
without requiring the Plus package on the curator host. Parity regressions compare
both parsers for accepted filenames, ranges and rejected identities. The curator
caller must pass the manifest's exact `anilist_id` and allowlisted
`numbering_override` to `parse_file_episode`; either missing or mismatched value
fails closed for these canonical identities. The returned Plex season remains the
manifest season. This parser does not choose series placement, change monitor
metadata, organize Plex collections or publish files on its own.
