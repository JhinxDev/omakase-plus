import Foundation
import XCTest
@testable import QualificationCore

final class LibraryFixtureTests: XCTestCase {
    private let local = URL(fileURLWithPath: "/app/baseline.mp4")
    private let alternateLibrary = String(repeating: "2", count: 32)
    private let alternateVersion = String(repeating: "b", count: 64)

    func testAllThreeKindsUseOneOfflineAssetWithDistinctExactIdentities() throws {
        let fixture = try XCTUnwrap(LibraryFixture(baselineURL: local))
        XCTAssertEqual(fixture.cards.map(\.kind), [.anime, .movie, .tv])
        XCTAssertEqual(Set(fixture.cards.map { $0.identity.resumeIdentity }).count, 3)
        for card in fixture.cards {
            let media = try XCTUnwrap(fixture.media(for: card.id))
            XCTAssertEqual(media.url, local)
            XCTAssertEqual(media.resumeIdentity, card.id.resumeIdentity)
            let foreign = try XCTUnwrap(LibraryIdentity(libraryID: alternateLibrary, target: card.id.target,
                                                       mediaVersion: card.id.mediaVersion))
            let otherCopy = try XCTUnwrap(LibraryIdentity(libraryID: card.id.libraryID, target: card.id.target,
                                                         mediaVersion: alternateVersion))
            XCTAssertNil(fixture.media(for: foreign))
            XCTAssertNil(fixture.media(for: otherCopy))
        }
        let network = try XCTUnwrap(URL(string: "https://example.invalid/baseline.mp4"))
        XCTAssertNil(LibraryFixture(baselineURL: network))
        XCTAssertNil(LibraryFixture(baselineURL: URL(fileURLWithPath: "/app/other.mp4")))
    }

    func testResumeSurvivesRoundTripWithoutCrossingEpisodeSeasonLibraryOrVersion() throws {
        let original = try makeIdentity()
        let episode = try makeIdentity(target: .tv(tmdbID: 4, season: 1, episode: 3))
        let season = try makeIdentity(target: .tv(tmdbID: 4, season: 2, episode: 2))
        let otherTitle = try makeIdentity(target: .tv(tmdbID: 5, season: 1, episode: 2))
        let otherLibrary = try makeIdentity(libraryID: alternateLibrary)
        let otherCopy = try makeIdentity(version: alternateVersion)
        var ledger = ResumeLedger()
        ledger.checkpoint(identity: original.resumeIdentity, position: 12, duration: 30, reachedEnd: false, now: 100)
        for key in [episode, season, otherTitle, otherLibrary, otherCopy] {
            XCTAssertNil(ledger.record(for: key.resumeIdentity, now: 101))
        }
        ledger.checkpoint(identity: otherCopy.resumeIdentity, position: 21, duration: 30, reachedEnd: false, now: 101)
        let restored = ResumeLedger.decode(try ledger.encoded())
        XCTAssertEqual(restored.record(for: original.resumeIdentity, now: 102)?.position, 12)
        XCTAssertEqual(restored.record(for: otherCopy.resumeIdentity, now: 102)?.position, 21)
    }

    func testIdentityRejectsMissingVersionAndMalformedCoordinates() throws {
        for target: LibraryTarget in [.anime(anilistID: 1, season: 1, episode: 0),
                                      .anime(anilistID: 1, season: 0, episode: 1),
                                      .movie(imdbID: "tt123456x"), .tv(tmdbID: 1, season: 1, episode: 0)] {
            XCTAssertNil(LibraryIdentity(libraryID: LibraryFixture.libraryID, target: target,
                                         mediaVersion: LibraryFixture.mediaVersion))
        }
        let target = LibraryTarget.animeFilm(anilistID: 1)
        XCTAssertEqual(try makeIdentity(target: target).target.kind, .anime)
        XCTAssertNotEqual(try makeIdentity(target: target).resumeIdentity,
                          try makeIdentity(target: .anime(anilistID: 1, season: 1, episode: 1)).resumeIdentity)
        XCTAssertNil(LibraryIdentity(libraryID: LibraryFixture.libraryID, target: target, mediaVersion: ""))
        XCTAssertNil(LibraryIdentity(libraryID: "default", target: target, mediaVersion: LibraryFixture.mediaVersion))
    }

    private func makeIdentity(target: LibraryTarget = .tv(tmdbID: 4, season: 1, episode: 2),
                              libraryID: String = LibraryFixture.libraryID,
                              version: String = LibraryFixture.mediaVersion) throws -> LibraryIdentity {
        try XCTUnwrap(LibraryIdentity(libraryID: libraryID, target: target, mediaVersion: version))
    }
}
