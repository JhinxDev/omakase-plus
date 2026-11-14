import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum FixtureConnectionError: Error {
    case invalidOrigin, invalidCode, invalidResponse, unauthorized, requestRejected
}

public struct FixtureOrigin: Sendable {
    let url: URL

    public init(_ text: String, allowLoopbackHTTP: Bool = false) throws {
        guard text == text.trimmingCharacters(in: .whitespacesAndNewlines),
              var parts = URLComponents(string: text), let host = parts.host,
              !host.isEmpty, parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              parts.port.map({ (1...65535).contains($0) }) ?? true,
              parts.scheme == "https" ||
                (allowLoopbackHTTP && parts.scheme == "http" && ["localhost", "127.0.0.1"].contains(host))
        else { throw FixtureConnectionError.invalidOrigin }
        parts.path = ""
        guard let url = parts.url else { throw FixtureConnectionError.invalidOrigin }
        self.url = url
    }

    func endpoint(_ path: String) -> URL {
        url.appendingPathComponent("v1").appendingPathComponent(path)
    }
}

public struct FixtureWireIdentity: Codable, Equatable, Sendable {
    let libraryID: String
    let domain: String
    let canonicalID: String
    let seasonNumber: Int
    let episodeNumber: Int
    let mediaVersion: String

    enum CodingKeys: String, CodingKey {
        case libraryID = "library_id", domain, canonicalID = "canonical_id"
        case seasonNumber = "season_number", episodeNumber = "episode_number", mediaVersion = "media_version"
    }

    var validated: LibraryIdentity? {
        let target: LibraryTarget
        switch domain {
        case "anime":
            guard let id = Int(canonicalID), String(id) == canonicalID else { return nil }
            target = .anime(anilistID: id, season: seasonNumber, episode: episodeNumber)
        case "anime-film":
            guard let id = Int(canonicalID), String(id) == canonicalID, seasonNumber == 0, episodeNumber == 0
            else { return nil }
            target = .animeFilm(anilistID: id)
        case "movie":
            guard seasonNumber == 0, episodeNumber == 0 else { return nil }
            target = .movie(imdbID: canonicalID)
        case "cloud":
            guard seasonNumber == 0, episodeNumber == 0 else { return nil }
            target = .cloud(fileID: canonicalID)
        case "tv":
            guard let id = Int(canonicalID), String(id) == canonicalID else { return nil }
            target = .tv(tmdbID: id, season: seasonNumber, episode: episodeNumber)
        default:
            return nil
        }
        return LibraryIdentity(libraryID: libraryID, target: target, mediaVersion: mediaVersion)
    }
}

public struct FixtureRemoteCard: Decodable, Sendable {
    public let card: LibraryCard
    public let position: Double
    let wireIdentity: FixtureWireIdentity

    enum CodingKeys: String, CodingKey {
        case identity, title, episodeTitle = "episode_title", position = "position_seconds"
    }

    public init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let wire = try values.decode(FixtureWireIdentity.self, forKey: .identity)
        let title = try values.decode(String.self, forKey: .title)
        let episode = try values.decodeIfPresent(String.self, forKey: .episodeTitle)
        let position = try values.decode(Double.self, forKey: .position)
        guard let identity = wire.validated, Self.labelIsValid(title), episode.map(Self.labelIsValid) ?? true,
              position.isFinite, (0...30).contains(position)
        else { throw FixtureConnectionError.invalidResponse }
        wireIdentity = wire
        card = LibraryCard(identity: identity, title: title, episodeTitle: episode,
                           synopsis: "Synthetic clip streamed from the test library.")
        self.position = position
    }

    private static func labelIsValid(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 120 && !value.unicodeScalars.contains {
            CharacterSet.controlCharacters.contains($0)
        }
    }
}

public struct FixtureRemotePlayback: Sendable {
    let sessionID: String
    public let url: URL
    public let position: Double
}

public struct FixtureDevice: Sendable {
    fileprivate let token: String
    public let accountLabel: String
}

