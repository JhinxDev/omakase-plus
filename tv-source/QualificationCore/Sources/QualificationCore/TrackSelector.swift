import Foundation

/// Which kind of subtitle or audio track the viewer asked for.
public enum TrackRole: String, Sendable, CaseIterable {
    /// Any full track in the language.
    case any
    /// Forced narrative only (signs, foreign dialogue).
    case forced
    /// Captions for the deaf and hard of hearing.
    case sdh
}

/// A configured or carried preference, expressed without any container identity.
public enum TrackPreference: Equatable, Sendable {
    /// Apply no preference; leave the container default in place.
    case automatic
    /// Subtitles explicitly off.
    case off
    case language(_ tag: String, role: TrackRole = .any)
}

/// A concrete choice, small enough to carry to the next episode.
public enum TrackChoice: Equatable, Sendable {
    case off
    case track(String)
}

public enum TrackSelection: Equatable, Sendable {
    case selected(EngineTrack)
    case off
    /// Nothing suitable, or no opinion: the caller leaves the default untouched.
    case none
}

/// Maps language + name + role preferences onto a container track identity.
///
/// The container identity is whatever the engine publishes, so the same rules
/// serve VLCKit track indexes and AVFoundation media options. Pure and
/// deterministic, so the mapping can be tested without a device.
public struct TrackSelector: Sendable {
    public init() {}

    /// Resolves a preference against the tracks an engine reports.
    public func select(from tracks: [EngineTrack], kind: TrackKind,
                       preference: TrackPreference) -> TrackSelection {
        switch preference {
        case .automatic:
            return .none
        case .off:
            return .off
        case let .language(tag, role):
            guard let wanted = LanguageQuery(tag) else { return .none }
            let ranked = tracks
                .filter { $0.kind == kind }
                .compactMap { track -> (EngineTrack, Rank)? in
                    guard let language = wanted.rank(track) else { return nil }
                    return (track, Rank(language: language, role: role.rank(track),
                                        isDefault: track.isDefault))
                }
                .sorted { lhs, rhs in lhs.1.beats(rhs.1, lhs.0, rhs.0) }
            guard let best = ranked.first else { return .none }
            return .selected(best.0)
        }
    }

    /// Infers forced/SDH role hints from a container name or description.
    ///
    /// Container metadata for MKV/Real-Debrid originals does not expose the
    /// disposition flags through every engine, so names such as "English forced"
    /// or "English SDH" are the remaining signal. Heuristic by nature; treat a
    /// wrong guess as a selection defect, not a crash.
    public static func roleHint(name: String?) -> (isForced: Bool, isSDH: Bool) {
        guard let name else { return (false, false) }
        let text = name.lowercased()
        let isForced = text.contains("forced") || text.contains("signs") || text.contains("sign/song")
        let isSDH = text.contains("sdh") || text.contains("hearing") || text.contains("hoh")
        // "forced" and "sdh" are mutually exclusive descriptions.
        return (isForced && !isSDH, isSDH)
    }

    private struct Rank {
        let language: Int
        let role: Int
        let isDefault: Bool

        func beats(_ other: Rank, _ lhs: EngineTrack, _ rhs: EngineTrack) -> Bool {
            if language != other.language { return language > other.language }
            if role != other.role { return role > other.role }
            if isDefault != other.isDefault { return isDefault }
            let lhsName = lhs.name ?? ""
            let rhsName = rhs.name ?? ""
            if lhsName != rhsName { return lhsName < rhsName }
            return lhs.id < rhs.id
        }
    }

    private struct LanguageQuery {
        let full: String
        let primary: String

        init?(_ tag: String) {
            let normalized = tag
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
                .replacingOccurrences(of: "_", with: "-")
            guard !normalized.isEmpty, normalized != "und", normalized != "auto" else { return nil }
            full = normalized
            primary = normalized.split(separator: "-").first.map(String.init) ?? normalized
        }

        /// 3 exact tag, 2 same primary language, 1 display-name mention.
        func rank(_ track: EngineTrack) -> Int? {
            if let language = track.language?.lowercased().replacingOccurrences(of: "_", with: "-"),
               !language.isEmpty {
                if language == full { return 3 }
                let trackPrimary = language.split(separator: "-").first.map(String.init) ?? language
                if trackPrimary == primary { return 2 }
            }
            if let name = track.name?.lowercased(), name.contains(primary), primary.count >= 2 {
                return 1
            }
            return nil
        }
    }
}

private extension TrackRole {
    /// Higher is a better fit. Forced tracks are demoted for a plain language
    /// preference because they only cover part of the dialogue.
    func rank(_ track: EngineTrack) -> Int {
        switch self {
        case .forced: return track.isForced ? 3 : 1
        case .sdh: return track.isSDH ? 3 : (track.isForced ? 0 : 1)
        case .any: return track.isForced ? 0 : (track.isSDH ? 1 : 2)
        }
    }
}

/// Records what the viewer chose and refuses stale default selections.
///
/// A default-selection task can sit in an await for seconds while the viewer
/// changes tracks (or the next episode starts). Capturing `revision()` before the
/// await and calling `mayApplyDefault(kind:revision:)` after it means a late
/// default can never overwrite a newer explicit choice, and an explicit Off is
/// carried to the next episode instead of being re-enabled by the configured
/// language.
public struct TrackSelectionLedger: Sendable {
    public struct Revision: Equatable, Sendable {
        public let item: UInt64
        public let choice: UInt64
    }

    public private(set) var itemToken: UInt64 = 0
    public private(set) var choiceRevision: UInt64 = 0
    private var carried: [TrackKind: TrackChoice] = [:]

    public init() {}

    /// Starts a new item. Carried choices survive, which is how a viewer's
    /// Danish or Off selection reaches the next episode.
    public mutating func beginItem(_ token: UInt64) {
        itemToken = token
    }

    /// Records an explicit viewer choice. Refused when it belongs to a replaced item.
    @discardableResult
    public mutating func recordExplicit(kind: TrackKind, choice: TrackChoice,
                                        itemToken: UInt64) -> Bool {
        guard itemToken == self.itemToken else { return false }
        choiceRevision &+= 1
        carried[kind] = choice
        return true
    }

    /// The choice to apply to a newly installed item, if the viewer made one.
    public func carry(for kind: TrackKind) -> TrackChoice? { carried[kind] }

    public func revision() -> Revision {
        Revision(item: itemToken, choice: choiceRevision)
    }

    /// A default may apply only if nothing changed since the task captured its
    /// revision and the viewer never chose anything for this kind.
    public func mayApplyDefault(kind: TrackKind, revision: Revision) -> Bool {
        revision.item == itemToken && revision.choice == choiceRevision && carried[kind] == nil
    }
}
