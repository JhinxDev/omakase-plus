import Foundation
import QualificationCore
import Security

enum PhoneKeychain {
    enum Storage {
        case roster(LiveProfileRoster)
        case legacy(LiveLibraryClient.Credential)
    }

    enum Failure: Error {
        /// The device is locked and the item cannot be read yet. Try again when the app is active.
        case locked
        /// The item exists but could not be read or decoded.
        case unavailable
    }

    private static let lookup: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "dev.omakase.plus.ios",
        kSecAttrAccount as String: "active",
    ]

    /// Readable after the first unlock, so a prewarmed or background launch while the
    /// phone is locked can still restore the saved profile.
    private static let accessibility = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

    static func read() throws -> Storage {
        var query = lookup
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return .roster(LiveProfileRoster()) }
        if status == errSecInteractionNotAllowed { throw Failure.locked }
        guard status == errSecSuccess, let data = result as? Data else { throw Failure.unavailable }
        let decoder = JSONDecoder()
        if let roster = try? decoder.decode(LiveProfileRoster.self, from: data) {
            return .roster(roster)
        }
        guard let credential = try? decoder.decode(LiveLibraryClient.Credential.self, from: data) else {
            throw Failure.unavailable
        }
        return .legacy(credential)
    }

    /// Saves the roster and moves any older item to the current accessibility class.
    static func save(_ roster: LiveProfileRoster) throws {
        let data = try JSONEncoder().encode(roster)
        let update: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: accessibility,
        ]
        var status = SecItemUpdate(lookup as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var item = lookup
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = accessibility
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Failure.unavailable }
    }
}
