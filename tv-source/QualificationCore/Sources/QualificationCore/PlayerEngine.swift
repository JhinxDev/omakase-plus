import Foundation

/// Platform-neutral description of a selectable elementary track.
///
/// Container identities are opaque strings chosen by the engine adapter: VLCKit
/// uses its track index, AVFoundation uses the media-option identity. Callers must
/// treat the identifier as opaque and only compare it for equality.
public struct EngineTrack: Hashable, Sendable {
    public let id: String
    public let kind: TrackKind
    /// BCP-47 tag when the container publishes one, lowercased by the adapter.
    public let language: String?
    /// Display name as the container reports it. Role detection falls back to this.
    public let name: String?
    public let isForced: Bool
    /// Captions for the deaf and hard of hearing (transcribes dialogue).
    public let isSDH: Bool
    public let isDefault: Bool

    public init(id: String, kind: TrackKind, language: String? = nil, name: String? = nil,
                isForced: Bool = false, isSDH: Bool = false, isDefault: Bool = false) {
        self.id = id
        self.kind = kind
        self.language = language
        self.name = name
        self.isForced = isForced
        self.isSDH = isSDH
        self.isDefault = isDefault
    }
}

public enum TrackKind: String, Sendable, CaseIterable {
    case audio
    case subtitle
}

/// Coarse transport state. `failed` is terminal for the current item.
public enum PlayerState: Equatable, Sendable {
    case idle
    case opening
    case ready
    case buffering
    case playing
    case paused
    case ended
    case failed

    /// Allowlisted label for a diagnostic. Carries no URL, token or error text.
    public var diagnosticLabel: String {
        switch self {
        case .idle: return "idle"
        case .opening: return "opening"
        case .ready: return "ready"
        case .buffering: return "buffering"
        case .playing: return "playing"
        case .paused: return "paused"
        case .ended: return "ended"
        case .failed: return "failed"
        }
    }
}

/// A sanitized playback failure.
///
/// `code` must never contain a URL, token or signed query. Adapters pass a short
/// symbolic code and the initializer drops control characters and truncates it, so
/// a diagnostic can be reported to the server without leaking the media URL.
public struct EngineFailure: Equatable, Error, Sendable {
    public enum Kind: String, Sendable {
        case network
        case media
        case authorization
        case unsupported
        case unknown
    }

    public let kind: Kind
    public let code: String

    public init(kind: Kind, code: String) {
        self.kind = kind
        self.code = Self.sanitized(code)
    }

    static func sanitized(_ raw: String) -> String {
        var text = String()
        for scalar in raw.unicodeScalars where !CharacterSet.controlCharacters.contains(scalar) {
            text.unicodeScalars.append(scalar)
        }
        return String(text.prefix(64))
    }
}

/// Events every engine must publish.
public enum PlayerEvent: Equatable, Sendable {
    /// The item is prepared and its metadata, tracks and duration are readable.
    case ready
    case failed(EngineFailure)
    case ended
    case stateChanged(PlayerState)
    /// Track list or selected track changed after `ready` (tracks appear late for
    /// some containers, and a user may change selection from the system UI).
    case tracksChanged
}

/// How precisely a seek should land.
public enum SeekTolerance: Equatable, Sendable {
    /// Let the engine choose the nearest sync point. Tolerant of an unbuffered
    /// streaming source, which is what a resume seek usually meets.
    case keyframe
    /// Bounded tolerance. Used for the single recovery seek when a keyframe
    /// landing fell outside the accepted distance.
    case bounded
}

/// Raw outcome of one seek attempt, as reported by the engine.
///
/// `finished` is the engine's completion flag and is deliberately not a claim
/// about position: a stream that switches playlists can complete a seek somewhere
/// unrelated. `SeekValidator` decides success from `landed` alone.
public struct SeekLanding: Equatable, Sendable {
    public let landed: Double?
    public let finished: Bool

