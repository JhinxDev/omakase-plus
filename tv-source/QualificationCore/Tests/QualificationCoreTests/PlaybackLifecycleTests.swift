import XCTest
@testable import QualificationCore

/// Truth table for the lifecycle repair: a completion may only call `play()`
/// while the viewer still wants this item, and a merely inactive scene must not
/// pause, cancel or invalidate anything.
final class PlaybackLifecycleTests: XCTestCase {
    func testLateReadinessAfterBackgroundCannotStartPlayback() {
        var lifecycle = PlaybackLifecycle()
        let token = lifecycle.beginOpening()
        XCTAssertTrue(lifecycle.mayStartPlayback(token: token))

        XCTAssertEqual(lifecycle.scene(.background), .pauseAndCheckpoint)
        XCTAssertFalse(lifecycle.mayStartPlayback(token: token))
        XCTAssertTrue(lifecycle.isCurrent(token))
        XCTAssertEqual(lifecycle.intent, .pausedByBackground)
    }

    func testInactiveIsTransientAndKeepsPlaybackAllowed() {
        var lifecycle = PlaybackLifecycle()
        let token = lifecycle.beginOpening()

        XCTAssertEqual(lifecycle.scene(.inactive), .none)
        XCTAssertEqual(lifecycle.intent, .opening)
        XCTAssertTrue(lifecycle.mayStartPlayback(token: token))

        lifecycle.markPlaying()
        XCTAssertEqual(lifecycle.scene(.inactive), .none)
        XCTAssertEqual(lifecycle.intent, .playing)
        XCTAssertTrue(lifecycle.mayStartPlayback(token: token))
    }

    func testReturningToForegroundRequiresAnExplicitPlay() {
        var lifecycle = PlaybackLifecycle()
        let token = lifecycle.beginOpening()
        lifecycle.markPlaying()
        _ = lifecycle.scene(.background)

        XCTAssertEqual(lifecycle.scene(.active), .none)
        XCTAssertEqual(lifecycle.intent, .pausedByUser)
        XCTAssertFalse(lifecycle.mayStartPlayback(token: token))
        XCTAssertFalse(lifecycle.isBackgrounded)
    }

    func testBackgroundWhilePausedStillCheckpointsOnce() {
        var lifecycle = PlaybackLifecycle()
        _ = lifecycle.beginOpening()
        lifecycle.markPausedByUser()
        XCTAssertEqual(lifecycle.scene(.background), .pauseAndCheckpoint)
        // A second background notification must not checkpoint again.
        XCTAssertEqual(lifecycle.scene(.background), .none)
    }

    func testBackgroundAfterEndOrStopDoesNothing() {
        var lifecycle = PlaybackLifecycle()
        _ = lifecycle.beginOpening()
        lifecycle.markPlaying()
        lifecycle.markEnded()
        XCTAssertEqual(lifecycle.scene(.background), .none)
        XCTAssertEqual(lifecycle.intent, .ended)

        lifecycle.stop()
        XCTAssertFalse(lifecycle.mayStartPlayback(token: lifecycle.token))
        XCTAssertEqual(lifecycle.scene(.background), .none)
    }

    func testNewItemInvalidatesOlderCompletions() {
        var lifecycle = PlaybackLifecycle()
        let first = lifecycle.beginOpening()
        let second = lifecycle.beginOpening()
        XCTAssertNotEqual(first, second)
        XCTAssertFalse(lifecycle.mayStartPlayback(token: first))
        XCTAssertFalse(lifecycle.isCurrent(first))
        XCTAssertTrue(lifecycle.mayStartPlayback(token: second))
    }

    func testStopInvalidatesPendingSeekAndReadiness() {
        var lifecycle = PlaybackLifecycle()
        let token = lifecycle.beginOpening()
        lifecycle.stop()
        XCTAssertFalse(lifecycle.mayStartPlayback(token: token))
        XCTAssertFalse(lifecycle.isCurrent(token))
        XCTAssertEqual(lifecycle.intent, .stopped)
        XCTAssertEqual(lifecycle.scene(.inactive), .none)
    }

    func testStoppingAnItemPreservesBackgroundStateForItsReplacement() {
        var lifecycle = PlaybackLifecycle()
        _ = lifecycle.beginOpening()
        _ = lifecycle.scene(.background)
        lifecycle.stop()

        let next = lifecycle.beginOpening()
        XCTAssertTrue(lifecycle.isBackgrounded)
        XCTAssertEqual(lifecycle.intent, .pausedByBackground)
        XCTAssertFalse(lifecycle.mayStartPlayback(token: next))
    }

    func testBackgroundPauseIsNotReportedAsPlaying() {
        var lifecycle = PlaybackLifecycle()
        _ = lifecycle.beginOpening()
        _ = lifecycle.scene(.background)
        // A late engine state change must not resurrect intent while backgrounded.
        lifecycle.markPlaying()
        XCTAssertEqual(lifecycle.intent, .pausedByBackground)
        lifecycle.markPausedByUser()
        XCTAssertEqual(lifecycle.intent, .pausedByBackground)
    }

    func testInstallingAnItemWhileBackgroundedCannotAutoplay() {
        var lifecycle = PlaybackLifecycle()
        _ = lifecycle.beginOpening()
        lifecycle.markPlaying()
        _ = lifecycle.scene(.background)

        // Next episode installed while the app is in the background.
        let token = lifecycle.beginOpening()
        XCTAssertEqual(lifecycle.intent, .pausedByBackground)
        XCTAssertFalse(lifecycle.mayStartPlayback(token: token))

        // Returning to the foreground requires an explicit play.
        _ = lifecycle.scene(.active)
        XCTAssertFalse(lifecycle.mayStartPlayback(token: token))
        lifecycle.markPlaying()
        XCTAssertTrue(lifecycle.mayStartPlayback(token: token))
    }
}
