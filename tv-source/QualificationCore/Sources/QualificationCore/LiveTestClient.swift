import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// How the server will hand this item's media to the app.
///
/// `mp4` and `hls` stay on AVPlayer, which demonstrably handles them. `direct` is
/// a provider original (MKV/H.264/HEVC with embedded ASS/PGS and alternate audio)
/// served as an HTTP range URL and routed to the VLC engine.
public enum LiveMediaFormat: String, Sendable, CaseIterable {
    case mp4
    case hls
    case direct
}

public struct LiveMediaInformation: Decodable, Sendable, Equatable {
    public let container: String?
    public let videoCodec: String?
    public let audioCodecs: [String]
    public let width: Int?
    public let height: Int?
    public let fileBytes: Int64?

    private enum CodingKeys: String, CodingKey {
        case container, videoCodec = "video_codec", audioCodecs = "audio_codecs"
        case width, height, fileBytes = "file_bytes"
    }

    public init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        container = Self.safeToken(try? fields.decode(String.self, forKey: .container))
        videoCodec = Self.safeToken(try? fields.decode(String.self, forKey: .videoCodec))
        audioCodecs = (try? fields.decode([String].self, forKey: .audioCodecs))?
            .prefix(4).compactMap { Self.safeToken($0) } ?? []
        if let w = try? fields.decode(Int.self, forKey: .width),
           let h = try? fields.decode(Int.self, forKey: .height),
           (1...16384).contains(w), (1...16384).contains(h) {
            width = w
            height = h
        } else {
            width = nil
            height = nil
        }
        fileBytes = (try? fields.decode(Int64.self, forKey: .fileBytes)).flatMap {
            (1...10_000_000_000_000).contains($0) ? $0 : nil
        }
    }

    public var hasDetails: Bool {
        container != nil || videoCodec != nil || !audioCodecs.isEmpty || width != nil || fileBytes != nil
    }

    private static func safeToken(_ value: String?) -> String? {
        guard let value, value.range(of: #"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,39}$"#,
                                     options: .regularExpression) == value.startIndex..<value.endIndex
        else { return nil }
        return value
    }
}

public struct LiveTestPass: Sendable {
    public let card: LibraryCard
    public let mediaID: String
    /// The playable URL. For `direct` this is the server-supplied range URL.
    ///
    /// Secret material: it can be a signed provider URL. Never log, persist or
    /// report it, and never put it in a diagnostic string.
    public let mediaURL: URL
    public let format: LiveMediaFormat
    /// Request headers the media URL needs. Empty for `mp4`/`hls`, where the
    /// pass-scoped cookie carries authorization. Values are secrets.
    public let mediaHeaders: [String: String]
    public let duration: Double
    public let position: Double
    public let expiresAt: Date
    public let nextWatchPath: String?
    public let supportsResumeSync: Bool
    public let supportsAccessCheck: Bool
    public let hasServerResume: Bool
    public let episodeName: String?
    public let audioLanguages: [String]
    public let subtitleLanguages: [String]
    public let mediaInformation: LiveMediaInformation?
    let resumeScope: String?
    let watchPath: String?
    private let cookie: HTTPCookie
    private let playlist: String?

    public var mediaCookies: [HTTPCookie] { [cookie] }

    public var isHLS: Bool { format == .hls }
    public var isDirect: Bool { format == .direct }
    public var isGrowingHLS: Bool { isHLS && playlist != "vod" }

    /// Fixed HLS can warm the saved position before returning its master
    /// playlist. Older servers can serve the same playlist without the hint.
    public func mediaURL(forResumeAt position: Double?) -> URL {
        guard isHLS, !isGrowingHLS, let position, position.isFinite,
              position > 0, position < duration,
              var components = URLComponents(url: mediaURL, resolvingAgainstBaseURL: false)
        else { return mediaURL }
        let milliseconds = Int((position * 1000).rounded(.down))
        guard milliseconds > 0 else { return mediaURL }
        components.queryItems = [URLQueryItem(name: "start_ms", value: String(milliseconds))]
        return components.url ?? mediaURL
    }

