import Foundation

public enum LibraryKind: String, CaseIterable, Sendable {
    case anime, movie, tv, cloud
}

/// The playable unit, not a shelf ID or a title display name.
public enum LibraryTarget: Hashable, Sendable {
    case anime(anilistID: Int, season: Int, episode: Int)
    case animeFilm(anilistID: Int)
    case movie(imdbID: String)
    case cloud(fileID: String)
    case tv(tmdbID: Int, season: Int, episode: Int)

    public var kind: LibraryKind {
        switch self {
        case .anime, .animeFilm: return .anime
        case .movie: return .movie
        case .cloud: return .cloud
        case .tv: return .tv
        }
    }

    func isImmediatelyBefore(_ next: LibraryTarget) -> Bool {
        switch self {
        case let .anime(id, season, episode) where (1..<5000).contains(episode):
            return next == .anime(anilistID: id, season: season, episode: episode + 1)
        case let .tv(id, season, episode) where (1..<1000).contains(episode):
            return next == .tv(tmdbID: id, season: season, episode: episode + 1)
        default:
            return false
        }
    }

    fileprivate var resumeID: String? {
        switch self {
        case let .anime(id, season, episode):
            guard id > 0, season > 0, (1...5000).contains(episode) else { return nil }
            return "anime-\(id)-\(season)-\(episode)"
        case let .animeFilm(id):
            guard id > 0 else { return nil }
            return "anime-film-\(id)"
        case let .movie(id):
            let digits = id.utf8.dropFirst(2)
            guard id.hasPrefix("tt"), (7...10).contains(digits.count),
                  digits.allSatisfy({ (48...57).contains($0) }) else { return nil }
            return "movie-\(id)"
        case let .cloud(id):
            guard id.utf8.count == 64, id.utf8.allSatisfy({
                (48...57).contains($0) || (97...102).contains($0)
            }) else { return nil }
            return id
        case let .tv(id, season, episode):
            guard (1...2_147_483_647).contains(id), (0...200).contains(season),
                  (1...1000).contains(episode) else { return nil }
            return "tv-\(id)-\(season)-\(episode)"
        }
    }
}

/// A version is required. The fixture uses the bundled asset's SHA-256; a server
/// adapter must use the opaque version returned for the resolved Omakase copy.
public struct LibraryIdentity: Hashable, Sendable {
    public let libraryID: String
    public let target: LibraryTarget
    public let mediaVersion: String
    public let resumeIdentity: ResumeIdentity

    public init?(libraryID: String, target: LibraryTarget, mediaVersion: String, viewerScope: String? = nil) {
        guard let id = target.resumeID,
              let key = ResumeIdentity(id: id, fingerprint: mediaVersion, libraryID: libraryID,
                                       viewerScope: viewerScope) else { return nil }
        self.libraryID = libraryID
        self.target = target
        self.mediaVersion = mediaVersion
        self.resumeIdentity = key
    }
}

public struct PlaybackMedia: Sendable {
    public let url: URL
    public let resumeIdentity: ResumeIdentity

    public init(url: URL, resumeIdentity: ResumeIdentity) {
        self.url = url
        self.resumeIdentity = resumeIdentity
    }
}

public struct LibraryCard: Identifiable, Sendable {
    public let identity: LibraryIdentity
    public let title: String
    public let episodeTitle: String?
    public let synopsis: String
    public var id: LibraryIdentity { identity }
    public var kind: LibraryKind { identity.target.kind }
}

public struct LibraryFixture: Sendable {
    public static let libraryID = "00000000000000000000000000000001"
    public static let mediaVersion = "7d649985878059299c7ba7010600c4c9b9aca07e7320169b1f10547111e6d3c0"
    public static let tracksVersion = "775c790bf5ab608abd33e442758849e4e347938007613468ec5750a1fec10b2f"
    public let cards: [LibraryCard]
    private let allCards: [LibraryCard]
    private let baselineURL: URL
    private let tracksURL: URL?

    public init?(baselineURL: URL, tracksURL: URL? = nil) {
        guard baselineURL.isFileURL, baselineURL.lastPathComponent == "baseline.mp4" else { return nil }
        if let tracksURL, !tracksURL.isFileURL || tracksURL.lastPathComponent != "track-check.mp4" { return nil }
        self.baselineURL = baselineURL
        self.tracksURL = tracksURL
        // Synthetic catalog IDs. All three cards share one clip, with separate resume records.
        let rows: [(LibraryTarget, String, String?, String)] = [
            (.anime(anilistID: 9000001, season: 1, episode: 1), "Paper Satellites", "S1 E1 · A Signal in the Snow",
             "A student radio club catches a signal that seems to come from tomorrow."),
            (.movie(imdbID: "tt999999991"), "The Violet Hour", nil,
             "On the last ferry home, a photographer finds an undeveloped roll with her name on it."),
            (.tv(tmdbID: 9000001, season: 1, episode: 2), "Northbound", "S1 E2 · The Night Shift",
             "The crew of a mountain station has one shift left to find a missing weather balloon."),
            (.anime(anilistID: 9000001, season: 1, episode: 2), "Paper Satellites", "S1 E2 · The Missing Frequency",
             "The club traces the signal to a transmitter that has been silent for decades."),
            (.tv(tmdbID: 9000001, season: 1, episode: 3), "Northbound", "S1 E3 · Whiteout",
             "The station loses its weather feed as the storm reaches the ridge."),
        ]
        allCards = rows.compactMap { target, title, episode, synopsis in
            let tracks = tracksURL != nil && target == .tv(tmdbID: 9000001, season: 1, episode: 3)
            return LibraryIdentity(libraryID: Self.libraryID, target: target,
                                   mediaVersion: tracks ? Self.tracksVersion : Self.mediaVersion)
                .map { LibraryCard(identity: $0, title: title, episodeTitle: episode, synopsis: synopsis) }
        }
        guard allCards.count == rows.count else { return nil }
        cards = Array(allCards.prefix(3))
    }

    public func media(for identity: LibraryIdentity) -> PlaybackMedia? {
        guard allCards.contains(where: { $0.identity == identity }) else { return nil }
        let url = identity.mediaVersion == Self.tracksVersion ? tracksURL : baselineURL
        return url.map { PlaybackMedia(url: $0, resumeIdentity: identity.resumeIdentity) }
    }

    public func adjacentEpisode(to card: LibraryCard, step: Int) -> LibraryCard? {
        guard step == -1 || step == 1, media(for: card.id) != nil else { return nil }
        let target: LibraryTarget
        switch card.id.target {
        case let .anime(id, season, episode): target = .anime(anilistID: id, season: season, episode: episode + step)
        case let .tv(id, season, episode): target = .tv(tmdbID: id, season: season, episode: episode + step)
        case .animeFilm, .movie, .cloud: return nil
        }
        return allCards.first { $0.id.target == target }
    }
}
