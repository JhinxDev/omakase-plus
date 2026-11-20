import Foundation

/// Portable mirror of the AVPlayer facts the qualification build reads.
///
/// AVFoundation types stay in the app target; this input type lets the mapping
/// that redacts and allowlists them live in QualificationCore, where `swift test`
/// can exercise it on any platform. It deliberately carries the access-log URI
/// and the error-log comment so the mapping proves those values are dropped
/// rather than assumed to be harmless.
public struct AVPlayerDiagnosticInput: Equatable, Sendable {
    public struct AccessEvent: Equatable, Sendable {
        public var bytesTransferred: Int64
        public var mediaRequests: Int
        public var uri: String?

        public init(bytesTransferred: Int64, mediaRequests: Int, uri: String? = nil) {
            self.bytesTransferred = bytesTransferred
            self.mediaRequests = mediaRequests
            self.uri = uri
        }
    }

    public struct ErrorLogEvent: Equatable, Sendable {
        public var statusCode: Int
        public var domain: String
        public var comment: String?
        public var uri: String?

        public init(statusCode: Int, domain: String, comment: String? = nil, uri: String? = nil) {
            self.statusCode = statusCode
            self.domain = domain
            self.comment = comment
            self.uri = uri
        }
    }

    public var itemStatus: String
    public var errorDomain: String?
    public var errorCode: Int?
    public var timeControlStatus: String
    public var waitingReason: String?
    public var currentTime: Double
    public var rate: Double
    public var loadedRanges: [PlaybackDiagnostic.TimeRange]
    public var seekableRanges: [PlaybackDiagnostic.TimeRange]
    public var accessEvents: [AccessEvent]
    public var errorEvents: [ErrorLogEvent]

    public init(itemStatus: String,
                errorDomain: String? = nil,
                errorCode: Int? = nil,
                timeControlStatus: String,
                waitingReason: String? = nil,
                currentTime: Double,
                rate: Double,
                loadedRanges: [PlaybackDiagnostic.TimeRange] = [],
                seekableRanges: [PlaybackDiagnostic.TimeRange] = [],
                accessEvents: [AccessEvent] = [],
                errorEvents: [ErrorLogEvent] = []) {
        self.itemStatus = itemStatus
        self.errorDomain = errorDomain
        self.errorCode = errorCode
        self.timeControlStatus = timeControlStatus
        self.waitingReason = waitingReason
        self.currentTime = currentTime
        self.rate = rate
        self.loadedRanges = loadedRanges
        self.seekableRanges = seekableRanges
        self.accessEvents = accessEvents
        self.errorEvents = errorEvents
    }
}

public extension PlaybackDiagnostic {
    /// Maps AVPlayer facts to the allowlisted payload.
    ///
    /// The access-log URI and the error-log comment are accepted by the input
    /// and never copied into an `Event`: only the numeric facts survive. Item
    /// error is reduced to domain and code, and the two text fields that remain
    /// (domain, waiting reason) pass through the single redaction function.
    static func avplayer(_ input: AVPlayerDiagnosticInput, trigger: Trigger,
                         mediaID: String? = nil) -> PlaybackDiagnostic {
        var events: [Event] = []
        events.append(contentsOf: input.accessEvents.suffix(2).map {
            Event(bytesTransferred: $0.bytesTransferred, mediaRequests: $0.mediaRequests)
        })
        events.append(contentsOf: input.errorEvents.suffix(2).map {
            Event(statusCode: $0.statusCode)
        })
        return PlaybackDiagnostic(
            engine: .avplayer,
            trigger: trigger,
            mediaID: mediaID,
            itemStatus: redactedText(input.itemStatus),
            itemError: input.errorDomain.map { ItemError(domain: $0, code: input.errorCode ?? 0) },
            timeControlStatus: redactedText(input.timeControlStatus),
            waitingReason: input.waitingReason,
            currentTime: input.currentTime,
            rate: input.rate,
            loadedRanges: input.loadedRanges,
            seekableRanges: input.seekableRanges,
            events: events
        )
    }
}
