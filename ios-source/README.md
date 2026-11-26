# Omakase Plus for iPhone

Native SwiftUI iPhone companion (`iOS 17+`, bundle ID `dev.omakase.plus.ios`, marketing version `0.7.1`) built on the shared [`QualificationCore`](../tv-source/QualificationCore) Swift package alongside the Apple TV app.

## Features

- **Persistent pairing & invite linking:** Connects via one-time 8-character pairing code (`POST /device?kind=iphone`) or `#library-...` invitation link. Credentials live in the iOS Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, retry gracefully if prewarmed while locked, confirm `401` responses against `/profile` before removing a saved profile, and slide device + web-session expiry forward on active use via `GET /device`.
- **Home & Edit Home:** Displays the shared Home banner and shelves, plus an **Edit Home** sheet (`PhoneHomeEditor`) and shelf context menu (`Hide this shelf`) synced per profile with the web app and Apple TV via `POST /home/layout`.
- **Library, Discover & Requests:** Browse Anime, Movies, and TV Shows, search and request new titles, and track request status with poster thumbnails and stage progress (`GET /requests`).
- **Native Playback:** `AVPlayerViewController` with Picture-in-Picture, AirPlay, configured audio/subtitle track defaults, Skip Intro, Next Episode, Media Info, and server-synced resume checkpoints.
- **Offline qualification shell:** Passing `-OmakaseSyntheticPrototype` launches the offline `PrototypeLibrary` (`dev.omakase.plus.iosprototype`) backed by `baseline.mp4` for deterministic Simulator UI testing.

## Local & CI Qualification

On a macOS host with Xcode (`26.3+`) and an iOS Simulator runtime:

```sh
bash scripts/qualify-ios.sh /absolute/path/to/new-ios-evidence
```

The [iOS simulator workflow](../.github/workflows/ios-simulator.yml) runs `QualificationCore` unit tests and `OmakasePlusPhoneUITests` (`PrototypeJourneyTests`) on PRs touching `ios-source/**` or `tv-source/QualificationCore/**`. On Windows/Linux, `cd tv-source/QualificationCore && swift test` verifies the shared Swift package directly.

## TestFlight Distribution

[`.github/workflows/ios-testflight.yml`](../.github/workflows/ios-testflight.yml) archives, signs, and uploads `OmakasePlusPhone` (`dev.omakase.plus.ios`) to App Store Connect / TestFlight via `scripts/deploy-testflight-ios.sh`.
