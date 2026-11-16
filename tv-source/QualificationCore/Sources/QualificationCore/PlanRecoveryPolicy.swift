/// The single automatic recovery for a viewer whose media plan was reclaimed.
///
/// The server keeps its idle reclamation but now says so: the pass is still
/// authorized, and its bounded timeline is gone. Reopening is worth exactly one
/// attempt per pass, at the last position the client actually trusted. A second
/// report for the same pass is shown to the viewer instead of looping, because
/// repeating it would hide a real server fault.
///
/// This lives in the portable core so the decision is covered by `swift test`
/// on a host without AVFoundation.
public struct PlanExpiryRecovery: Sendable {
    public enum Action: Equatable, Sendable {
        /// Open a fresh pass and continue from this position.
        case reopen(position: Double, keepPaused: Bool)
        /// Do not reopen again. Show this text to the viewer.
        case report(String)
    }

    public static let failedMessage =
        "This video could not reopen. Press Play to try again from the saved position."
    public static let repeatedMessage =
        "This video’s stream expired again. Reopen the title to keep watching."

    private var recovered: Set<String> = []

    public init() {}

    /// Ask what to do about one expired plan.
    ///
    /// - Parameters:
    ///   - mediaID: the pass the server reported expired.
    ///   - lastReliablePosition: the newest position this device actually
    ///     checkpointed. Nil or non-finite falls back to the start of the copy.
    ///   - keepPaused: whether the viewer had paused, so recovery preserves intent.
    public mutating func action(mediaID: String, lastReliablePosition: Double?,
                                keepPaused: Bool) -> Action {
        guard !recovered.contains(mediaID) else { return .report(Self.repeatedMessage) }
        recovered.insert(mediaID)
        let position = lastReliablePosition.flatMap { $0.isFinite ? max(0, $0) : nil } ?? 0
        return .reopen(position: position, keepPaused: keepPaused)
    }

    /// True once this pass has already used its single automatic attempt.
    public func hasRecovered(mediaID: String) -> Bool { recovered.contains(mediaID) }
}
