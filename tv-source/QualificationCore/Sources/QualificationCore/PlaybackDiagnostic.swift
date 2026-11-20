import Foundation

/// Structured, allowlisted AVPlayer facts that may be posted to the server.
///
/// AVPlayer diagnostics are compiled into the app but have never been observed
/// on a real Apple TV. Until that happens the reporting path has to be safe by
/// construction: every field here is a number, an allowlisted label, or a tiny
/// text run passed through `redactedText`. The free-form snapshot this replaces
/// embedded the access-log URI and the error-log comment, which can carry a
/// provider path, a signed query or user-info.
///
/// Not here, deliberately: any URL, request URI, header, cookie, token,
/// localizedDescription or full error string. The access log's URI is dropped
/// before the event is built, so it cannot appear even by accident.
public struct PlaybackDiagnostic: Codable, Equatable, Sendable {
    public enum Engine: String, Codable, Sendable {
        case avplayer
        case vlc
        case none
    }

    /// Why the app is reporting. Replaces the free-form `"state=..."` prefix.
    public enum Trigger: String, Codable, Sendable {
        case error
        case state
        case heartbeat
    }

    /// (start, duration) in seconds. Duration, not end, keeps the wire shape
    /// exactly what the diagnostic contract asked for.
    public struct TimeRange: Codable, Equatable, Sendable {
        public var start: Double
        public var duration: Double

        public init(start: Double, duration: Double) {
            self.start = start
            self.duration = duration
        }
    }

    /// Item error reduced to the NSError domain and code. `domain` is the only
    /// text field carried through the redactor; `localizedDescription` is never
    /// read because it can embed the failing URL.
    public struct ItemError: Codable, Equatable, Sendable {
        public var domain: String
        public var code: Int

        public init(domain: String, code: Int) {
            self.domain = PlaybackDiagnostic.redactedText(domain)
            self.code = code
        }
    }

    /// One numeric access/error-log fact. `statusCode` comes from AVPlayer's
    /// error log, the other two from its access log; the URI is dropped.
    public struct Event: Codable, Equatable, Sendable {
        public var statusCode: Int?
        public var bytesTransferred: Int64?
        public var mediaRequests: Int?

        public init(statusCode: Int? = nil, bytesTransferred: Int64? = nil, mediaRequests: Int? = nil) {
            self.statusCode = statusCode
            self.bytesTransferred = bytesTransferred
            self.mediaRequests = mediaRequests
        }

        enum CodingKeys: String, CodingKey {
            case statusCode = "status_code"
            case bytesTransferred = "bytes_transferred"
            case mediaRequests = "media_requests"
        }
    }

    public var schema: String
    public var engine: Engine
    public var trigger: Trigger
    public private(set) var mediaID: String?
    public var itemStatus: String?
    public var itemError: ItemError?
    public var timeControlStatus: String?
    public var waitingReason: String?
    public var currentTime: Double?
    public var rate: Double?
    public var loadedRanges: [TimeRange]
    public var seekableRanges: [TimeRange]
    public var events: [Event]

    public init(engine: Engine, trigger: Trigger = .state, mediaID: String? = nil,
                itemStatus: String? = nil, itemError: ItemError? = nil,
                timeControlStatus: String? = nil, waitingReason: String? = nil,
                currentTime: Double? = nil, rate: Double? = nil,
                loadedRanges: [TimeRange] = [], seekableRanges: [TimeRange] = [],
                events: [Event] = []) {
        self.schema = PlaybackDiagnostic.schemaVersion
        self.engine = engine
        self.trigger = trigger
        // A media id is the server's own handle for the served asset. It is an
        // allowlisted identifier, never part of a URL or token.
        self.mediaID = mediaID.flatMap(PlaybackDiagnostic.mediaIdentifier)
        self.itemStatus = itemStatus
        self.itemError = itemError
        self.timeControlStatus = timeControlStatus
        self.waitingReason = waitingReason.map(PlaybackDiagnostic.redactedText)
        self.currentTime = currentTime.flatMap(PlaybackDiagnostic.finiteSeconds)
        self.rate = rate.flatMap { $0.isFinite ? $0 : nil }
        self.loadedRanges = loadedRanges.filter(PlaybackDiagnostic.isUsableRange)
        self.seekableRanges = seekableRanges.filter(PlaybackDiagnostic.isUsableRange)
        self.events = Array(events.prefix(PlaybackDiagnostic.maximumEvents))
    }

