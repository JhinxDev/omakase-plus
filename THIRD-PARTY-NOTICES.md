# Third-party notices

The application code retains the original MIT license and copyright attribution
in LICENSE. Changes do not remove upstream authorship or imply unaided authorship.

Geist Sans is distributed under SIL Open Font License 1.1. Its full license,
copyright and exact source record are retained beside the bundled font in
src/omakase/plus/static/GEIST-OFL.txt and GEIST-SOURCE.md.

Service logos are unmodified vendor identity assets. Their source URLs, hashes
and dimensions are recorded in src/omakase/plus/static/services/provenance.json.
Trademarks remain with their owners; display does not imply endorsement.

Product screenshots show sample accounts, progress, ratings and availability.
Catalog posters and artwork originate from the referenced metadata providers.
The MIT code license does not grant rights to those images, trademarks or media.
Keep their provenance/disclosure and review redistribution separately. No movie,
episode, provider account, tracking subscription or API key is bundled.

Python dependencies are resolved by uv.lock and the generated hashed requirement
files. Their own licenses apply. Distributing a wheel, executable or container
requires preserving applicable dependency notices; this source notice is not a
complete SPDX inventory or a grant covering optional third-party services.

The native tvOS build downloads and dynamically links TVVLCKit 3.7.3.
VideoLAN's [pinned binary archive](https://download.videolan.org/pub/cocoapods/prod/TVVLCKit-3.7.3-319ed2c0-79128878.tar.xz)
has SHA-256 `b5f90c226ed54d9dc1c03901c60dc7749b74a53caace2c3047e4c0b7a063e46c`.
The archive includes the GNU LGPL version 2.1 text in `COPYING.txt`, which
the build places alongside the unsigned IPA. TVVLCKit has its own license
and is outside this repository's MIT grant. [VideoLAN VLCKit source](https://code.videolan.org/videolan/VLCKit)

The tvOS qualification MP4 files are synthetic FFmpeg fixtures. The baseline
uses a test pattern and silence. The track fixture uses the same picture
with generated audio tones and timed text.
