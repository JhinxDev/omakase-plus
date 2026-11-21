import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum LiveLibraryKind: String, CaseIterable, Decodable, Identifiable, Sendable {
    case anime, movie, tv, cloud
    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .anime: "Anime"
        case .movie: "Movies"
        case .tv: "TV Shows"
        case .cloud: "Cloud"
        }
    }
}

public struct LiveLibraryEntry: Decodable, Identifiable, Sendable {
    public let id: String
    public let title: String
    public let note: String
    public let watchPath: String?
    public let artworkID: String?
    public let posterURL: URL?
    public let backdropURL: URL?
    public let summary: String?
    public let progress: Double?
    public let featuredPath: String?
    public let featuredEpisode: Int?
    public let watched: Bool?
    public let episodeArtwork: String?
    public let durationSeconds: Int?
    public let isFolder: Bool?
    public let watchlistArtwork: Bool?
    public let availability: String?

    public init(
        homeTitleID: String, title: String, note: String, watchPath: String?,
        artworkID: String? = nil, posterURL: URL? = nil, backdropURL: URL? = nil,
        progress: Double? = nil
    ) {
        id = homeTitleID
        self.title = title
        self.note = note
        self.watchPath = watchPath
        self.artworkID = artworkID
        self.posterURL = posterURL
        self.backdropURL = backdropURL
        self.progress = progress
        summary = nil
        featuredPath = nil
        featuredEpisode = nil
        watched = nil
        episodeArtwork = nil
        durationSeconds = nil
        isFolder = nil
        watchlistArtwork = nil
        availability = nil
    }

    public var episodeDisplayTitle: String {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { return name }
        if let range = note.range(of: #"\bE[1-9][0-9]{0,3}\b"#, options: .regularExpression) {
            return "Episode " + note[range].dropFirst()
        }
        return "Episode"
    }

    public var durationLabel: String? {
        guard let durationSeconds, (1...14_400).contains(durationSeconds) else { return nil }
        return durationSeconds < 60 ? "\(durationSeconds) sec" : "\((durationSeconds + 59) / 60) min"
    }

    public var availabilityPending: Bool { watchPath == nil && availability == "partial" }

    public var unavailableLabel: String? {
        guard watchPath == nil else { return nil }
        return availabilityPending ? "Not checked yet" : "Unavailable"
    }

    enum CodingKeys: String, CodingKey {
        case id, title, note, watchPath = "watch_path"
        case artworkID = "artwork_id", posterURL = "poster_url", backdropURL = "backdrop_url"
        case summary, progress, featuredPath = "featured_path", featuredEpisode = "featured_episode"
        case isFolder = "is_folder", watchlistArtwork = "watchlist_artwork"
        case watched, availability, episodeArtwork = "episode_artwork", durationSeconds = "duration_seconds"
    }
}

public struct LiveLibraryDetails: Decodable, Sendable {
    public let title: String
    public let episodeTitle: String
    public let episodeNumber: Int?
    public let note: String
    public let summary: String?
    public let artwork: String?

    enum CodingKeys: String, CodingKey {
        case title, episodeTitle = "episode_title", episodeNumber = "episode_number"
        case note, summary, artwork
    }
}

public struct LiveLibraryPage: Decodable, Sendable {
    public let items: [LiveLibraryEntry]
    public let nextOffset: Int?
    public let featured: LiveLibraryEntry?
    public let featuredRecent: Bool?
    public let continueWatching: [LiveLibraryEntry]?
    public let titleSummary: String?
    public let availability: LiveLibraryAvailability?

    public var hasUncheckedEpisodes: Bool {
        availability?.state == "partial" || items.contains { $0.availabilityPending }
    }

    enum CodingKeys: String, CodingKey {
        case items, nextOffset = "next_offset", featured, continueWatching = "continue_watching"
        case titleSummary = "title_summary", featuredRecent = "featured_recent", availability
    }
}

public struct LiveLibraryAvailability: Decodable, Sendable {
    public let state: String
    public let seasons: [Int]?
    public let retryOffset: Int?

    enum CodingKeys: String, CodingKey {
        case state, seasons, retryOffset = "retry_offset"
    }
}

public enum LiveRecommendationCategory: String, CaseIterable, Identifiable, Sendable {
    case anime, tv, movies

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .anime: "Anime"
        case .tv: "TV Shows"
        case .movies: "Movies"
        }
    }
}

public struct LiveTitleRequest: Decodable, Identifiable, Sendable {
    public let kind: String
    public let titleID: String
    public let title: String
    public let state: String
    public let label: String
    public let detail: String
    public let statusLabel: String?
    public let canCheck: Bool?
    public let lastCheckedAt: String?
    public let nextAction: Action?

