import Foundation
import XCTest
@testable import QualificationCore

final class LiveProfileRosterTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    func testProfilesRememberSelectionAndSeparateIdenticalAccountIDsOnDifferentServers() throws {
        let first = entry(account: "42")
        let second = entry(account: "42", host: "other.example.com")
        var roster = LiveProfileRoster()
        try roster.select(first, now: now)
        try roster.select(second, now: now)

        let stored = try JSONEncoder().encode(roster)
        let restored = try JSONDecoder().decode(LiveProfileRoster.self, from: stored)
        XCTAssertEqual(restored.entries.map(\.id), [first.id, second.id])
        XCTAssertEqual(restored.selected?.id, second.id)
        XCTAssertEqual(restored.selected?.credential.token, second.credential.token)
    }

    func testReapprovalReplacesOneCredentialAndRevocationNeverSelectsAnotherPerson() throws {
        let first = entry(account: "42", token: "A")
        let second = entry(account: "43", token: "B")
        let replacement = entry(account: "42", token: "C")
        var roster = LiveProfileRoster()
        try roster.select(first, now: now)
        try roster.select(second, now: now)
        XCTAssertEqual(try roster.select(replacement, now: now)?.token, first.credential.token)
        XCTAssertEqual(roster.entries.map(\.id), [first.id, second.id])
        XCTAssertEqual(roster.selected?.credential.token, replacement.credential.token)

        roster.remove(replacement.id)
        XCTAssertNil(roster.selected)
        XCTAssertEqual(roster.entries.map(\.id), [second.id])
        roster.prune(now: now.addingTimeInterval(31))
        XCTAssertTrue(roster.entries.isEmpty)
    }

    func testCapacityDoesNotEvictExistingProfiles() throws {
        var roster = LiveProfileRoster()
        for index in 1...LiveProfileRoster.capacity {
            try roster.select(entry(account: String(index)), now: now)
        }
        let selected = roster.selected?.id
        XCTAssertThrowsError(try roster.select(entry(account: "99"), now: now))
        XCTAssertEqual(roster.entries.count, LiveProfileRoster.capacity)
        XCTAssertEqual(roster.selected?.id, selected)
    }

    func testProfileResponseRequiresAccountIdentityAndSuppliesFallbackName() throws {
        let response = Data(#"{"id":"42","name":"   ","has_avatar":false}"#.utf8)
        let profile = try LiveLibraryProfile.decode(response)
        XCTAssertEqual(profile.name, "Profile")
        XCTAssertFalse(profile.hasAvatar)
        for id in ["", "0", "01", "../42", "42\n"] {
            let invalid = try JSONSerialization.data(withJSONObject: ["id": id, "name": "A", "has_avatar": false])
            XCTAssertThrowsError(try LiveLibraryProfile.decode(invalid))
        }
    }

    private func entry(account: String, host: String = "tv.example.com", token: String = "X") -> LiveSavedProfile {
        let profile = LiveLibraryProfile(accountID: account, name: "Viewer", hasAvatar: false)
        let credential = LiveLibraryClient.Credential(origin: "https://\(host)",
                                                       token: String(repeating: token, count: 43),
                                                       expiresAt: now.addingTimeInterval(30))
        return LiveSavedProfile(profile: profile, credential: credential)
    }
}