    init(reply: LiveTestReply, origin: FixtureOrigin, watchPath: String? = nil) throws {
        guard let format = LiveMediaFormat(rawValue: reply.mediaFormat ?? LiveMediaFormat.mp4.rawValue)
        else { throw FixtureConnectionError.invalidResponse }
        // A playlist only describes HLS; a direct item is one range URL instead.
        guard reply.mediaPlaylist == nil || (format == .hls
                && ["event", "vod"].contains(reply.mediaPlaylist ?? ""))
        else { throw FixtureConnectionError.invalidResponse }
        let directURL: URL?
        let headers: [String: String]
        if format == .direct {
            guard let validated = Self.headers(reply.mediaHeaders) else {
                throw FixtureConnectionError.invalidResponse
            }
            if let path = reply.mediaPath {
                let pathParts = ["plus", "api", "native-tv-test", "v1", "direct", reply.mediaID, reply.mediaToken]
                guard origin.url.scheme == "https", reply.mediaRangeURL == nil, validated.isEmpty,
                      path == "/" + pathParts.joined(separator: "/") else {
                    throw FixtureConnectionError.invalidResponse
                }
                directURL = pathParts.reduce(origin.url) { $0.appendingPathComponent($1) }
            } else {
                guard let url = Self.rangeURL(reply.mediaRangeURL) else {
                    throw FixtureConnectionError.invalidResponse
                }
                directURL = url
            }
            headers = validated
        } else {
            // The cookie and playlist path carry authorization for the other formats.
            guard reply.mediaPath == nil, reply.mediaRangeURL == nil, reply.mediaHeaders?.isEmpty ?? true
            else { throw FixtureConnectionError.invalidResponse }
            directURL = nil
            headers = [:]
        }
        guard let target = reply.identity.validated?.target,
              let identity = LibraryIdentity(libraryID: reply.identity.libraryID, target: target,
                                             mediaVersion: reply.identity.mediaVersion,
                                             viewerScope: reply.resumeScope),
              let host = origin.url.host,
              origin.url.scheme == "https", !reply.title.isEmpty, reply.title.count <= 120,
              !reply.title.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              reply.mediaID.utf8.count == 32, reply.mediaID.utf8.allSatisfy({
                  (48...57).contains($0) || (97...102).contains($0)
              }),
              FixtureClient.tokenIsValid(reply.mediaToken),
              (1...43_200).contains(reply.duration), (0..<reply.duration).contains(reply.position),
              (1...43_200).contains(reply.expiresIn)
        else { throw FixtureConnectionError.invalidResponse }
        let path = "/plus/api/native-tv-test/v1/media/\(reply.mediaID)"
        let expires = Date().addingTimeInterval(TimeInterval(reply.expiresIn))
        guard let cookie = HTTPCookie(properties: [
            .name: "omakase_tv_test_media", .value: reply.mediaToken,
            .domain: host, .path: path, .secure: "TRUE", .discard: "TRUE",
            .expires: expires,
        ]) else { throw FixtureConnectionError.invalidResponse }
        self.cookie = cookie
        self.mediaID = reply.mediaID
        self.format = format
        self.mediaHeaders = headers
        self.playlist = reply.mediaPlaylist
        let media = origin.url.appendingPathComponent(String(path.dropFirst()))
        self.mediaURL = directURL ?? (format == .hls ? media.appendingPathComponent("index.m3u8") : media)
        self.duration = reply.duration
        self.position = reply.position
        self.supportsResumeSync = reply.resumeSync == true
        self.supportsAccessCheck = reply.accessCheck == true
        self.hasServerResume = reply.resumeUpdatedAt.map { (0...1e12).contains($0) } ?? false
        self.resumeScope = reply.resumeScope
        self.watchPath = watchPath
        self.expiresAt = expires
        self.nextWatchPath = Self.validNextPath(reply.nextWatchPath, for: identity.target)
        let episode = reply.identity.episodeNumber > 0
            ? "S\(reply.identity.seasonNumber) E\(reply.identity.episodeNumber)" : nil
        self.episodeName = episode == nil ? nil : Self.displayText(reply.episodeName, limit: 120)
        self.audioLanguages = (reply.audioLanguages ?? []).prefix(4).compactMap { Self.displayText($0, limit: 48) }
        self.subtitleLanguages = (reply.subtitleLanguages ?? []).prefix(4).compactMap { Self.displayText($0, limit: 48) }
        self.mediaInformation = reply.mediaInformation.flatMap { $0.hasDetails ? $0 : nil }
        self.card = LibraryCard(identity: identity, title: reply.title, episodeTitle: episode,
                                synopsis: episode == nil ? "" : Self.displayText(reply.episodeSummary, limit: 650) ?? "")
    }

    /// Validates a server-supplied range URL. Only HTTPS is accepted because a
    /// signed provider URL over plain HTTP would leak its own token.
    static func rangeURL(_ raw: String?) -> URL? {
        guard let raw, !raw.isEmpty, raw.count <= 2048,
              !raw.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              raw == raw.trimmingCharacters(in: .whitespacesAndNewlines),
              let parts = URLComponents(string: raw), parts.scheme?.lowercased() == "https",
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.fragment == nil,
              let url = parts.url
        else { return nil }
        return url
    }

    /// Header names this app will not accept from the server: they would let the
    /// reply redefine transport semantics or shadow the pass-scoped cookie.
    private static let reservedHeaders: Set<String> = [
        "host", "cookie", "range", "if-range", "content-length", "connection",
        "transfer-encoding", "accept-encoding", "user-agent",
    ]

