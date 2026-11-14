import Foundation

/// The app's intent for the current item, as opposed to what the engine reports.
///
/// Intent is the only authority for "may this completion start playing?". Every
/// asynchronous completion (readiness, seek landing, next-episode replacement,
/// default track selection) loads the intent current at that moment instead of
/// remembering the intent captured when it started.
public enum TransportIntent: Equatable, Sendable {
    case idle
    /// An item is installed and preparing. Autoplay is still allowed.
    case opening
    case playing
    /// The viewer paused deliberately.
    case pausedByUser
    /// The app went to the background. Nothing may auto-start; the item is kept.
    case pausedByBackground
    case ended
    case stopped
}

/// Scene changes that matter to playback.
///
/// `.inactive` is transient presentation (Control Center, a system overlay, a
/// button press) and must not pause or cancel anything. Only `.background` is a
/// real exit from the foreground.
public enum AppScenePhase: Sendable {
    case active
    case inactive
    case background
}

public enum PlaybackLifecycleAction: Equatable, Sendable {
    case none
    /// Pause the engine and checkpoint once; keep the item and its pass.
    case pauseAndCheckpoint
}

/// Pure state machine for transport intent and background handling.
///
/// It exists because readiness, seek and replacement completions were able to
/// call `play()` after the viewer left the app: each of them only checked that a
/// player object matched, not whether playback was still wanted. Completions now
/// carry the token they started with and ask `mayStartPlayback(token:)`.
public struct PlaybackLifecycle: Sendable {
    public private(set) var intent: TransportIntent
    public private(set) var token: UInt64
    public private(set) var isBackgrounded: Bool

    public init(intent: TransportIntent = .idle, token: UInt64 = 0) {
        self.intent = intent
        self.token = token
        self.isBackgrounded = false
    }

    /// Installs a new item generation. Any completion holding an older token is
    /// refused from then on. Installing while the app is already backgrounded
    /// starts in `.pausedByBackground`, so the readiness completion cannot play.
    @discardableResult
    public mutating func beginOpening() -> UInt64 {
        token &+= 1
        intent = isBackgrounded ? .pausedByBackground : .opening
        return token
    }

    public mutating func markPlaying() {
        guard !isBackgrounded else { return }
        intent = .playing
    }

    public mutating func markPausedByUser() {
        guard !isBackgrounded else { return }
        intent = .pausedByUser
    }

    public mutating func markEnded() {
        intent = .ended
    }

    /// Tears the item down. Invalidates every pending completion.
    public mutating func stop() {
        token &+= 1
        intent = .stopped
    }

    /// Applies a scene change and reports what the caller must do about it.
    ///
    /// `.inactive` never acts: on tvOS, Control Center and the power button make
    /// the scene inactive while the viewer is still watching.
    public mutating func scene(_ phase: AppScenePhase) -> PlaybackLifecycleAction {
        switch phase {
        case .active:
            isBackgrounded = false
            // Returning to the foreground does not resume by itself; the viewer
            // presses Play. A background pause becomes an ordinary pause.
            if intent == .pausedByBackground { intent = .pausedByUser }
            return .none
        case .inactive:
            return .none
        case .background:
            isBackgrounded = true
            switch intent {
            case .idle, .stopped, .ended, .pausedByBackground:
                return .none
            case .opening, .playing, .pausedByUser:
                intent = .pausedByBackground
                return .pauseAndCheckpoint
            }
        }
    }

    /// True when a completion for `token` may call `play()`.
    ///
    /// Allows `.opening` and `.playing` so a slow opening still starts when the
    /// viewer is watching, including while the scene is merely inactive.
    public func mayStartPlayback(token candidate: UInt64) -> Bool {
        candidate == token && !isBackgrounded && (intent == .opening || intent == .playing)
    }

    /// True when the viewer still wants this item on screen at all.
    public func isCurrent(_ candidate: UInt64) -> Bool {
        candidate == token && intent != .stopped
    }
}