    public static let schemaVersion = "playback_diagnostic_v1"
    public static let maximumEvents = 4

    enum CodingKeys: String, CodingKey {
        case schema
        case engine
        case trigger
        case mediaID = "media_id"
        case itemStatus = "item_status"
        case itemError = "item_error"
        case timeControlStatus = "time_control"
        case waitingReason = "waiting_reason"
        case currentTime = "current_time"
        case rate
        case loadedRanges = "loaded_ranges"
        case seekableRanges = "seekable_ranges"
        case events
    }

    /// The single redaction function kept for the few fields that must remain
    /// text. It removes every URL-shaped run entirely: scheme, user-info, host,
    /// port, path, query and fragment all go, and every occurrence in a string
    /// is handled, not just the first.
    ///
    /// A string that does not contain a URL is returned unchanged (bounded to
    /// 600 characters), so short symbolic values such as an error domain or a
    /// waiting-reason rawValue survive. Prefer deleting a field over calling
    /// this: text that is not needed should not be collected at all.
    public static func redactedText(_ value: String) -> String {
        guard !value.isEmpty else { return "" }
        var text = value
        for pattern in urlPatterns {
            guard let expression = try? NSRegularExpression(pattern: pattern) else { continue }
            let matches = expression.matches(in: text, range: NSRange(text.startIndex..., in: text))
            for match in matches.reversed() {
                guard let range = Range(match.range, in: text) else { continue }
                var matched = String(text[range])
                var suffix = ""
                // Keep trailing sentence punctuation out of the replacement so
                // "see https://host/x." does not turn into "<url".
                while let last = matched.last, ".,;:!?'\"".contains(last) {
                    suffix = String(last) + suffix
                    matched.removeLast()
                }
                guard !matched.isEmpty else { continue }
                text.replaceSubrange(range, with: "<url>" + suffix)
            }
        }
        return String(text.prefix(600))
    }

    /// Absolute and protocol-relative URLs first, then a bare provider host with
    /// optional user-info, port, path and query. The bare pass is what catches an
    /// error string that formats a URL without its scheme.
    private static let urlPatterns: [String] = [
        #"(?i)\b[a-z][a-z0-9+.-]*://[^\s"'<>()\[\]{}]*"#,
        #"(?i)\b//[^\s"'<>()\[\]{}]+"#,
        #"(?i)(?<![A-Za-z0-9_.-])(?:[^\s"'<>()\[\]{}/@]+:[^\s"'<>()\[\]{}/@]*@)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{1,5})?(?:/[^\s"'<>()\[\]{}]*)?"#,
    ]

    private static func mediaIdentifier(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count == 32,
              trimmed.allSatisfy({ ("0"..."9").contains($0) || ("a"..."f").contains($0) })
        else { return nil }
        return trimmed
    }

    private static func finiteSeconds(_ value: Double) -> Double? {
        value.isFinite && value >= 0 ? value : nil
    }

    private static func isUsableRange(_ range: TimeRange) -> Bool {
        range.start.isFinite && range.duration.isFinite && range.start >= 0 && range.duration > 0
    }
}

public extension PlaybackDiagnostic {
    /// A copy carrying the server-issued media id, for the server's own
    /// served-asset trace. The id must be 32 lowercase hex characters; anything
    /// else (including a URL) is dropped rather than sent.
    func attaching(mediaID: String?) -> PlaybackDiagnostic {
        var copy = self
        copy.mediaID = mediaID.flatMap(PlaybackDiagnostic.mediaIdentifier)
        return copy
    }

    /// JSON body posted to the server. Sorted keys keep encoding deterministic
    /// for the tests; absent optional fields stay absent rather than null.
    func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}
