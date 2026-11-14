import AVFoundation
import Foundation
import QualificationCore
import XCTest
@testable import OmakaseQualification

// Apple-only tests are prepared, not executed or typechecked on Windows.
final class PlaybackSessionTests: XCTestCase {
    @MainActor
    func testStartupAndRepeatedStopDoNotCreateAPlayer() {
        let suite = "QualificationTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let session = PlaybackSession(defaults: defaults)
        XCTAssertNil(session.player)
        XCTAssertEqual(session.status, "Idle")
        session.stop()
        session.stop()
        XCTAssertNil(session.player)
        XCTAssertNil(defaults.data(forKey: "qualification.resume.v1"))
    }

    func testFailureCodeDropsURLsBodiesAndUnknownDomains() {
        let error = NSError(domain: "https://private.invalid/secret", code: 17,
                            userInfo: [NSLocalizedDescriptionKey: "token=secret"])
        XCTAssertEqual(PlaybackSession.failureCode(error), "other:17")
        let network = NSError(domain: NSURLErrorDomain, code: -1009,
                              userInfo: [NSURLErrorFailingURLStringErrorKey: "https://private.invalid/token"])
        XCTAssertEqual(PlaybackSession.failureCode(network), "network:-1009")
    }

    @MainActor
    func testDirectOpeningAfterBackgroundedStopWaitsForExplicitPlay() {
        let session = PlaybackSession()
        let engine = SessionEngine()
        defer { session.stop() }

        session.scenePhaseChanged(.background)
        session.stop()
        installDirect(engine, in: session)

        XCTAssertEqual(session.status, "Paused")
        XCTAssertEqual(engine.plays, 0)
        session.togglePlayPause()
        session.scenePhaseChanged(.active)
        XCTAssertEqual(engine.plays, 0)

        session.togglePlayPause()
        XCTAssertEqual(engine.plays, 1)
    }

    @MainActor
    func testLatePlayingCannotUndoExplicitPause() {
        let session = PlaybackSession()
        let engine = SessionEngine()
        defer { session.stop() }

        installDirect(engine, in: session)
        XCTAssertEqual(engine.plays, 1)
        session.togglePlayPause()
        XCTAssertEqual(session.status, "Paused")
        engine.deliverPlaying()
        XCTAssertEqual(session.status, "Paused")
        XCTAssertEqual(engine.pauses, 2)

        session.togglePlayPause()
        XCTAssertEqual(engine.plays, 2)
    }

    @MainActor
    func testLatePlayingAfterReturnDoesNotResumePlayer() {
        let session = PlaybackSession()
        let engine = SessionEngine()
        defer { session.stop() }

        installDirect(engine, in: session)
        session.scenePhaseChanged(.background)
        session.scenePhaseChanged(.active)
        engine.deliverPlaying()

        XCTAssertEqual(session.status, "Paused")
        XCTAssertEqual(engine.plays, 1)
        XCTAssertEqual(engine.pauses, 2)
    }

    @MainActor
    private func installDirect(_ engine: SessionEngine, in session: PlaybackSession) {
        session.install(engine: engine, avPlayer: nil, resumePosition: nil,
                        identity: nil, cookies: [], expectedDuration: nil, carried: [],
                        requiresPlayToOpen: true)
    }
}

@MainActor
private final class SessionEngine: PlayerEngine {
    var state: PlayerState = .opening
    var currentTime: Double { 0 }
    var duration: Double { 60 }
    var ranges: TransportRanges { TransportRanges() }
    var tracks: [EngineTrack] { [] }
    var hasVideoOutput: Bool { true }
    var onEvent: ((PlayerEvent) -> Void)?
    private(set) var plays = 0
    private(set) var pauses = 0

    func play() { plays += 1 }
    func pause() { pauses += 1 }
    func stop() {}

    func deliverPlaying() {
        state = .playing
        onEvent?(.stateChanged(.playing))
    }

    func seek(to seconds: Double, tolerance: SeekTolerance,
              completion: @escaping @MainActor (SeekLanding) -> Void) {
        Task { @MainActor in completion(SeekLanding(landed: nil, finished: false)) }
    }

    func selectTrack(kind: TrackKind, id: String?) -> Bool { false }
    func selectedTrackID(kind: TrackKind) -> String? { nil }
}