    public struct Action: Decodable, Sendable {
        public let kind: String
        public let label: String
        public let enabled: Bool
        public let reason: String?
    }
    public let reasonText: String?
    public let stages: [Stage]?
    public let fallbackSearch: FallbackSearch?
    public let fallbackTransfer: FallbackTransfer?

    public struct FallbackSearch: Decodable, Sendable {
        public let headline: String
        public let outcome: String
        public let checkedAt: String?

        enum CodingKeys: String, CodingKey {
            case headline, outcome, checkedAt = "checked_at"
        }
    }

    public struct FallbackTransfer: Decodable, Sendable {
        public let state: String
        public let headline: String
    }

    public var displayStatus: String {
        if let statusLabel, !statusLabel.isEmpty { return statusLabel }
        return label
    }

    public struct Stage: Decodable, Identifiable, Sendable {
        public let id: String
        public let label: String
        public let state: String
        public let occurredAt: String?

        enum CodingKeys: String, CodingKey {
            case id = "stage", label, state, occurredAt = "occurred_at"
        }
    }

    public var id: String { kind + ":" + titleID }

    enum CodingKeys: String, CodingKey {
        case kind, titleID = "title_id", title, state, label, detail, canCheck = "can_check"
        case lastCheckedAt = "last_checked_at", nextAction = "next_action", reasonText = "reason_text"
        case statusLabel = "status_label", stages
        case fallbackSearch = "fallback_search", fallbackTransfer = "fallback_transfer"
    }
}

public struct LiveRequestsPage: Decodable, Sendable {
    public struct Category: Decodable, Identifiable, Sendable {
        public let key: String
        public let title: String
        public let requests: [LiveTitleRequest]

        public var id: String { key }
    }

    public let categories: [Category]
}

public struct LiveRecommendation: Decodable, Identifiable, Sendable {
    public let id: String
    public let kind: String
    public let title: String
    public let posterURL: URL?
    public let summary: String?
    public let request: LiveTitleRequest?
    public let canRequest: Bool

    public var key: String { kind + ":" + id }

    enum CodingKeys: String, CodingKey {
        case id, kind, title, posterURL = "poster_url", summary, request, canRequest = "can_request"
    }
}

public struct LiveRecommendationsPage: Decodable, Sendable {
    public struct Row: Decodable, Identifiable, Sendable {
        public let key: String
        public let title: String
        public let cards: [LiveRecommendation]

        public var id: String { key }
    }

    public struct Warning: Decodable, Identifiable, Sendable {
        public let kind: String
        public let label: String
        public let detail: String

        public var id: String { kind }
    }

    public let category: String
    public let rows: [Row]
    public let requests: [LiveTitleRequest]
    public let warnings: [Warning]?
}

public enum LiveLibraryError: Error {
    case copyUnavailable, copyChanged, unsupportedCopy, resumeSuperseded, plexConnectionRequired
}

public struct LiveLibraryOption: Decodable, Identifiable, Hashable, Sendable {
    public enum Kind: String, Decodable, Sendable { case personal, household }

    public let id: String
    public let name: String
    public let kind: Kind

    static func decodeList(_ data: Data) throws -> [Self] {
        struct Reply: Decodable { let items: [LiveLibraryOption] }
        let options = try JSONDecoder().decode(Reply.self, from: data).items
        guard (1...2).contains(options.count), options.filter({ $0.kind == .personal }).count == 1,
              Set(options.map(\.id)).count == options.count,
              options.allSatisfy({ option in
                  option.id.utf8.count == 32 && option.id.utf8.allSatisfy {
                      (48...57).contains($0) || (97...102).contains($0)
                  } && !option.name.isEmpty && option.name.utf8.count <= 320
                    && !option.name.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
              })
        else { throw FixtureConnectionError.invalidResponse }
        return options
    }
}

public struct LiveLibraryClient: Sendable {
    public struct Credential: Codable, Sendable {
        let origin: String
        let token: String
        let expiresAt: Date
    }

    public struct DeviceChallenge: Sendable {
        public let code: String
        public let verificationURL: URL
        public let expiresAt: Date
        fileprivate let origin: FixtureOrigin
        fileprivate let token: String
        fileprivate let session: URLSession

        public func waitForApproval() async throws -> LiveLibraryClient {
            try await waitForApproval(pollInterval: .seconds(5))
        }

        func waitForApproval(pollInterval: Duration) async throws -> LiveLibraryClient {
            while true {
                try Task.checkCancellation()
                if let client = try await check() { return client }
                let remaining = expiresAt.timeIntervalSinceNow
                guard remaining > 0 else { throw FixtureConnectionError.unauthorized }
                try await Task.sleep(for: min(pollInterval, .seconds(remaining)))
            }
        }

