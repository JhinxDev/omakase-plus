import Foundation
import QualificationCore
import Security

enum PhoneKeychain {
    enum Storage {
        case roster(LiveProfileRoster)
        case legacy(LiveLibraryClient.Credential)
    }

    struct Unavailable: Error {}

    private static let lookup: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "dev.omakase.plus.ios",
        kSecAttrAccount as String: "active",
    ]

    static func read() throws -> Storage {
        var query = lookup
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return .roster(LiveProfileRoster()) }
        guard status == errSecSuccess, let data = result as? Data else { throw Unavailable() }
        let decoder = JSONDecoder()
        if let roster = try? decoder.decode(LiveProfileRoster.self, from: data) {
            return .roster(roster)
        }
        return .legacy(try decoder.decode(LiveLibraryClient.Credential.self, from: data))
    }

    static func save(_ roster: LiveProfileRoster) throws {
        let data = try JSONEncoder().encode(roster)
        let value = [kSecValueData as String: data]
        var status = SecItemUpdate(lookup as CFDictionary, value as CFDictionary)
        if status == errSecItemNotFound {
            var item = lookup
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Unavailable() }
    }
}
