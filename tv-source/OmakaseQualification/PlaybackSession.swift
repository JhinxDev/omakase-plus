import AVFoundation
import Combine
import Foundation
import QualificationCore
import SwiftUI

/// Owns the current playback item and decides what may start playing.
///
/// Transport is delegated to a `PlayerEngine`: `AVPlayerEngine` keeps the existing
/// AVPlayer/AVKit behaviour for mp4 and HLS, `VLCPlayerEngine` handles provider
/// originals served as a direct HTTP range URL. The session itself never calls
/// `play()` from an asynchronous completion without first asking
/// `PlaybackLifecycle` whether playback is still wanted, which is what used to
/// resume audio after the viewer left the app.
@MainActor
final class PlaybackSession: ObservableObject {
    private enum NextEpisodeError: Error { case unavailable }
    private enum WaitPhase { case opening, buffering }

    /// Identity of one installed item. `generation` rejects callbacks from a
    /// replaced item; `intent` is the lifecycle token those callbacks must still
    /// hold before they are allowed to start playback.
    private struct SessionToken: Equatable {
        let generation: UUID
        let intent: UInt64
    }

    @Published private(set) var player: AVPlayer?
    @Published private(set) var status = "Idle"
    @Published private(set) var errorMessage: String?
    @Published private(set) var position: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var intro: LiveIntro?
    @Published private(set) var openingTimedOut = false
    /// True while an item is installed on either engine. Replaces "player != nil"
    /// as the presentation signal, because the VLC path has no AVPlayer.
    @Published private(set) var isPresenting = false
    /// Milliseconds from item install to the first playing frame.
    ///
    /// The server times the open, the grant and the first media GET that returned
    /// bytes. Only the client can time the wait after that: readiness, the seeded
    /// seek and the first decoded frame. Recorded per item and cleared on install.
    @Published private(set) var clientPlayingMilliseconds: Int?
    private var installedAt: ContinuousClock.Instant?
    @Published var canPlayNext = false
    @Published private(set) var directTracks: [EngineTrack] = []
    @Published private(set) var isDirectPlaybackRequested = false
    private(set) var requiresFreshMediaPass = false
    /// Non-nil while a direct (provider original) item is playing.
    private(set) var directEngine: VLCPlayerEngine?

    private let defaults: UserDefaults
    private var ledger: ResumeLedger
    private let resumeKey: String
    private let openingTimeout: Duration
    private var identity: ResumeIdentity?
    /// Characteristics whose selection was explicitly carried from the previous
    /// item, so the configured defaults must not overwrite them. Captured per item.
    private var itemCarriedSelections: Set<AVMediaCharacteristic> = []
    private var remoteFinished: ((Double?) -> Void)?
    private var liveCheckpoint: ((Double) -> Void)?
    private var engine: (any PlayerEngine)?
    private var lifecycle = PlaybackLifecycle() {
        didSet {
            let requested = lifecycle.mayStartPlayback(token: lifecycle.token)
            if isDirectPlaybackRequested != requested { isDirectPlaybackRequested = requested }
        }
    }
    private let trackSelector = TrackSelector()
    private var tracks = TrackSelectionLedger()
    private let seekValidator = SeekValidator()
    private var progressTask: Task<Void, Never>?
    private var generation = UUID()
    private var intentToken: UInt64 = 0
    private var started = false
    private var readyHandled = false
    private var canCheckpoint = false
    private var reachedEnd = false
    private var lastSave: TimeInterval = 0
    private var expectedDuration: Double?
    private var resumeTarget: Double?
    private var seekReadiness: Task<Void, Never>?
    private var playbackWait: Task<Void, Never>?
    private var mediaCookies: [HTTPCookie] = []
    /// True while the app's own default track selection is running, so an
    /// AVKit selection notification from that selection is not recorded as a
    /// viewer choice.
    private var applyingDefaults = false

    init(defaults: UserDefaults = .standard, resumeKey: String = "qualification.resume.v1",
         openingTimeout: Duration = .seconds(45)) {
        self.defaults = defaults
        self.resumeKey = resumeKey
        self.openingTimeout = openingTimeout
        ledger = ResumeLedger.decode(defaults.data(forKey: resumeKey) ?? Data())
    }

    private var sessionToken: SessionToken {
        SessionToken(generation: generation, intent: intentToken)
    }

    private func isCurrent(_ token: SessionToken) -> Bool { generation == token.generation }

    func resumeRecord(for sample: QualificationSample) -> ResumeRecord? {
        resumeRecord(for: sample.media.resumeIdentity)
    }

    func resumeRecord(for identity: ResumeIdentity) -> ResumeRecord? {
        ledger.record(for: identity, now: Date().timeIntervalSince1970)
    }

    func clearFailure() {
        requiresFreshMediaPass = false
        openingTimedOut = false
        errorMessage = nil
    }

    private(set) var lastDiagnostic: PlaybackDiagnostic?