        private func check() async throws -> LiveLibraryClient? {
            let remaining = expiresAt.timeIntervalSinceNow
            guard remaining > 0 else { throw FixtureConnectionError.unauthorized }
            var request = URLRequest(url: LiveLibraryClient.endpoint(origin, "device"),
                                     timeoutInterval: min(remaining, 8))
            request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
            let data: Data
            let response: URLResponse
            do {
                (data, response) = try await session.data(for: request)
            } catch let error as URLError where [
                .timedOut, .networkConnectionLost, .notConnectedToInternet, .cannotConnectToHost,
            ].contains(error.code) {
                return nil
            }
            switch (response as? HTTPURLResponse)?.statusCode {
            case 202, 502, 503, 504: return nil
            case 200 where data.count <= 4096:
                let approval = try JSONDecoder().decode(DeviceApproval.self, from: data)
                guard (1...2_592_000).contains(approval.expiresIn) else {
                    throw FixtureConnectionError.invalidResponse
                }
                return LiveLibraryClient(origin: origin, token: token, session: session,
                                         expiresAt: Date().addingTimeInterval(TimeInterval(approval.expiresIn)))
            case 401: throw FixtureConnectionError.unauthorized
            default: throw FixtureConnectionError.requestRejected
            }
        }
    }

    private struct DeviceApproval: Decodable {
        let expiresIn: Int
        enum CodingKeys: String, CodingKey { case expiresIn = "expires_in" }
    }

    private struct DeviceReply: Decodable {
        let deviceToken: String
        let userCode: String
        let expiresIn: Int
        enum CodingKeys: String, CodingKey {
            case deviceToken = "device_token", userCode = "user_code", expiresIn = "expires_in"
        }
    }

    private struct Redeem: Encodable { let code: String }
    private struct Pairing: Decodable {
        let token: String
        let expiresIn: Int

        enum CodingKeys: String, CodingKey {
            case token, expiresIn = "expires_in"
        }
    }
    private struct Play: Encodable {
        let watch_path: String
        let engine: String?
    }
    private let origin: FixtureOrigin
    private let token: String
    private let session: URLSession
    public let expiresAt: Date
    public private(set) var selectedLibrary: LiveLibraryOption?

    init(origin: FixtureOrigin, token: String, session: URLSession, expiresAt: Date) {
        self.origin = origin
        self.token = token
        self.session = session
        self.expiresAt = expiresAt
    }

    public var credential: Credential {
        Credential(origin: origin.url.absoluteString, token: token, expiresAt: expiresAt)
    }

    public static func restore(_ credential: Credential) throws -> Self {
        guard FixtureClient.tokenIsValid(credential.token),
              (0...2_592_000).contains(credential.expiresAt.timeIntervalSinceNow)
        else { throw FixtureConnectionError.unauthorized }
        return Self(origin: try FixtureOrigin(credential.origin), token: credential.token,
                    session: makeSession(), expiresAt: credential.expiresAt)
    }

    public static func startPairing(server: String) async throws -> DeviceChallenge {
        try await startPairing(server: server, session: makeSession())
    }

    static func startPairing(server: String, session: URLSession) async throws -> DeviceChallenge {
        let origin = try FixtureOrigin(server.trimmingCharacters(in: .whitespacesAndNewlines))
        var request = URLRequest(url: endpoint(origin, "device"))
        request.httpMethod = "POST"
        let data = try await load(request, session: session, maxBytes: 4096)
        let device = try JSONDecoder().decode(DeviceReply.self, from: data)
        guard FixtureClient.tokenIsValid(device.deviceToken), (1...300).contains(device.expiresIn),
              device.userCode.range(of: "^[A-Z2-9]{4}-[A-Z2-9]{4}$", options: .regularExpression) != nil
        else { throw FixtureConnectionError.invalidResponse }
        var verification = URLComponents(url: origin.url.appendingPathComponent("plus/native-tv-library"),
                                         resolvingAgainstBaseURL: false)
        verification?.queryItems = [URLQueryItem(name: "code", value: device.userCode)]
        guard let url = verification?.url else { throw FixtureConnectionError.invalidOrigin }
        return DeviceChallenge(code: device.userCode, verificationURL: url,
                               expiresAt: Date().addingTimeInterval(TimeInterval(device.expiresIn)),
                               origin: origin, token: device.deviceToken, session: session)
    }

