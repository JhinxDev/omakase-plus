# Native Apple TV app

The native tvOS app is the recommended living-room client for Omakase Plus. It
talks to your own authenticated server and can play from Plex or directly from a
connected Real-Debrid account.

## What the app does

- Browse Anime, Movies and TV shelves, with Continue Watching and title details.
- On recent native builds, the Apple TV Home Screen Top Shelf combines activity
  across Anime, Movies and TV. Open the app after pairing to populate the shelf.
  It refreshes after supported playback saves.
- Pair a device with a one-time code from the web app; the device stays linked
  until you remove it in **Settings → Devices**.
- Play episodes and films with resume sync and a next-episode control. The
  AVPlayer path uses native controls. The direct-original candidate uses the
  TVVLCKit player with its own controls.
- Remember playback progress on the server, so a title resumes on the web, the
  Apple TV or another linked device.
- Finishing an episode or movie credits anime, TV and movie progress, History and
  Continue Watching order at the same 85 percent threshold as the web player,
  and queues the AniList sync through the durable tracker outbox for anime.
- TV and movie completions are stored with an Apple TV source detail, so History
  distinguishes them from Plex and manual records.
- Show subtitle tracks with an always-on preference in Settings. Available
  tracks depend on the media and playback transport.
- Open **Media Info** from the player controls on supported builds. When available,
  it shows resolution, video and audio codecs, container and file size.
- Prepare the next episode on the AVPlayer path so Next can start quickly.
  The direct-original candidate opens a new TVVLCKit item on Next.
- Refresh the current shelf with the circular-arrow button. Back returns from an
  episode to the series page before the shelf.
- After about ten seconds of waiting, the loading screen switches to a subtle
  looping animation instead of a plain spinner.

## Playback sources

The app follows the account's playback source setting:

- **Real-Debrid only** uses the connected Real-Debrid account and never calls
  Plex. Matched anime, movie and TV entries play from the Real-Debrid copy.
- **Plex only** uses the exact Plex copy and Plex playback.
- **Auto** prefers the Real-Debrid copy and falls back to Plex when needed.

No Plex server or NAS is required for the Real-Debrid path.

## Playback formats, subtitles and seeking

The direct-original candidate requests a signed HTTP range URL from a
compatible server. TVVLCKit can consume compatible original MKV media and
expose embedded ASS, PGS, and alternate audio tracks. Physical Apple TV
qualification for original-file seeking, track selection, and resume is still
pending. The app can also play the server's existing MP4 and HLS responses
through AVPlayer.

For the HLS path, the server probes each Real-Debrid video for embedded text subtitle tracks
(ASS, SRT, WebVTT and similar), converts them in the background and exposes them
as HLS subtitle tracks. The Apple TV shows its standard subtitle menu, and the
**Subtitles: Always on** setting selects English automatically when a title has
text subtitles.

Image-based PGS subtitles cannot be converted to text without OCR, so titles
that only ship PGS have no subtitle track on HLS.

The HLS path gives instant warm starts and a short seek within the
provider's ready window. For H.264 copies, Omakase can also build a seekable HLS
timeline from the file's keyframe index, which supports long jumps. HEVC copies
use exact six-second H.264 seek segments generated from the direct file, so
far-forward seeks work instead of erroring; preparing a far segment takes about
eight seconds and converts only that 720p window.

## Building and installing

The source lives in [tv-source](../tv-source). The macOS build host must have
full Xcode and the tvOS device SDK. From a committed checkout, run:

```bash
bash scripts/build-tvos.sh /absolute/empty-output-directory
```

The script builds the committed tvOS source, runs the portable Swift package
tests, and compiles both app schemes for arm64 Apple TV. It verifies that each
executable links its embedded TVVLCKit framework before packaging the
production app. The output directory contains the unsigned IPA, the dependency
license, and a JSON receipt with the source commit, Git tree, toolchain,
dependency digest, app version and IPA digest. The manual **Unsigned tvOS
build** GitHub workflow uses the same script with Xcode 26.3 on macOS 15.

A builder without a Git checkout can use a verified source snapshot. Create
the archive from the commit you intend to build:

```bash
git archive --format=tar --output=/absolute/native-source.tar HEAD tv-source scripts/build-tvos.sh
git rev-parse HEAD 'HEAD^{tree}'
shasum -a 256 /absolute/native-source.tar
```

Pass the recorded SHA-256, commit and tree to the remote build host alongside
the archive. Compare the SHA-256 before extracting the build script, then run:

```bash
bash scripts/build-tvos.sh --snapshot /absolute/native-source.tar SHA256 COMMIT TREE /absolute/empty-output-directory
```

The script verifies the archive digest and the embedded commit, and records
the Git tree supplied by the operator. It downloads the pinned VideoLAN
TVVLCKit 3.7.3 archive or accepts the same bytes through `TVVLCKIT_ARCHIVE`.
Both routes verify the exact size and SHA-256 before extraction. TVVLCKit is
dynamically linked and its archive provides the LGPL license bundled alongside
the IPA. Review the applicable redistribution and corresponding-source
obligations before distributing the artifact. The build defaults to an empty server
origin so viewers can enter their own server when pairing. A private build can
set the environment variable `OMAKASE_SERVER_ORIGIN` to an HTTPS origin. The
value is embedded in the IPA and is omitted from the build receipt.

Use the project's Apple TV signing flow to sign the production IPA for the
authorized device. Signing, installation and playback verification are
separate steps. Before installation, compare the app version and build in the
receipt with the installed device and the intended source commit. Check the
version and build again after installation.

### Direct-player UI regression

On the existing cloud Mac with an installed tvOS Simulator, run
`scripts/qualify-direct-controls.sh SIMULATOR_UDID OUTPUT_DIR`. The script copies
the project into a disposable directory, retains the exact production
`NativePlayer.swift`, and supplies deterministic session/track models and a
synthetic background. It checks control containment, overlap, collapsed track
choices, scrolling, selection and Siri Remote focus. It does not decode media,
contact a server, or prove physical playback. Review its XCTest screenshots as
well as assertions; build the full production app separately before installation.