    public init(landed: Double?, finished: Bool) {
        self.landed = landed
        self.finished = finished
    }
}

/// A half-open time interval in seconds reported by an engine.
public struct MediaRange: Equatable, Sendable, Comparable {
    public let start: Double
    public let end: Double

    public init(start: Double, end: Double) {
        self.start = start
        self.end = end
    }

    public var isValid: Bool { start.isFinite && end.isFinite && end > start && start >= 0 }

    public static func < (lhs: MediaRange, rhs: MediaRange) -> Bool {
        lhs.start == rhs.start ? lhs.end < rhs.end : lhs.start < rhs.start
    }
}

/// Buffered / seekable description shared by both engines.
public struct TransportRanges: Equatable, Sendable {
    public let buffered: [MediaRange]
    public let seekable: [MediaRange]

    public init(buffered: [MediaRange] = [], seekable: [MediaRange] = []) {
        self.buffered = Self.normalized(buffered)
        self.seekable = Self.normalized(seekable)
    }

    /// Drops non-finite or empty ranges, sorts them and merges overlaps and
    /// touching neighbours, so equal inputs from different engines compare equal.
    public static func normalized(_ ranges: [MediaRange]) -> [MediaRange] {
        let valid = ranges.filter(\.isValid).sorted()
        var merged: [MediaRange] = []
        for range in valid {
            if let last = merged.last, range.start <= last.end + 0.001 {
                merged[merged.count - 1] = MediaRange(start: last.start, end: max(last.end, range.end))
            } else {
                merged.append(range)
            }
        }
        return merged
    }

    public var isSeekable: Bool { !seekable.isEmpty }

    public func isSeekable(_ seconds: Double) -> Bool {
        guard seconds.isFinite else { return false }
        return seekable.contains { seconds >= $0.start && seconds <= $0.end }
    }

    /// Diagnostic text. Contains no URL, host or token.
    public var summary: String {
        func describe(_ ranges: [MediaRange]) -> String {
            guard !ranges.isEmpty else { return "none" }
            return ranges.prefix(4).map { String(format: "%.1f-%.1f", $0.start, $0.end) }.joined(separator: ",")
        }
        return "buffered=\(describe(buffered)) seekable=\(describe(seekable))"
    }
}

/// A media transport the app can drive.
///
/// The protocol carries no AVFoundation, UIKit or VLCKit type so it can live in
/// the portable core package and be exercised by `swift test` on a Mac. Concrete
/// adapters stay in the app target.
///
/// Callback contract, which the adapters must honour and callers may rely on:
/// an engine never invokes `onEvent` or a seek completion synchronously from
/// inside one of its own methods. Callbacks arrive on the main actor on a later
/// turn, so a callback may tear the engine down, start another seek or mutate
/// session state without re-entrancy.
@MainActor
public protocol PlayerEngine: AnyObject {
    var state: PlayerState { get }
    /// Seconds into the item; `.nan` when the engine cannot report a position yet.
    var currentTime: Double { get }
    /// Seconds of total media; `.nan` for a growing playlist or before ready.
    var duration: Double { get }
    var ranges: TransportRanges { get }
    var tracks: [EngineTrack] { get }
    /// False when there is nothing to draw (audio-only item).
    var hasVideoOutput: Bool { get }
    var onEvent: ((PlayerEvent) -> Void)? { get set }

    func play()
    func pause()
    func stop()

    /// Requests a seek and reports the landing asynchronously.
    ///
    /// Exactly one completion is delivered per accepted request, and never from
    /// inside this call.
    func seek(to seconds: Double, tolerance: SeekTolerance,
              completion: @escaping @MainActor (SeekLanding) -> Void)

    /// Selects a track by opaque identity, or clears selection with `nil`
    /// (subtitle Off). Returns false when the engine has no such track.
    @discardableResult
    func selectTrack(kind: TrackKind, id: String?) -> Bool

    func selectedTrackID(kind: TrackKind) -> String?
}