    public static func link(invitation: String) async throws -> Self {
        let parts = invitation.trimmingCharacters(in: .whitespacesAndNewlines)
            .split(separator: "#", omittingEmptySubsequences: false)
        guard parts.count == 2, parts[1].hasPrefix("library-") else {
            throw FixtureConnectionError.invalidCode
        }
        let code = parts[1].dropFirst("library-".count).uppercased()
        guard code.utf8.count == 20, code.utf8.allSatisfy({
            (48...57).contains($0) || (65...70).contains($0)
        }) else { throw FixtureConnectionError.invalidCode }
        let origin = try FixtureOrigin(String(parts[0]))
        let session = makeSession()
        var request = URLRequest(url: endpoint(origin, "redeem"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Redeem(code: code))
        let data = try await load(request, session: session, maxBytes: 4096)
        let pairing = try JSONDecoder().decode(Pairing.self, from: data)
        guard FixtureClient.tokenIsValid(pairing.token), (1...3600).contains(pairing.expiresIn)
        else { throw FixtureConnectionError.invalidResponse }
        return Self(origin: origin, token: pairing.token, session: session,
                    expiresAt: Date().addingTimeInterval(TimeInterval(pairing.expiresIn)))
    }

    public func profile() async throws -> LiveLibraryProfile {
        let data = try await send(Self.endpoint(origin, "profile"), timeout: 8)
        return try LiveLibraryProfile.decode(data)
    }

    public func profileAvatar() async throws -> Data {
        try await send(Self.endpoint(origin, "profile/avatar"), maxBytes: 256 * 1024, timeout: 8)
    }

    public func recommendations(_ category: LiveRecommendationCategory) async throws -> LiveRecommendationsPage {
        let expected = category == .movies ? "movie" : category.rawValue
        return try await catalogPage("for-you", query: .init(name: "category", value: category.rawValue),
                                     category: category.rawValue, kinds: [expected])
    }

    public func searchCatalog(_ query: String) async throws -> LiveRecommendationsPage {
        try await catalogPage("catalog-search", query: .init(name: "q", value: query),
                              category: "all", kinds: ["anime", "movie", "tv"])
    }

    private func catalogPage(_ path: String, query: URLQueryItem, category: String,
                             kinds: Set<String>) async throws -> LiveRecommendationsPage {
        var components = URLComponents(url: Self.endpoint(origin, path), resolvingAgainstBaseURL: false)
        components?.queryItems = [query]
        guard let url = components?.url else { throw FixtureConnectionError.invalidOrigin }
        let data = try await send(url, maxBytes: 256_000, timeout: 35)
        let page = try JSONDecoder().decode(LiveRecommendationsPage.self, from: data)
        guard page.category == category, page.rows.count <= 12, page.requests.count <= 100,
              (page.warnings?.count ?? 0) <= 3,
              Set(page.rows.map(\.id)).count == page.rows.count,
              page.rows.allSatisfy({ row in
                  row.cards.count <= 30 && Set(row.cards.map(\.key)).count == row.cards.count
                      && row.cards.allSatisfy { kinds.contains($0.kind) }
              }) else { throw FixtureConnectionError.invalidResponse }
        return page
    }

    public func requestTitle(_ card: LiveRecommendation, check: Bool = false) async throws -> LiveTitleRequest {
        guard check || card.canRequest else { throw FixtureConnectionError.requestRejected }
        return try await requestTitle(kind: card.kind, titleID: card.id, check: check)
    }

    public func checkRequest(_ request: LiveTitleRequest) async throws -> LiveTitleRequest {
        guard request.canCheck == true else { throw FixtureConnectionError.requestRejected }
        return try await requestTitle(kind: request.kind, titleID: request.titleID, check: true)
    }

    private func requestTitle(kind: String, titleID: String, check: Bool) async throws -> LiveTitleRequest {
        let body = try JSONEncoder().encode(["kind": kind, "title_id": titleID])
        let path = check ? "for-you/request/check" : "for-you/request"
        let data = try await send(Self.endpoint(origin, path), method: "POST", body: body, maxBytes: 16_384,
                                  timeout: 45)
        let result = try JSONDecoder().decode(LiveTitleRequest.self, from: data)
        guard result.id == kind + ":" + titleID else { throw FixtureConnectionError.invalidResponse }
        return result
    }

    public func requests() async throws -> LiveRequestsPage {
        let data = try await send(scoped(Self.endpoint(origin, "requests")), maxBytes: 1_048_576,
                                  timeout: 35)
        let page = try JSONDecoder().decode(LiveRequestsPage.self, from: data)
        guard page.categories.map(\.id) == ["active", "attention", "history"],
              page.categories.flatMap(\.requests).count <= 150
        else { throw FixtureConnectionError.invalidResponse }
        return page
    }

    public func homeProjection<Projection: Decodable>(as type: Projection.Type) async throws -> Projection {
        let data = try await send(scoped(Self.endpoint(origin, "home")), maxBytes: 524_288, timeout: 20)
        return try JSONDecoder().decode(type, from: data)
    }

    public func editHome(action: String, target: String = "") async throws {
        let body = try JSONEncoder().encode(["action": action, "target": target])
        _ = try await send(scoped(Self.endpoint(origin, "home/layout")), method: "POST", body: body,
                           maxBytes: 16_384, timeout: 12)
    }

    public func homeArtwork(_ path: String) async throws -> Data? {
        if path.range(of: #"^artwork/[1-9][0-9]{0,17}/(banner|poster)$"#,
                      options: .regularExpression) != nil {
            return try await send(scoped(Self.endpoint(origin, path)), maxBytes: 6 * 1024 * 1024, timeout: 12)
        }
        if path.range(of: #"^watchlist/[1-9][0-9]{0,9}/poster$"#, options: .regularExpression) != nil {
            return try await send(scoped(Self.endpoint(origin, path)), maxBytes: 4 * 1024 * 1024, timeout: 12)
        }
        guard let url = URL(string: path), url.scheme == "https", url.host == "image.tmdb.org",
              url.port == nil, url.user == nil, url.password == nil, url.query == nil,
              url.fragment == nil, url.path.hasPrefix("/t/p/") else { return nil }
        return try await Self.load(URLRequest(url: url, timeoutInterval: 12),
                                   session: session, maxBytes: 6 * 1024 * 1024)
    }

    public func recommendationArtwork(_ card: LiveRecommendation) async throws -> Data? {
        guard let url = card.posterURL, url.scheme == "https", url.port == nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { return nil }
        switch url.host {
        case "image.tmdb.org" where url.path.hasPrefix("/t/p/"): break
        case "s4.anilist.co", "s.anilist.co":
            guard url.path.hasPrefix("/file/anilistcdn/") else { return nil }
        default: return nil
        }
        return try await Self.load(URLRequest(url: url, timeoutInterval: 12),
                                   session: session, maxBytes: 4 * 1024 * 1024)
    }

    public func watchlist() async throws -> [LiveLibraryEntry] {
        let data = try await send(scoped(Self.endpoint(origin, "watchlist")), maxBytes: 64 * 1024, timeout: 8)
        let page = try JSONDecoder().decode(LiveLibraryPage.self, from: data)
        guard page.items.count <= 8, Set(page.items.map(\.id)).count == page.items.count,
              page.items.allSatisfy({
                  $0.id.range(of: #"^[1-9][0-9]{0,9}$"#, options: .regularExpression) != nil
                      && $0.watchPath == nil && $0.featuredPath == nil
              }) else { throw FixtureConnectionError.invalidResponse }
        return page.items
    }

    public func libraries() async throws -> [LiveLibraryOption] {
        let data = try await send(Self.endpoint(origin, "libraries"), timeout: 8)
        return try LiveLibraryOption.decodeList(data)
    }

    public func selecting(_ option: LiveLibraryOption?) -> Self {
        var client = self
        client.selectedLibrary = option?.kind == .household ? option : nil
        return client
    }

    public func page(kind: LiveLibraryKind, title: LiveLibraryEntry?, offset: Int,
                     query search: String = "") async throws -> LiveLibraryPage {
        var components = URLComponents(url: Self.endpoint(origin, title == nil ? "catalog" : "episodes"),
                                       resolvingAgainstBaseURL: false)
        var query = [URLQueryItem(name: "kind", value: kind.rawValue),
                     URLQueryItem(name: "offset", value: String(offset))]
        if let title { query.append(URLQueryItem(name: "title_id", value: title.id)) }
        if title == nil && !search.isEmpty { query.append(URLQueryItem(name: "q", value: search)) }
        components?.queryItems = query
        guard let url = components?.url else { throw FixtureConnectionError.invalidOrigin }
        let data = try await send(scoped(url), maxBytes: 256_000)
        let page = try JSONDecoder().decode(LiveLibraryPage.self, from: data)
        guard page.items.count <= 30, (page.continueWatching?.count ?? 0) <= 8,
              Set(page.items.map(\.id)).count == page.items.count,
              page.nextOffset.map({ $0 == offset + 30 }) ?? true
        else { throw FixtureConnectionError.invalidResponse }
        return page
    }

    /// Which transport the server should reserve for this item.
    ///
    /// `native` asks for the bounded VOD timeline that AVPlayer plays with
    /// Apple's own controls. `direct` asks for the provider original itself,
    /// which only the VLC engine can decode; it is the explicit fallback for a
    /// copy the system player cannot remux.
    public enum PlaybackEngine: String, Sendable {
        case native
        case direct
    }

    public func play(_ entry: LiveLibraryEntry, featured: Bool = false,
                     engine: PlaybackEngine = .native) async throws -> LiveTestPass {
        guard let path = featured ? entry.featuredPath ?? entry.watchPath : entry.watchPath
        else { throw LiveLibraryError.copyUnavailable }
        return try await play(path: path, engine: engine)
    }

    public func refreshPlayback(_ current: LiveTestPass, acceptUpdatedCopy: Bool = false) async throws -> LiveTestPass {
        guard let path = current.watchPath, current.mediaURL.host == origin.url.host,
              current.mediaURL.port == origin.url.port else { throw LiveLibraryError.copyUnavailable }
        let replacement = try await play(path: path, engine: current.isDirect ? .direct : .native)
        guard replacement.mediaID != current.mediaID else { throw FixtureConnectionError.invalidResponse }
        let previousIdentity = current.card.identity
        let replacementIdentity = replacement.card.identity
        guard replacementIdentity.libraryID == previousIdentity.libraryID,
              replacementIdentity.target == previousIdentity.target,
              replacement.resumeScope == current.resumeScope else {
            await release(replacement)
            throw FixtureConnectionError.invalidResponse
        }
        guard acceptUpdatedCopy || replacementIdentity == previousIdentity else {
            await release(replacement)
            throw LiveLibraryError.copyChanged
        }
        return replacement
    }

    private func play(path: String, engine: PlaybackEngine) async throws -> LiveTestPass {
        let body = try JSONEncoder().encode(Play(watch_path: path, engine: engine.rawValue))
        let data = try await send(
            scoped(Self.endpoint(origin, "play")), method: "POST", body: body, timeout: 30
        )
        let pass = try LiveTestPass(reply: JSONDecoder().decode(LiveTestReply.self, from: data),
                                    origin: origin, watchPath: path)
        if let selectedLibrary, (pass.card.identity.libraryID != selectedLibrary.id || pass.resumeScope == nil) {
            await release(pass)
            throw FixtureConnectionError.invalidResponse
        }
        return pass
    }

    public func nextEpisode(after current: LiveTestPass) async throws -> LiveTestPass {
        guard let path = current.nextWatchPath else { throw LiveLibraryError.copyUnavailable }
        let data = try await send(Self.endpoint(origin, "media/\(current.mediaID)/next"), method: "POST")
        let next = try LiveTestPass(reply: JSONDecoder().decode(LiveTestReply.self, from: data),
                                    origin: origin, watchPath: path)
        guard next.mediaID != current.mediaID else { throw FixtureConnectionError.invalidResponse }
        guard current.card.identity.libraryID == next.card.identity.libraryID,
              current.resumeScope == next.resumeScope,
              current.card.identity.target.isImmediatelyBefore(next.card.identity.target)
        else {
            await release(next)
            throw FixtureConnectionError.invalidResponse
        }
        return next
    }

    public func unlink() async throws {
        let data = try await send(Self.endpoint(origin, "unlink"), method: "POST", allowExpired: true)
        try Self.confirmUnlink(data)
    }

    static func confirmUnlink(_ data: Data) throws {
        struct Reply: Decodable { let unlinked: Bool }
        guard (try? JSONDecoder().decode(Reply.self, from: data))?.unlinked == true else {
            throw FixtureConnectionError.invalidResponse
        }
    }

    public func playbackAccessLoss(for pass: LiveTestPass) async throws -> LivePlaybackAccessLoss? {
        guard pass.supportsAccessCheck, pass.mediaURL.host == origin.url.host,
              pass.mediaURL.port == origin.url.port else {
            throw FixtureConnectionError.invalidResponse
        }
        let url = Self.endpoint(origin, "media/\(pass.mediaID)/access")
        do {
            struct Receipt: Decodable {
                let allowed: Bool
                let stream: String?
                let recover: String?
            }
            let data = try await send(url, timeout: 5)
            let receipt = try JSONDecoder().decode(Receipt.self, from: data)
            guard receipt.allowed else {
                throw FixtureConnectionError.invalidResponse
            }
            // The pass is still authorized, but the bounded media plan it was
            // opened on was reclaimed while the viewer was paused. That is a
            // different outcome from revocation or a replaced copy.
            if receipt.recover == "reopen" || receipt.stream == "expired" {
                return .planExpired
            }
            return nil
        } catch FixtureConnectionError.unauthorized {
            return .profile
        } catch LiveLibraryError.copyUnavailable {
            return .media
        }
    }

    @MainActor
    public func resumeWriter(
        for pass: LiveTestPass,
        onAccessLost: @escaping (LivePlaybackAccessLoss) -> Void = { _ in }
    ) -> LiveResumeWriter? {
        guard pass.supportsResumeSync else { return nil }
        return LiveResumeWriter(onAccessLost: onAccessLost) { seq, position in
            struct Sample: Encodable { let seq: Int; let position_seconds: Double }
            struct Receipt: Decodable { let saved_at: Double }
            let body = try JSONEncoder().encode(Sample(seq: seq, position_seconds: min(position, pass.duration)))
            let url = Self.endpoint(origin, "media/\(pass.mediaID)/resume")
            let data = try await send(url, method: "POST", body: body, timeout: 5)
            let receipt = try JSONDecoder().decode(Receipt.self, from: data)
            guard (0...1e12).contains(receipt.saved_at) else { throw FixtureConnectionError.invalidResponse }
        }
    }

    public func release(_ pass: LiveTestPass) async {
        _ = try? await send(Self.endpoint(origin, "media/\(pass.mediaID)/close"), method: "POST")
    }

    /// Posts one structured playback diagnostic.
    ///
    /// The payload type has no field for a URL, request URI, header, cookie,
    /// token or localized error description, and its remaining text fields are
    /// already redacted, so no free-form message reaches this route. A payload
    /// over the route's bound is dropped rather than truncated into invalid JSON.
    public func reportDiagnostic(_ diagnostic: PlaybackDiagnostic) async {
        guard var components = URLComponents(url: Self.endpoint(origin, "diagnostic"),
                                             resolvingAgainstBaseURL: false)
        else { return }
        components.queryItems = nil
        guard let url = components.url,
              let body = try? diagnostic.encoded(), body.count <= 4096
        else { return }
        _ = try? await send(url, method: "POST", body: body, maxBytes: 4096, timeout: 10)
    }

    public func details(path: String) async throws -> LiveLibraryDetails {
        guard path.hasPrefix("/plus/watch/"), path.utf8.count <= 512,
              var components = URLComponents(url: Self.endpoint(origin, "details"),
                                             resolvingAgainstBaseURL: false)
        else { throw FixtureConnectionError.invalidResponse }
        components.queryItems = [URLQueryItem(name: "path", value: path)]
        guard let url = components.url else { throw FixtureConnectionError.invalidResponse }
        let data = try await send(scoped(url), maxBytes: 64 * 1024)
        return try JSONDecoder().decode(LiveLibraryDetails.self, from: data)
    }

    public func detailsArtwork(path: String) async throws -> Data? {
        guard path.range(of: #"^episode-artwork/(anime|tv)/[1-9][0-9]{0,17}/[0-9]{1,3}/[1-9][0-9]{0,3}$"#,
                          options: .regularExpression) != nil else { return nil }
        return try await send(scoped(Self.endpoint(origin, path)), maxBytes: 6 * 1024 * 1024)
    }

    public func artwork(_ entry: LiveLibraryEntry, banner: Bool) async throws -> Data? {
        if let path = entry.episodeArtwork,
           path.range(of: #"^episode-artwork/(anime|tv)/[1-9][0-9]{0,17}/[0-9]{1,3}/[1-9][0-9]{0,3}$"#,
                      options: .regularExpression) != nil {
            return try await send(scoped(Self.endpoint(origin, path)), maxBytes: 6 * 1024 * 1024)
        }
        if let id = entry.artworkID, id.range(of: "^[1-9][0-9]{0,17}$", options: .regularExpression) != nil {
            let path = "artwork/\(id)/\(banner ? "banner" : "poster")"
            return try await send(scoped(Self.endpoint(origin, path)), maxBytes: 6 * 1024 * 1024)
        }
        if entry.watchlistArtwork == true,
           entry.id.range(of: #"^[1-9][0-9]{0,9}$"#, options: .regularExpression) != nil {
            return try await send(scoped(Self.endpoint(origin, "watchlist/\(entry.id)/poster")),
                                  maxBytes: 4 * 1024 * 1024, timeout: 12)
        }
        let candidate = banner ? entry.backdropURL : entry.posterURL
        guard let url = candidate, url.scheme == "https", url.host == "image.tmdb.org",
              url.port == nil, url.user == nil, url.password == nil, url.query == nil,
              url.fragment == nil, url.path.hasPrefix("/t/p/") else { return nil }
        return try await Self.load(URLRequest(url: url), session: session, maxBytes: 6 * 1024 * 1024)
    }

    private static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.timeoutIntervalForResource = 120
        return URLSession(configuration: configuration, delegate: RefuseRedirects(), delegateQueue: nil)
    }

    private static func endpoint(_ origin: FixtureOrigin, _ path: String) -> URL {
        origin.url.appendingPathComponent("plus/api/native-tv-test/v1/library").appendingPathComponent(path)
    }

    private func scoped(_ url: URL) throws -> URL {
        guard let selectedLibrary else { return url }
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw FixtureConnectionError.invalidOrigin
        }
        components.queryItems = (components.queryItems ?? []) + [
            URLQueryItem(name: "library_id", value: selectedLibrary.id),
        ]
        guard let result = components.url else { throw FixtureConnectionError.invalidOrigin }
        return result
    }

