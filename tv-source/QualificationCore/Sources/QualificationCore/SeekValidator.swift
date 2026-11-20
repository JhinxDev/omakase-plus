import Foundation

/// Decides whether a seek actually landed where it was asked to.
///
/// This exists because the seek completion flag is not evidence of position, and
/// because the two tolerance policies in use contradicted each other: the resume
/// seek is issued with unlimited (keyframe) tolerance so an unprepared stream can
/// answer at all, while the app then asserted a strict three second landing. An
/// unlimited-tolerance seek can legitimately land outside that window, so the
/// previous code either accepted a wrong position or failed a recoverable one.
///
/// The validator keeps the loose first attempt and adds exactly one bounded
/// recovery attempt, then reports a recoverable failure. It is pure so the truth
/// table can be tested without a device.
public struct SeekValidator: Sendable {
    public enum Failure: Equatable, Sendable {
        /// The requested target was not a usable time.
        case invalidTarget
        /// The engine reported no position, or a NaN/infinite one.
        case nonFiniteLanding
        /// A finite position outside the media, typically a player reset.
        case absurdLanding(landed: Double)
        /// A plausible position too far from the target.
        case farFromTarget(distance: Double)
    }

    public enum Reason: String, Equatable, Sendable {
        case nonFiniteLanding
        case absurdLanding
        case farFromTarget
    }

    public enum Verdict: Equatable, Sendable {
        case accepted(landed: Double)
        case retry(tolerance: SeekTolerance, attempt: Int, reason: Reason)
        case failed(Failure)
    }

    /// Accepted distance between the requested target and the landing, in seconds.
    /// 3.0 keeps the repair's existing threshold; it is not tightened here.
    public let tolerance: Double
    /// Total attempts including the first. Two means one recovery seek.
    public let maxAttempts: Int

    public init(tolerance: Double = 3.0, maxAttempts: Int = 2) {
        self.tolerance = tolerance.isFinite && tolerance >= 0 ? tolerance : 3.0
        self.maxAttempts = maxAttempts < 1 ? 1 : maxAttempts
    }

    /// Tolerance for the first attempt: loose, so an unbuffered stream can answer.
    public static let firstAttemptTolerance: SeekTolerance = .keyframe

    /// Evaluates one attempt. `attempt` is 1-based.
    ///
    /// Acceptance depends only on where the player landed. The completion flag is
    /// carried for diagnostics and deliberately ignored, because a finished seek
    /// can land somewhere unrelated after a playlist switch while an unfinished
    /// seek can already sit exactly on target with the buffer still filling.
    public func evaluate(_ landing: SeekLanding, target: Double, duration: Double?,
                         attempt: Int = 1) -> Verdict {
        guard target.isFinite, target >= 0 else { return .failed(.invalidTarget) }
        let current = max(1, attempt)

        func recover(_ reason: Reason, failure: Failure) -> Verdict {
            guard current < maxAttempts else { return .failed(failure) }
            // Retry with bounded tolerance: the loose attempt already had its turn.
            return .retry(tolerance: .bounded, attempt: current + 1, reason: reason)
        }

        guard let landed = landing.landed, landed.isFinite else {
            return recover(.nonFiniteLanding, failure: .nonFiniteLanding)
        }
        if landed < 0 { return recover(.absurdLanding, failure: .absurdLanding(landed: landed)) }
        if let duration, duration.isFinite, duration > 0, landed > duration + tolerance {
            return recover(.absurdLanding, failure: .absurdLanding(landed: landed))
        }

        let distance = abs(landed - target)
        if distance <= tolerance { return .accepted(landed: landed) }
        return recover(.farFromTarget, failure: .farFromTarget(distance: distance))
    }

    /// User-facing recovery text for a terminal failure. States the action, never
    /// a URL or a numeric distance the viewer cannot use.
    public static func recoveryMessage(for failure: Failure) -> String {
        switch failure {
        case .invalidTarget:
            return "This saved position is not valid. Select Play from start."
        case .nonFiniteLanding, .absurdLanding:
            return "Resume seek interrupted. Select Play from start or try Resume again."
        case .farFromTarget:
            return "The video stopped away from the saved position. Select Play from start or try Resume again."
        }
    }
}