    /// One structured snapshot for the server, built through the portable
    /// mapping. The AVPlayer branch carries allowlisted AVFoundation facts; the
    /// VLC branch carries numbers and an allowlisted transport label only, never
    /// a URL, header or cookie.
    func diagnostic(trigger: PlaybackDiagnostic.Trigger = .state) -> PlaybackDiagnostic {
        if let av = engine as? AVPlayerEngine {
            return av.diagnosticSnapshot(trigger: trigger)
        }
        guard let engine else { return PlaybackDiagnostic(engine: .none, trigger: trigger) }
        let ranges = engine.ranges
        return PlaybackDiagnostic(
            engine: .vlc,
            trigger: trigger,
            timeControlStatus: engine.state.diagnosticLabel,
            currentTime: engine.currentTime.isFinite ? engine.currentTime : nil,
            loadedRanges: ranges.buffered.map {
                PlaybackDiagnostic.TimeRange(start: $0.start, duration: $0.end - $0.start)
            },
            seekableRanges: ranges.seekable.map {
                PlaybackDiagnostic.TimeRange(start: $0.start, duration: $0.end - $0.start)
            }
        )
    }

    private func fail(_ message: String, requiresFreshMediaPass: Bool = false) {
        lastDiagnostic = diagnostic(trigger: .error)
        self.requiresFreshMediaPass = requiresFreshMediaPass
        errorMessage = message
        status = message
        stop(preserveStatus: true)
    }

    /// Call only from an explicit operator Play/Resume action.
    @discardableResult
    func begin(sample: QualificationSample, resume: Bool) -> Bool {
        begin(media: sample.media, resume: resume)
    }

    @discardableResult
    func begin(media: PlaybackMedia, resume: Bool, card: LibraryCard? = nil) -> Bool {
        stop()
        let resumePosition = resume ? resumeRecord(for: media.resumeIdentity).flatMap { record in
            !record.reachedEnd && record.position > 0 && record.position < record.duration ? record.position : nil
        } : nil
        guard !resume || resumePosition != nil else {
            fail("Saved position unavailable. Choose Play from start.")
            return false
        }
        return begin(url: media.url, resumePosition: resumePosition, identity: media.resumeIdentity, card: card)
    }

    func beginRemote(url: URL, position: Double, finished: @escaping (Double?) -> Void) -> Bool {
        guard begin(url: url, resumePosition: position > 0 ? position : nil, identity: nil) else { return false }
        remoteFinished = finished
        return true
    }

    func beginLive(_ pass: LiveTestPass, resumePosition: Double?,
                   startPaused: Bool = false,
                   onCheckpoint: ((Double) -> Void)? = nil) -> Bool {
        if pass.isDirect {
            guard beginDirect(pass, resumePosition: resumePosition) else { return false }
            liveCheckpoint = onCheckpoint
            return true
        }
        guard begin(url: pass.mediaURL(forResumeAt: resumePosition), resumePosition: resumePosition,
                    identity: pass.card.identity.resumeIdentity, cookies: pass.mediaCookies,
                    expectedDuration: pass.isGrowingHLS ? pass.duration : nil, card: pass.card,
                    startPaused: startPaused, intro: pass.intro)
        else { return false }
        liveCheckpoint = onCheckpoint
        return true
    }