    private func send(_ url: URL, method: String = "GET", body: Data? = nil,
                      maxBytes: Int = 4096, allowExpired: Bool = false,
                      timeout: TimeInterval = 120) async throws -> Data {
        guard allowExpired || Date() < expiresAt else { throw FixtureConnectionError.unauthorized }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        return try await Self.load(request, session: session, maxBytes: maxBytes)
    }

    private static func load(_ request: URLRequest, session: URLSession, maxBytes: Int) async throws -> Data {
        let (data, response) = try await session.data(for: request)
        switch (response as? HTTPURLResponse)?.statusCode {
        case 200 where data.count <= maxBytes: return data
        case 401: throw FixtureConnectionError.unauthorized
        case 403 where data.count <= 4096:
            struct Detail: Decodable { let detail: String }
            if (try? JSONDecoder().decode(Detail.self, from: data))?.detail == "viewer_plex_required" {
                throw LiveLibraryError.plexConnectionRequired
            }
            throw FixtureConnectionError.requestRejected
        case 404: throw LiveLibraryError.copyUnavailable
        case 409: throw LiveLibraryError.resumeSuperseded
        case 415: throw LiveLibraryError.unsupportedCopy
        default: throw FixtureConnectionError.requestRejected
        }
    }
}

public enum LivePlaybackAccessLoss: Equatable, Sendable {
    /// The device link or profile was revoked. Playback cannot continue.
    case profile
    /// The copy is gone. A different copy needs an explicit reload.
    case media
    /// The pass is still authorized but the media plan it played was reclaimed
    /// while paused. The stream can be reopened once at the saved position.
    case planExpired
}

