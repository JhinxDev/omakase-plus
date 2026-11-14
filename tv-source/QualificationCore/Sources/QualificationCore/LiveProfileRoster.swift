import Foundation

public struct LiveLibraryProfile: Codable, Sendable {
    public let accountID: String
    public let name: String
    public let hasAvatar: Bool

    private enum CodingKeys: String, CodingKey {
        case accountID = "id", name, hasAvatar = "has_avatar"
    }

    static func decode(_ data: Data) throws -> Self {
        let profile = try JSONDecoder().decode(Self.self, from: data)
        guard let number = Int64(profile.accountID), number > 0,
              String(number) == profile.accountID, profile.name.utf8.count <= 256
        else { throw FixtureConnectionError.invalidResponse }
        let visible = profile.name.trimmingCharacters(in: .whitespacesAndNewlines)
        return Self(accountID: profile.accountID, name: visible.isEmpty ? "Profile" : visible,
                    hasAvatar: profile.hasAvatar)
    }
}

public struct LiveSavedProfile: Codable, Identifiable, Sendable {
    public struct ID: Codable, Hashable, Sendable {
        let origin: String
        let accountID: String
    }

    public let profile: LiveLibraryProfile
    public let credential: LiveLibraryClient.Credential
    public var expiresAt: Date { credential.expiresAt }
    public var serverHost: String { URL(string: credential.origin)?.host ?? "" }
    public var serverAddress: String {
        guard let port = URL(string: credential.origin)?.port else { return serverHost }
        return "\(serverHost):\(port)"
    }
    public var id: ID { ID(origin: credential.origin, accountID: profile.accountID) }

    public init(profile: LiveLibraryProfile, credential: LiveLibraryClient.Credential) {
        self.profile = profile
        self.credential = credential
    }
}

public struct LiveProfileRoster: Codable, Sendable {
    public enum RosterError: Error { case full }
    public static let capacity = 8
    public private(set) var entries: [LiveSavedProfile] = []
    private var selectedID: LiveSavedProfile.ID?

    public init() {}

    public var selected: LiveSavedProfile? { entries.first { $0.id == selectedID } }

    @discardableResult
    public mutating func select(_ entry: LiveSavedProfile, now: Date = Date()) throws -> LiveLibraryClient.Credential? {
        prune(now: now)
        guard entry.expiresAt > now else { throw FixtureConnectionError.unauthorized }
        let replaced: LiveLibraryClient.Credential?
        if let index = entries.firstIndex(where: { $0.id == entry.id }) {
            let old = entries[index].credential
            replaced = old.token == entry.credential.token ? nil : old
            entries[index] = entry
        } else {
            guard entries.count < Self.capacity else { throw RosterError.full }
            replaced = nil
            entries.append(entry)
        }
        selectedID = entry.id
        return replaced
    }

    public mutating func remove(_ id: LiveSavedProfile.ID) {
        entries.removeAll { $0.id == id }
        if selectedID == id { selectedID = nil }
    }

    public mutating func prune(now: Date = Date()) {
        entries.removeAll { $0.expiresAt <= now }
        if selected == nil { selectedID = nil }
    }
}