final class RefuseRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public struct FixtureClient: Sendable {
    private struct PairRequest: Encodable { let code: String }
    private struct PairResponse: Decodable { let device_token: String; let account_label: String }
    private struct CatalogResponse: Decodable { let cards: [FixtureRemoteCard] }
    private struct StartRequest: Encodable { let identity: FixtureWireIdentity; let resume: Bool }
    private struct StartResponse: Decodable {
        let session_id: String
        let media_grant: String
        let identity: FixtureWireIdentity
        let position_seconds: Double
    }
    private struct CloseRequest: Encodable { let position_seconds: Double? }
    private struct Empty: Decodable {}

    private let origin: FixtureOrigin
    private let session: URLSession

    public init(origin: FixtureOrigin) {
        self.origin = origin
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false
        config.urlCache = nil
        config.timeoutIntervalForRequest = 8
        config.timeoutIntervalForResource = 12
        session = URLSession(configuration: config, delegate: RefuseRedirects(), delegateQueue: nil)
    }

    public func pair(code: String) async throws -> FixtureDevice {
        let clean = code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard clean.utf8.count == 16, clean.utf8.allSatisfy({ (48...57).contains($0) || (65...70).contains($0) })
        else { throw FixtureConnectionError.invalidCode }
        let result: PairResponse = try await request("pair", body: JSONEncoder().encode(PairRequest(code: clean)))
        guard Self.tokenIsValid(result.device_token), ["Test account 1", "Test account 2"].contains(result.account_label)
        else { throw FixtureConnectionError.invalidResponse }
        return FixtureDevice(token: result.device_token, accountLabel: result.account_label)
    }

    public func catalog(device: FixtureDevice) async throws -> [FixtureRemoteCard] {
        let result: CatalogResponse = try await request("catalog", device: device)
        guard result.cards.count <= 12, Set(result.cards.map { $0.card.id }).count == result.cards.count
        else { throw FixtureConnectionError.invalidResponse }
        return result.cards
    }

    public func start(_ card: FixtureRemoteCard, resume: Bool, device: FixtureDevice) async throws -> FixtureRemotePlayback {
        let body = try JSONEncoder().encode(StartRequest(identity: card.wireIdentity, resume: resume))
        let result: StartResponse = try await request("play", device: device, body: body)
        guard Self.tokenIsValid(result.session_id), Self.tokenIsValid(result.media_grant),
              result.identity == card.wireIdentity, result.position_seconds.isFinite,
              (0..<30).contains(result.position_seconds), resume || result.position_seconds == 0
        else { throw FixtureConnectionError.invalidResponse }
        return FixtureRemotePlayback(sessionID: result.session_id,
                                     url: origin.endpoint("media/\(result.media_grant)"),
                                     position: result.position_seconds)
    }

    public func close(_ play: FixtureRemotePlayback, position: Double?, device: FixtureDevice) async throws {
        let body = try JSONEncoder().encode(CloseRequest(position_seconds: position))
        let _: Empty = try await request("play/\(play.sessionID)/close", device: device, body: body)
    }

    public func logout(device: FixtureDevice) async throws {
        let _: Empty = try await request("logout", device: device, body: Data("{}".utf8))
    }

    private func request<Response: Decodable>(_ path: String, device: FixtureDevice? = nil,
                                              body: Data? = nil) async throws -> Response {
        var request = URLRequest(url: origin.endpoint(path))
        if let device { request.setValue("Bearer \(device.token)", forHTTPHeaderField: "Authorization") }
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw FixtureConnectionError.invalidResponse }
        if response.statusCode == 401 { throw FixtureConnectionError.unauthorized }
        guard response.statusCode == 200 else { throw FixtureConnectionError.requestRejected }
        guard data.count <= 32_768 else { throw FixtureConnectionError.invalidResponse }
        return try JSONDecoder().decode(Response.self, from: data)
    }

    static func tokenIsValid(_ token: String) -> Bool {
        token.utf8.count == 43 && token.utf8.allSatisfy {
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45 || $0 == 95
        }
    }
}