    /// Validates media headers. Rejects anything that could split the request
    /// (CR/LF) or smuggle a transport override. Returns nil for an invalid set so
    /// the whole pass is refused instead of played with partial credentials.
    static func headers(_ raw: [String: String]?) -> [String: String]? {
        guard let raw else { return [:] }
        guard raw.count <= 8 else { return nil }
        var result: [String: String] = [:]
        var seen = Set<String>()
        for (name, value) in raw {
            let lowered = name.lowercased()
            guard (1...64).contains(name.count), !value.isEmpty, value.count <= 512,
                  !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
                  isToken(name), !reservedHeaders.contains(lowered),
                  !lowered.hasPrefix("proxy-"), seen.insert(lowered).inserted
            else { return nil }
            result[lowered] = value
        }
        return result
    }

    private static func isToken(_ name: String) -> Bool {
        let separators = CharacterSet(charactersIn: "!#$%&'*+-.^_`|~")
        let allowed = CharacterSet.alphanumerics.union(separators)
        return name.unicodeScalars.allSatisfy(allowed.contains)
    }

    private static func displayText(_ value: String?, limit: Int) -> String? {
        guard let value, !value.isEmpty, value.count <= limit,
              !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        else { return nil }
        return value
    }

    private static func validNextPath(_ path: String?, for target: LibraryTarget) -> String? {
        guard let path else { return nil }
        switch target {
        case let .anime(_, _, episode) where episode < 5000:
            let pattern = "^/plus/watch/[1-9][0-9]{0,17}/\(episode + 1)$"
            return path.range(of: pattern, options: .regularExpression) == path.startIndex..<path.endIndex
                ? path : nil
        case let .tv(id, season, episode) where episode < 1000:
            return path == "/plus/watch/title/tv/\(id)/\(season)/\(episode + 1)" ? path : nil
        default:
            return nil
        }
    }
}

struct LiveTestReply: Decodable {
    let identity: FixtureWireIdentity
    let title: String
    let mediaID: String
    let mediaToken: String
    let mediaFormat: String?
    let mediaPlaylist: String?
    /// Same-origin path for a server-issued direct pass.
    let mediaPath: String?
    /// HTTPS range URL for `media_format: "direct"`. Secret: never logged.
    let mediaRangeURL: String?
    /// Additional request headers for the range URL. Values are secrets.
    let mediaHeaders: [String: String]?
    let duration: Double
    let position: Double
    let expiresIn: Int
    let nextWatchPath: String?
    let resumeSync: Bool?
    let accessCheck: Bool?
    let resumeUpdatedAt: Double?
    let resumeScope: String?
    let episodeName: String?
    let episodeSummary: String?
    let audioLanguages: [String]?
    let subtitleLanguages: [String]?
    let mediaInformation: LiveMediaInformation?

    enum CodingKeys: String, CodingKey {
        case identity, title, mediaID = "media_id", mediaToken = "media_token"
        case mediaFormat = "media_format", mediaPlaylist = "media_playlist"
        case mediaPath = "media_url", mediaRangeURL = "media_range_url", mediaHeaders = "media_headers"
        case duration = "duration_seconds", position = "position_seconds", expiresIn = "expires_in"
        case nextWatchPath = "next_watch_path"
        case resumeSync = "resume_sync", resumeUpdatedAt = "resume_updated_at"
        case accessCheck = "access_check"
        case resumeScope = "resume_scope"
        case episodeName = "episode_name", episodeSummary = "episode_summary"
        case audioLanguages = "audio_languages", subtitleLanguages = "subtitle_languages"
        case mediaInformation = "media_info"
    }
}

public struct LiveTestClient: Sendable {
    private struct Redeem: Encodable { let code: String }
    private let origin: FixtureOrigin
    private let code: String
    private let session: URLSession

    public init(invitation: String) throws {
        let parts = invitation.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: "#", omittingEmptySubsequences: false)
        guard parts.count == 2 else { throw FixtureConnectionError.invalidCode }
        let normalized = parts[1].uppercased()
        guard normalized.utf8.count == 20, normalized.utf8.allSatisfy({
            (48...57).contains($0) || (65...70).contains($0)
        }) else { throw FixtureConnectionError.invalidCode }
        origin = try FixtureOrigin(String(parts[0]))
        code = normalized
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.timeoutIntervalForResource = 12
        session = URLSession(configuration: configuration, delegate: RefuseRedirects(), delegateQueue: nil)
    }

    public func redeem() async throws -> LiveTestPass {
        var request = URLRequest(url: origin.url.appendingPathComponent("plus/api/native-tv-test/v1/redeem"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Redeem(code: code))
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200, data.count <= 4096
        else { throw FixtureConnectionError.requestRejected }
        return try LiveTestPass(reply: JSONDecoder().decode(LiveTestReply.self, from: data), origin: origin)
    }
}