    private func begin(url: URL, resumePosition: Double?, identity: ResumeIdentity?,
                       cookies: [HTTPCookie] = [], expectedDuration: Double? = nil,
                       card: LibraryCard? = nil, startPaused: Bool = false,
                       intro: LiveIntro? = nil) -> Bool {
        stop()
        clearFailure()
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            fail("Audio session failed (\(Self.failureCode(error)))")
            return false
        }
        let item = makeItem(url: url, cookies: cookies, card: card)
        let player = AVPlayer()
        player.allowsExternalPlayback = false
        let engine = AVPlayerEngine(player: player, item: item)
        install(engine: engine, avPlayer: player, resumePosition: resumePosition, identity: identity,
                cookies: cookies, expectedDuration: expectedDuration,
                carried: itemCarriedSelections, startPaused: startPaused, intro: intro)
        itemCarriedSelections = []
        if let resumePosition, resumePosition.isFinite, resumePosition > 0,
           url.pathExtension.lowercased() == "m3u8" {
            // Seed native HLS before attaching the item so AVFoundation can
            // request media near the saved position. Readiness still checks
            // where the seek landed before allowing playback or checkpoints.
            item.seek(to: CMTime(seconds: resumePosition, preferredTimescale: 600),
                      toleranceBefore: .zero, toleranceAfter: .zero, completionHandler: nil)
        }
        if player.currentItem !== item { player.replaceCurrentItem(with: item) }
        return true
    }

    /// Starts a provider original on the VLC engine.
    ///
    /// The URL and headers are secrets: nothing here is logged, and a header the
    /// engine cannot attach fails the start instead of being ignored.
    private func beginDirect(_ pass: LiveTestPass, resumePosition: Double?) -> Bool {
        stop()
        clearFailure()
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            fail("Audio session failed (\(Self.failureCode(error)))")
            return false
        }
        let engine: VLCPlayerEngine
        do {
            engine = try VLCPlayerEngine(url: pass.mediaURL, headers: pass.mediaHeaders)
        } catch let failure as EngineFailure {
            fail(failure.kind == .unsupported
                 ? "This title needs a player this build does not include."
                 : "Playback failed (\(failure.kind.rawValue):\(failure.code))")
            return false
        } catch {
            fail("Playback failed (\(Self.failureCode(error)))")
            return false
        }
        install(engine: engine, avPlayer: nil, resumePosition: resumePosition,
                identity: pass.card.identity.resumeIdentity, cookies: [],
                expectedDuration: pass.duration, carried: [], requiresPlayToOpen: true)
        return true
    }

    func replaceLive(with pass: LiveTestPass, resumePosition: Double? = nil,
                     keepPaused: Bool = false,
                     onCheckpoint: ((Double) -> Void)? = nil) async throws {
        guard Date() < pass.expiresAt else { throw NextEpisodeError.unavailable }
        if pass.isDirect || directEngine != nil {
            // A series normally keeps one transport. Replacing across engines is
            // not supported: close this item and reopen the next episode instead.
            guard pass.isDirect, directEngine != nil else { throw NextEpisodeError.unavailable }
            try await replaceDirect(pass)
        } else {
            // `resumePosition` is nil for an episode transition, and set only when
            // a reclaimed media plan is reopened at the viewer's saved position.
            try await replace(url: pass.mediaURL(forResumeAt: resumePosition),
                              identity: pass.card.identity.resumeIdentity,
                              cookies: pass.mediaCookies,
                              expectedDuration: pass.isGrowingHLS ? pass.duration : nil, card: pass.card,
                              resumePosition: resumePosition, keepPaused: keepPaused, intro: pass.intro)
        }
        liveCheckpoint = onCheckpoint
    }

    func replace(with media: PlaybackMedia, card: LibraryCard) async throws {
        try await replace(url: media.url, identity: media.resumeIdentity, card: card)
    }

    private func replace(url: URL, identity: ResumeIdentity, cookies: [HTTPCookie] = [],
                         expectedDuration: Double? = nil, card: LibraryCard,
                         resumePosition: Double? = nil,
                         keepPaused: Bool = false, intro: LiveIntro? = nil) async throws {
        try Task.checkCancellation()
        guard let previousItem = (engine as? AVPlayerEngine)?.item else { throw CancellationError() }
        let currentToken = sessionToken
        let wasPaused = isPausedByViewer
        let item = makeItem(url: url, cookies: cookies, card: card)
        if let resumePosition, resumePosition.isFinite, resumePosition > 0,
           url.pathExtension.lowercased() == "m3u8" {
            // Seed the saved position before the item is attached to any player.
            // Attaching first let AVFoundation begin from zero and the seeded seek
            // arrived too late to change where the item actually opened.
            item.seek(to: CMTime(seconds: resumePosition, preferredTimescale: 600),
                      toleranceBefore: .zero, toleranceAfter: .zero, completionHandler: nil)
        }
        let successor = AVPlayer()
        successor.allowsExternalPlayback = false
        successor.replaceCurrentItem(with: item)
        let deadline = ContinuousClock.now + .seconds(15)
        var installed = false
        defer {
            if !installed {
                item.asset.cancelLoading()
                successor.replaceCurrentItem(with: nil)
                for cookie in cookies { HTTPCookieStorage.shared.deleteCookie(cookie) }
            }
        }
        // AVFoundation starts preparing the item once it is associated with a player.
        // Leave the current episode and its checkpoint intact until the new one is ready.
        while item.status == .unknown && ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(100))
            guard isCurrent(currentToken) else { throw CancellationError() }
        }
        try Task.checkCancellation()
        guard item.status == .readyToPlay else { throw NextEpisodeError.unavailable }
        var carried: Set<AVMediaCharacteristic> = []
        if await carryTrackChoice(.audible, from: previousItem, to: item) { carried.insert(.audible) }
        if await carryTrackChoice(.legible, from: previousItem, to: item) { carried.insert(.legible) }
        try Task.checkCancellation()
        guard isCurrent(currentToken) else { throw CancellationError() }
        guard item.status == .readyToPlay else { throw NextEpisodeError.unavailable }
        // Deliberate: the successor replaces the current item only after the
        // lifecycle has accepted it. A backgrounded app installs the item but the
        // readiness completion cannot start playing it.
        let pauseOnInstall = keepPaused || (!wasPaused && isPausedByViewer)
        detachCurrentPlayback()
        clearFailure()
        let successorEngine = AVPlayerEngine(player: successor, item: item)
        // keepPaused is carried as install-time intent, not as an engine pause after
        // the fact: a readiness or seek completion would otherwise start playback.
        install(engine: successorEngine, avPlayer: successor, resumePosition: resumePosition,
                identity: identity, cookies: cookies, expectedDuration: expectedDuration,
                carried: carried, startPaused: pauseOnInstall, intro: intro)
        installed = true
    }

    /// Replaces a direct item with the next episode on a fresh VLC engine.
    private func replaceDirect(_ pass: LiveTestPass) async throws {
        try Task.checkCancellation()
        let currentToken = sessionToken
        let engine: VLCPlayerEngine
        do {
            engine = try VLCPlayerEngine(url: pass.mediaURL, headers: pass.mediaHeaders)
        } catch let failure as EngineFailure {
            throw failure
        }
        try Task.checkCancellation()
        guard isCurrent(currentToken) else { throw CancellationError() }
        detachCurrentPlayback(preserveTrackChoices: true)
        clearFailure()
        install(engine: engine, avPlayer: nil, resumePosition: nil,
                identity: pass.card.identity.resumeIdentity, cookies: [],
                expectedDuration: pass.duration, carried: [], requiresPlayToOpen: true)
    }

    private func carryTrackChoice(_ characteristic: AVMediaCharacteristic, from previous: AVPlayerItem,
                                  to next: AVPlayerItem) async -> Bool {
        guard !Task.isCancelled,
              let oldGroup = try? await previous.asset.loadMediaSelectionGroup(for: characteristic),
              !previous.currentMediaSelection.mediaSelectionCriteriaCanBeAppliedAutomatically(to: oldGroup),
              let newGroup = try? await next.asset.loadMediaSelectionGroup(for: characteristic),
              !Task.isCancelled else { return false }

        let selection = previous.currentMediaSelection
        guard !selection.mediaSelectionCriteriaCanBeAppliedAutomatically(to: oldGroup) else { return false }
        guard let chosen = selection.selectedMediaOption(in: oldGroup) else {
            if characteristic == .legible, newGroup.allowsEmptySelection {
                // An explicit Off is a choice the defaults must not turn back on.
                next.select(nil, in: newGroup)
                return true
            }
            return false
        }
        guard let tag = chosen.extendedLanguageTag?.lowercased(), !tag.isEmpty, tag != "und" else { return false }

        // A language alone can match both full and forced captions, or a commentary track.
        let traits: [AVMediaCharacteristic] = [
            .containsOnlyForcedSubtitles, .transcribesSpokenDialogForAccessibility,
            .describesMusicAndSoundForAccessibility, .describesVideoForAccessibility, .easyToRead,
        ]
        let matches = newGroup.options.filter { option in
            option.isPlayable && option.extendedLanguageTag?.lowercased() == tag
                && option.displayName == chosen.displayName
                && traits.allSatisfy { option.hasMediaCharacteristic($0) == chosen.hasMediaCharacteristic($0) }
        }
        guard matches.count == 1 else { return false }
        next.select(matches[0], in: newGroup)
        return true
    }

    private func makeItem(url: URL, cookies: [HTTPCookie], card: LibraryCard?) -> AVPlayerItem {
        // HLS makes separate playlist and segment requests. Cookies stay path-scoped to this pass.
        for cookie in cookies { HTTPCookieStorage.shared.setCookie(cookie) }
        let options: [String: Any]? = cookies.isEmpty ? nil : [AVURLAssetHTTPCookiesKey: cookies]
        let item = AVPlayerItem(asset: AVURLAsset(url: url, options: options))
        if let card {
            let metadata: [(AVMetadataIdentifier, String?)] = [
                (.commonIdentifierTitle, card.title), (.iTunesMetadataTrackSubTitle, card.episodeTitle),
            ]
            item.externalMetadata = metadata.compactMap { identifier, value in
                guard let value else { return nil }
                let entry = AVMutableMetadataItem()
                entry.identifier = identifier
                entry.value = value as NSString
                entry.extendedLanguageTag = "und"
                return entry
            }
        }
        return item
    }

    func install(engine: any PlayerEngine, avPlayer: AVPlayer?, resumePosition: Double?,
                 identity: ResumeIdentity?, cookies: [HTTPCookie], expectedDuration: Double?,
                 carried: Set<AVMediaCharacteristic>, requiresPlayToOpen: Bool = false,
                 startPaused: Bool = false, intro: LiveIntro? = nil) {
        self.identity = identity
        itemCarriedSelections = carried
        self.expectedDuration = expectedDuration
        resumeTarget = resumePosition
        position = 0
        duration = 0
        self.intro = intro
        reachedEnd = false
        started = false
        readyHandled = false
        canCheckpoint = false
        lastSave = 0
        status = "Loading"
        clientPlayingMilliseconds = nil
        installedAt = .now
        directEngine = engine as? VLCPlayerEngine
        mediaCookies = cookies
        generation = UUID()
        intentToken = lifecycle.beginOpening()
        if startPaused {
            // Declare the viewer's pause before readiness can complete, so neither a
            // readiness nor a seek completion may start playing on its own.
            lifecycle.markPausedByUser()
        }
        tracks.beginItem(intentToken)
        self.engine = engine
        player = avPlayer
        isPresenting = true
        let token = sessionToken
        engine.onEvent = { [weak self] event in self?.handle(event, token: token) }
        startProgressTask(token: token)
        if lifecycle.mayStartPlayback(token: token.intent) {
            monitorWait(.opening, token: token)
            // libVLC starts opening only after its first play command.
            if requiresPlayToOpen { engine.play() }
        } else {
            status = "Paused"
        }
    }

    // MARK: - Engine events

    private func handle(_ event: PlayerEvent, token: SessionToken) {
        guard isCurrent(token) else { return }
        switch event {
        case .ready:
            guard !readyHandled else { return }
            readyHandled = true
            if let av = engine as? AVPlayerEngine {
                let carried = itemCarriedSelections
                applyingDefaults = carried != [.audible, .legible]
                Task { @MainActor [weak self] in
                    defer { self?.applyingDefaults = false }
                    await self?.applyTrackPreferences(av.item, token: token, carried: carried)
                }
            } else {
                applyingDefaults = true
                directTracks = directEngine?.tracks ?? []
                applyDirectTrackDefaults(token: token)
                applyingDefaults = false
            }
            if lifecycle.mayStartPlayback(token: token.intent) {
                startReadyItem(token: token)
            }
        case .failed(let failure):
            fail("Playback failed (\(failure.kind.rawValue):\(failure.code))", requiresFreshMediaPass: true)
        case .ended:
            updateProgress()
            reachedEnd = true
            cancelPlaybackWait()
            status = "End reached. Human review pending."
            lifecycle.markEnded()
            saveProgress()
        case .stateChanged(let state):
            applyEngineState(state, token: token)
        case .tracksChanged:
            if let directEngine {
                directTracks = directEngine.tracks
                applyDirectTrackDefaults(token: token)
            } else if !applyingDefaults {
                recordCurrentSelection()
            }
        }
    }

    private func applyEngineState(_ state: PlayerState, token: SessionToken) {
        guard isCurrent(token), let engine, engine.state == state else { return }
        // AVKit drives the AVPlayer transport directly. A depleted HLS item can
        // wait for data instead of reaching .playing when the viewer presses Play.
        if (state == .playing || state == .buffering),
           let player, player.timeControlStatus != .paused,
           isPausedByViewer, !lifecycle.isBackgrounded, !reachedEnd,
           onResumeRequested?() == true {
            engine.pause()
            return
        }
        switch state {
        case .playing:
            // AVKit can drive AVPlayer directly. Read its current transport
            // state before accepting an explicit Play after a pause.
            let avKitPlaying = player?.timeControlStatus == .playing
            let avKitResumed = avKitPlaying && !lifecycle.isBackgrounded
                && lifecycle.intent == .pausedByUser && !reachedEnd
            guard lifecycle.mayStartPlayback(token: token.intent) || avKitResumed else {
                engine.pause()
                return
            }
            // libVLC can already report Playing while its initial resume seek
            // is pending. Keep the opening deadline and status in that case.
            guard started || avKitResumed else { return }
            cancelPlaybackWait()
            started = true
            lifecycle.markPlaying()
            status = "Playing"
            recordClientPlaying()
        case .paused:
            guard started, !reachedEnd else { return }
            cancelPlaybackWait()
            status = "Paused"
            lifecycle.markPausedByUser()
            if liveCheckpoint != nil {
                updateProgress()
                saveProgress()
            }
        case .buffering:
            guard started, !reachedEnd, lifecycle.mayStartPlayback(token: token.intent) else { return }
            status = "Buffering"
            monitorWait(.buffering, token: token)
        case .idle, .opening, .ready, .ended, .failed:
            break
        }
    }

    private func monitorWait(_ phase: WaitPhase, token: SessionToken) {
        guard playbackWait == nil else { return }
        let limit = phase == .opening ? openingTimeout : .seconds(30)
        playbackWait = Task { @MainActor [weak self] in
            do { try await Task.sleep(for: limit) } catch { return }
            guard let self, self.isCurrent(token), !Task.isCancelled, let engine = self.engine else { return }
            self.playbackWait = nil
            switch phase {
            case .opening where !self.started:
                self.openingTimedOut = true
                self.fail("The video took too long to open.", requiresFreshMediaPass: true)
            case .buffering where self.started && !self.reachedEnd && engine.state == .buffering:
                self.fail("Playback stalled while waiting for data.", requiresFreshMediaPass: true)
            default: break
            }
        }
    }

    private func cancelPlaybackWait() {
        playbackWait?.cancel()
        playbackWait = nil
    }

    /// The only place playback starts. Asks the lifecycle first, so a readiness,
    /// seek or replacement completion that arrives after the viewer left cannot
    /// call play().
    private func beginPlaying(_ engine: any PlayerEngine, token: SessionToken) {
        cancelPlaybackWait()
        guard isCurrent(token) else { return }
        guard lifecycle.mayStartPlayback(token: token.intent) else {
            // Keep the prepared item so an explicit Play resumes the same place.
            status = lifecycle.isBackgrounded ? "Paused" : status
            return
        }
        started = true
        status = "Buffering"
        engine.play()
        if engine.state == .playing {
            applyEngineState(.playing, token: token)
        }
    }

    /// Records the client's own install-to-playing time once per item.
    private func recordClientPlaying() {
        guard clientPlayingMilliseconds == nil, let installedAt else { return }
        let elapsed = ContinuousClock.now - installedAt
        clientPlayingMilliseconds = Int(elapsed.components.seconds) * 1000
            + Int(elapsed.components.attoseconds / 1_000_000_000_000_000)
    }

    private func startReadyItem(token: SessionToken) {
        guard let engine, isCurrent(token) else { return }
        if expectedDuration != nil, let av = engine as? AVPlayerEngine {
            let requested = resumeTarget ?? 0
            let item = av.item
            // A growing EVENT playlist otherwise starts near its live edge.
            status = resumeTarget == nil ? "Preparing playback" : "Preparing saved position"
            seekReadiness = Task { @MainActor [weak self] in
                for _ in 0..<60 {
                    guard let self, self.isCurrent(token), !Task.isCancelled else { return }
                    if let range = item.seekableTimeRanges.map(\.timeRangeValue).first(where: {
                        $0.start.seconds <= (self.resumeTarget == nil ? 0.12 : requested)
                            && $0.end.seconds > requested
                    }) {
                        self.issueSeek(engine, target: max(range.start.seconds, requested), attempt: 1,
                                       token: token, duration: self.expectedDuration ?? engine.duration)
                        return
                    }
                    try? await Task.sleep(for: .milliseconds(500))
                }
                guard let self, self.isCurrent(token), !Task.isCancelled else { return }
                self.fail(self.resumeTarget == nil ? "The beginning is still preparing. Try again." :
                    "The saved position is still preparing. Try Resume again or play from start.")
            }
            return
        }
        let length = expectedDuration ?? engine.duration
        if let saved = resumeTarget {
            guard length.isFinite, length > 0, saved < length else {
                // A direct item may report its length late; a resume request is
                // still honoured because the validator checks the landing.
                if length.isFinite, length > 0 {
                    fail("Saved position is outside this clip. Choose Play from start.")
                    return
                }
                resumeTarget = nil
                issueSeek(engine, target: saved, attempt: 1, token: token, duration: length)
                return
            }
            issueSeek(engine, target: saved, attempt: 1, token: token, duration: length)
        } else {
            beginPlaying(engine, token: token)
        }
    }

    /// Issues one seek attempt and validates where it landed.
    private func issueSeek(_ engine: any PlayerEngine, target: Double, attempt: Int,
                           token: SessionToken, duration: Double) {
        status = target < 0.12 ? "Seeking to the beginning" : "Seeking to saved position"
        let tolerance: SeekTolerance = attempt == 1 ? SeekValidator.firstAttemptTolerance : .bounded
        engine.seek(to: target, tolerance: tolerance) { [weak self] landing in
            guard let self, self.isCurrent(token), let current = self.engine, current === engine,
                  self.lifecycle.mayStartPlayback(token: token.intent) else { return }
            let verdict = self.seekValidator.evaluate(landing, target: target,
                                                      duration: duration.isFinite ? duration : nil,
                                                      attempt: attempt)
            switch verdict {
            case .accepted:
                self.resumeTarget = nil
                self.beginPlaying(engine, token: token)
            case let .retry(_, nextAttempt, _):
                self.issueSeek(engine, target: target, attempt: nextAttempt, token: token, duration: duration)
            case let .failed(failure):
                self.fail(SeekValidator.recoveryMessage(for: failure))
            }
        }
    }

    // MARK: - Track preferences

    private func applyTrackPreferences(_ item: AVPlayerItem, token: SessionToken,
                                       carried: Set<AVMediaCharacteristic>) async {
        // Defaults describe how a fresh item should start. A choice carried from
        // the previous episode is an explicit decision and must win, including an
        // explicit Off, or the next episode silently reverts to the configured
        // language. The revision snapshot stops a late default from overwriting a
        // choice the viewer makes while this task is awaiting.
        let revision = tracks.revision()
        let userDefaults = UserDefaults.standard
        if !carried.contains(.audible) {
            let audio = userDefaults.string(forKey: "omakase.tv.audio-language") ?? "auto"
            if audio != "auto", tracks.mayApplyDefault(kind: .audio, revision: revision) {
                await selectLanguage(audio, characteristic: .audible, item: item, token: token,
                                     revision: revision)
            }
        }
        guard !carried.contains(.legible), tracks.mayApplyDefault(kind: .subtitle, revision: revision)
        else { return }
        let always = userDefaults.object(forKey: "omakase.tv.subtitles-always") as? Bool ?? true
        guard always else { return }
        let subtitles = userDefaults.string(forKey: "omakase.tv.subtitle-language") ?? "en"
        await selectLanguage(subtitles == "auto" ? "en" : subtitles, characteristic: .legible,
                             item: item, token: token, revision: revision)
    }

    private func selectLanguage(_ language: String, characteristic: AVMediaCharacteristic,
                                item: AVPlayerItem, token: SessionToken,
                                revision: TrackSelectionLedger.Revision) async {
        _ = try? await item.asset.load(.availableMediaCharacteristicsWithMediaSelectionOptions)
        let kind: TrackKind = characteristic == .audible ? .audio : .subtitle
        let deadline = Date().addingTimeInterval(6)
        while Date() < deadline {
            guard isCurrent(token), tracks.mayApplyDefault(kind: kind, revision: revision) else { return }
            if let group = item.asset.mediaSelectionGroup(forMediaCharacteristic: characteristic) {
                let options = group.options.filter { option in
                    characteristic != .legible || !option.hasMediaCharacteristic(.containsOnlyForcedSubtitles)
                }
                let preferred = options.first { option in
                    if let tag = option.extendedLanguageTag, tag.lowercased().hasPrefix(language.lowercased()) {
                        return true
                    }
                    if let locale = option.locale, locale.identifier.lowercased().hasPrefix(language.lowercased()) {
                        return true
                    }
                    return option.displayName.localizedCaseInsensitiveContains(language)
                } ?? (characteristic == .legible ? options.first : nil)
                if let preferred {
                    guard isCurrent(token), tracks.mayApplyDefault(kind: kind, revision: revision) else { return }
                    item.select(preferred, in: group)
                    return
                }
            }
            try? await Task.sleep(for: .milliseconds(150))
        }
    }

    /// Applies configured or carried preferences to a VLC item.
    private func applyDirectTrackDefaults(token: SessionToken) {
        guard let engine = directEngine, isCurrent(token) else { return }
        let revision = tracks.revision()
        let userDefaults = UserDefaults.standard
        for kind in TrackKind.allCases {
            if let carry = tracks.carry(for: kind) {
                apply(carry, kind: kind, engine: engine)
                continue
            }
            guard tracks.mayApplyDefault(kind: kind, revision: revision) else { continue }
            let preference = configuredPreference(for: kind, defaults: userDefaults)
            guard preference != .automatic else { continue }
            let selection = trackSelector.select(from: engine.tracks, kind: kind, preference: preference)
            switch selection {
            case .selected(let track):
                guard tracks.mayApplyDefault(kind: kind, revision: revision) else { continue }
                engine.selectTrack(kind: kind, id: track.id)
            case .off:
                guard tracks.mayApplyDefault(kind: kind, revision: revision) else { continue }
                engine.selectTrack(kind: kind, id: nil)
            case .none:
                break
            }
        }
    }

    private func configuredPreference(for kind: TrackKind,
                                      defaults userDefaults: UserDefaults) -> TrackPreference {
        switch kind {
        case .audio:
            let language = userDefaults.string(forKey: "omakase.tv.audio-language") ?? "auto"
            return language == "auto" ? .automatic : .language(language)
        case .subtitle:
            let always = userDefaults.object(forKey: "omakase.tv.subtitles-always") as? Bool ?? true
            guard always else { return .automatic }
            let language = userDefaults.string(forKey: "omakase.tv.subtitle-language") ?? "en"
            return .language(language == "auto" ? "en" : language)
        }
    }

    private func apply(_ choice: TrackChoice, kind: TrackKind, engine: any PlayerEngine) {
        switch choice {
        case .off:
            engine.selectTrack(kind: kind, id: nil)
        case .track(let id):
            engine.selectTrack(kind: kind, id: id)
        }
    }

    /// Records a viewer's AVKit menu choice so the next episode carries it.
    private func recordCurrentSelection() {
        guard started, let av = engine as? AVPlayerEngine else { return }
        for kind in TrackKind.allCases {
            if let id = av.selectedTrackID(kind: kind) {
                tracks.recordExplicit(kind: kind, choice: .track(id), itemToken: intentToken)
            } else if kind == .subtitle,
                      let group = av.item.asset.mediaSelectionGroup(forMediaCharacteristic: .legible),
                      group.allowsEmptySelection {
                tracks.recordExplicit(kind: kind, choice: .off, itemToken: intentToken)
            }
        }
    }

    /// Viewer-initiated track choice, used by any in-app control and by tests.
    @discardableResult
    func selectTrack(kind: TrackKind, id: String?) -> Bool {
        guard let engine, engine.selectTrack(kind: kind, id: id) else { return false }
        tracks.recordExplicit(kind: kind, choice: id.map(TrackChoice.track) ?? .off, itemToken: intentToken)
        objectWillChange.send()
        return true
    }

    // MARK: - Progress

    private func startProgressTask(token: SessionToken) {
        progressTask?.cancel()
        progressTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, self.isCurrent(token), !Task.isCancelled else { return }
                self.updateProgress()
                if Date().timeIntervalSince1970 - self.lastSave >= 5 { self.saveProgress() }
            }
        }
    }

    private func updateProgress() {
        guard started, let engine else { return }
        let current = engine.currentTime
        let length = expectedDuration ?? engine.duration
        guard current.isFinite, current >= 0, length.isFinite, length > 0 else { return }
        duration = length
        position = min(current, length)
        // An unopened or still-buffering restart must not replace a good resume with zero.
        canCheckpoint = canCheckpoint || position > 0
        if reachedEnd, duration > 0, position < duration - 2 { reachedEnd = false }
    }

    private func saveProgress() {
        guard started, canCheckpoint, let identity, duration > 0, position <= duration else { return }
        let now = Date().timeIntervalSince1970
        ledger.checkpoint(identity: identity, position: position, duration: duration, reachedEnd: reachedEnd, now: now)
        if let data = try? ledger.encoded() { defaults.set(data, forKey: resumeKey) }
        lastSave = now
        liveCheckpoint?(reachedEnd ? duration : position)
    }

    // MARK: - Scene phase and transport commands

    /// Applies a scene change. Only a real background pauses and checkpoints; a
    /// transient inactive scene (Control Center, power button, a system overlay)
    /// changes nothing, which is what the previous "phase != .active" check got
    /// wrong.
    func scenePhaseChanged(_ phase: AppScenePhase) {
        switch lifecycle.scene(phase) {
        case .pauseAndCheckpoint:
            // The stall/opening timer must not fail a backgrounded item.
            cancelPlaybackWait()
            engine?.pause()
            status = "Paused"
            updateProgress()
            saveProgress()
        case .none:
            break
        }
    }

    /// Pause when the app leaves the foreground, keeping the item and its pass.
    ///
    /// Opening Control Center with the TV/power button only makes the scene
    /// inactive; stopping there tore the player down, which is why the owner was
    /// taken out of what he was watching. This pauses and checkpoints without
    /// detaching, so returning resumes the same item at the same position. An
    /// explicit stop, a finished item or lost access still tears down.
    func pauseForBackground() {
        scenePhaseChanged(.background)
    }

    /// True when the viewer paused and has not resumed since.
    ///
    /// Read before a reclaimed media plan is reopened, so the recovery keeps the
    /// viewer's intent instead of starting playback on its own.
    var isPausedByViewer: Bool { lifecycle.intent == .pausedByUser }

    /// Takes over a viewer's Play request before the engine is asked to play.
    ///
    /// Returns true when the request was handled elsewhere, for example a media
    /// plan that has to be reopened at the saved position first. The presentation
    /// stays on screen while that happens.
    var onResumeRequested: (() -> Bool)?

    /// Play/pause from a remote command. Honours the lifecycle intent.
    func togglePlayPause() {
        guard let engine, isPresenting, !lifecycle.isBackgrounded, !reachedEnd else { return }
        let token = sessionToken
        if lifecycle.mayStartPlayback(token: token.intent) {
            lifecycle.markPausedByUser()
            cancelPlaybackWait()
            status = "Paused"
            engine.pause()
        } else {
            if onResumeRequested?() == true { return }
            lifecycle.markPlaying()
            if started {
                engine.play()
            } else if readyHandled {
                startReadyItem(token: token)
            } else {
                status = "Loading"
                monitorWait(.opening, token: token)
                engine.play()
            }
        }
    }

    /// Relative seek from a remote command. Validated by the same landing rules.
    func skip(by delta: Double) {
        guard let engine, isPresenting, duration.isFinite, duration > 0 else { return }
        let target = min(max(position + delta, 0), max(duration - 1, 0))
        engine.seek(to: target, tolerance: .keyframe) { [weak self] landing in
            guard let self, self.engine === engine, landing.landed != nil else { return }
            self.updateProgress()
        }
    }

    func skipIntro(_ marker: LiveIntro, on player: AVPlayer) {
        guard self.player === player, started, marker == intro,
              marker.contains(player.currentTime().seconds), marker.end < duration else { return }
        let target = CMTime(seconds: marker.end, preferredTimescale: 1_000)
        player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func stop(preserveStatus: Bool = false) {
        let hadPlayer = isPresenting
        detachCurrentPlayback()
        player = nil
        intro = nil
        isPresenting = false
        directEngine = nil
        canPlayNext = false
        if hadPlayer {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            if !preserveStatus { status = reachedEnd ? "End reached. Human review pending." : "Stopped; partial progress saved when duration was known" }
        }
    }

    func stopForLostAccess() {
        // Skip the final position sample when access has already ended.
        started = false
        fail("Playback access ended.", requiresFreshMediaPass: true)
    }

    private func detachCurrentPlayback(preserveTrackChoices: Bool = false) {
        engine?.pause()
        updateProgress()
        saveProgress()
        let finish = remoteFinished
        let checkpoint = started && canCheckpoint ? position : nil
        remoteFinished = nil
        liveCheckpoint = nil
        generation = UUID() // Reject queued callbacks and late seek completions.
        lifecycle.stop()
        cancelPlaybackWait()
        seekReadiness?.cancel()
        seekReadiness = nil
        progressTask?.cancel()
        progressTask = nil
        engine?.onEvent = nil
        engine?.stop()
        engine = nil
        directEngine = nil
        directTracks = []
        player = nil
        applyingDefaults = false
        for cookie in mediaCookies { HTTPCookieStorage.shared.deleteCookie(cookie) }
        mediaCookies.removeAll()
        identity = nil
        expectedDuration = nil
        resumeTarget = nil
        itemCarriedSelections = []
        started = false
        readyHandled = false
        canCheckpoint = false
        if !preserveTrackChoices { tracks = TrackSelectionLedger() }
        finish?(checkpoint)
    }

    nonisolated static func failureCode(_ error: Error?) -> String {
        guard let error else { return "unknown" }
        let ns = error as NSError
        let domain: String
        switch ns.domain {
        case NSURLErrorDomain: domain = "network"
        case AVFoundationErrorDomain: domain = "avfoundation"
        case NSOSStatusErrorDomain: domain = "osstatus"
        default: domain = "other"
        }
        return "\(domain):\(ns.code)"
    }

    nonisolated static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0, seconds < 360_000 else { return "Unknown" }
        let total = Int(seconds)
        return String(format: "%02d:%02d:%02d", total / 3600, (total / 60) % 60, total % 60)
    }

    nonisolated static func shouldSuggestNext(position: Double, duration: Double) -> Bool {
        duration.isFinite && duration > 0 && position <= duration
            && position >= duration - min(45, duration * 0.15)
    }
}

extension AppScenePhase {
    /// Maps SwiftUI's scene phase onto the portable lifecycle input. `.inactive`
    /// stays `.inactive`: it is presentation, not an exit.
    init(_ phase: ScenePhase) {
        switch phase {
        case .active: self = .active
        case .inactive: self = .inactive
        case .background: self = .background
        @unknown default: self = .inactive
        }
    }
}