/// Serialize writes for one playback pass and coalesce AVPlayer's pending samples.
@MainActor
public final class LiveResumeWriter {
    private struct Checkpoint {
        let sequence: Int
        let position: Double
    }

    private let onAccessLost: (LivePlaybackAccessLoss) -> Void
    private let save: (Int, Double) async throws -> Void
    private var task: Task<Void, Never>?
    private var pending: Double?
    private var unsaved: Checkpoint?
    private var sequence = 0
    private var superseded = false
    public private(set) var localPosition: Double?

    init(onAccessLost: @escaping (LivePlaybackAccessLoss) -> Void = { _ in },
         save: @escaping (Int, Double) async throws -> Void) {
        self.onAccessLost = onAccessLost
        self.save = save
    }

    public func offer(_ position: Double) {
        guard position.isFinite, position >= 0, localPosition != position else { return }
        localPosition = position
        guard !superseded else { return }
        pending = position
        if task == nil { task = Task { await drain() } }
    }

    public func flush(retryUnsaved: Bool = false) async {
        await task?.value
        guard retryUnsaved, !superseded, task == nil, let unsaved else { return }
        task = Task { await drain(retrying: unsaved) }
        await task?.value
    }

    private func drain(retrying checkpoint: Checkpoint? = nil) async {
        defer { task = nil }
        if let checkpoint { await write(checkpoint) }
        while let position = pending, !superseded {
            pending = nil
            sequence += 1
            await write(Checkpoint(sequence: sequence, position: position))
        }
    }

    private func write(_ checkpoint: Checkpoint) async {
        unsaved = nil
        do {
            try await saveWithRetry(checkpoint.sequence, checkpoint.position)
        } catch LiveLibraryError.resumeSuperseded {
            superseded = true
            pending = nil
        } catch LiveLibraryError.copyUnavailable {
            accessLost(.media)
        } catch FixtureConnectionError.unauthorized {
            accessLost(.profile)
        } catch {
            // A lost response may have saved this exact sample. Retry with the same sequence.
            unsaved = checkpoint
        }
    }

    private func accessLost(_ loss: LivePlaybackAccessLoss) {
        superseded = true
        pending = nil
        onAccessLost(loss)
    }

    private func saveWithRetry(_ seq: Int, _ position: Double) async throws {
        do {
            try await save(seq, position)
        } catch is URLError {
            try await Task.sleep(for: .milliseconds(350))
            try await save(seq, position)
        } catch FixtureConnectionError.requestRejected {
            try await Task.sleep(for: .milliseconds(350))
            try await save(seq, position)
        }
    }
}

