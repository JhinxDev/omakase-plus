import Foundation

struct TVTopShelfRecency {
    private let withFraction = ISO8601DateFormatter()
    private let standard = ISO8601DateFormatter()

    init() {
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    }

    func date(_ value: String?) -> Date {
        guard let value else { return .distantPast }
        return withFraction.date(from: value) ?? standard.date(from: value) ?? .distantPast
    }
}

enum TVTopShelfActionKind: String, CaseIterable, Codable, Sendable {
    case resume, details

    var sectionTitle: String {
        switch self {
        case .resume: "Continue Watching"
        case .details: "Watchlist"
        }
    }
}

struct TVTopShelfLink: Equatable, Sendable {
    static let scheme = "omakaseplus-tv"

    let id: UUID
    let action: TVTopShelfActionKind

    var url: URL? {
        var components = URLComponents()
        components.scheme = Self.scheme
        components.host = "top-shelf"
        components.path = "/\(action.rawValue)/\(id.uuidString.lowercased())"
        return components.url
    }

    init(id: UUID, action: TVTopShelfActionKind) {
        self.id = id
        self.action = action
    }

    init?(url: URL) {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme == Self.scheme, components.host == "top-shelf",
              components.user == nil, components.password == nil, components.port == nil,
              components.query == nil, components.fragment == nil else { return nil }
        let path = components.percentEncodedPath.split(separator: "/", omittingEmptySubsequences: false)
        guard path.count == 3, path[0].isEmpty,
              let action = TVTopShelfActionKind(rawValue: String(path[1])),
              let id = UUID(uuidString: String(path[2])),
              path[2] == id.uuidString.lowercased() else { return nil }
        self.init(id: id, action: action)
    }
}

struct TVTopShelfCard: Codable, Sendable {
    let id: UUID
    let action: TVTopShelfActionKind
    let kind: String
    let match: String
    let title: String
    let progress: Double?

    var link: TVTopShelfLink { TVTopShelfLink(id: id, action: action) }
}

struct TVTopShelfSnapshot: Codable, Sendable {
    static let maximumCards = 16
    let scope: String
    let expiresAt: Date
    let cards: [TVTopShelfCard]

    func card(for link: TVTopShelfLink) -> TVTopShelfCard? {
        cards.first { $0.link == link }
    }
}

/// This app-group directory contains display text, JPEGs and opaque identifiers only.
struct TVTopShelfStore {
    let directory: URL

    #if os(tvOS)
    static func shared() -> Self? {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "OmakaseTopShelfAppGroup") as? String,
              let container = topShelfGroupCandidates(configured: group, bundleID: Bundle.main.bundleIdentifier)
                  .lazy.compactMap({ FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: $0) })
                  .first
        else { return nil }
        return Self(directory: container.appendingPathComponent("Library/Caches/TopShelf-v1", isDirectory: true))
    }
    #endif

    func snapshot(now: Date = Date()) -> TVTopShelfSnapshot? {
        let url = directory.appendingPathComponent("shelf.json")
        guard let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize,
              (1...32_768).contains(size), let data = try? Data(contentsOf: url), data.count <= 32_768,
              let snapshot = try? JSONDecoder().decode(TVTopShelfSnapshot.self, from: data),
              snapshot.expiresAt > now, snapshot.cards.count <= TVTopShelfSnapshot.maximumCards,
              Set(snapshot.cards.map(\.id)).count == snapshot.cards.count else { return nil }
        return snapshot
    }

    func artworkURL(for id: UUID) -> URL {
        directory.appendingPathComponent(id.uuidString + ".jpg", isDirectory: false)
    }

    func publish(_ snapshot: TVTopShelfSnapshot, artwork: [UUID: Data]) throws {
        let manager = FileManager.default
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        for card in snapshot.cards {
            if let image = artwork[card.id] {
                try image.write(to: artworkURL(for: card.id), options: .atomic)
            }
        }
        let data = try JSONEncoder().encode(snapshot)
        try data.write(to: directory.appendingPathComponent("shelf.json"), options: .atomic)
        let files = (try? manager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        let active = Set(snapshot.cards.map { artworkURL(for: $0.id).lastPathComponent })
        for file in files where file.pathExtension == "jpg" && !active.contains(file.lastPathComponent) {
            try? manager.removeItem(at: file)
        }
    }

    func clear() {
        // Remove the manifest first so a reader cannot start a new stale load.
        try? FileManager.default.removeItem(at: directory.appendingPathComponent("shelf.json"))
        try? FileManager.default.removeItem(at: directory)
    }
}

func topShelfGroupCandidates(configured group: String, bundleID: String?) -> [String] {
    let groupPrefix = "group."
    let groupSuffix = ".top-shelf"
    guard group.hasPrefix(groupPrefix), group.hasSuffix(groupSuffix), let bundleID else { return [group] }
    let host = group.dropFirst(groupPrefix.count).dropLast(groupSuffix.count)
    guard !host.isEmpty else { return [group] }
    let prefix = String(host) + "."
    guard bundleID.hasPrefix(prefix),
          let team = bundleID.dropFirst(prefix.count).split(separator: ".").first,
          team.range(of: #"^[A-Z0-9]{10}$"#, options: .regularExpression) != nil else { return [group] }
    return ["\(group).\(team)", group]
}
