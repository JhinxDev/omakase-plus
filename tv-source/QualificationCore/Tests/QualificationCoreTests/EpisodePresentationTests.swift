import Foundation
import XCTest
@testable import QualificationCore

final class EpisodePresentationTests: XCTestCase {
    func testLegacyEntriesRemainDecodableAndUntitledEpisodesHaveAnExactFallback() throws {
        let entry = try decode(#"{"id":"1-5","title":"  ","note":"S1 E5"}"#)
        XCTAssertEqual(entry.episodeDisplayTitle, "Episode 5")
        XCTAssertNil(entry.durationLabel)
        XCTAssertNil(try decode(#"{"id":"5","title":"Fifth signal","note":"S1 E5"}"#).durationSeconds)
    }

    func testRuntimeUsesKnownPositiveBoundedSecondsAndRoundsMinutesUp() throws {
        for (seconds, label) in [(30, "30 sec"), (60, "1 min"), (1441, "25 min"), (14400, "240 min")] {
            let entry = try decode("{\"id\":\"2\",\"title\":\"Night Crossing\",\"note\":\"S1 E2\",\"duration_seconds\":\(seconds)}")
            XCTAssertEqual(entry.durationLabel, label)
            XCTAssertEqual(entry.episodeDisplayTitle, "Night Crossing")
        }
        for seconds in [-1, 0, 14401] {
            XCTAssertNil(try decode("{\"id\":\"2\",\"title\":\"Night Crossing\",\"note\":\"S1 E2\",\"duration_seconds\":\(seconds)}").durationLabel)
        }
    }

    func testUncheckedAvailabilityDoesNotClaimTheEpisodeIsUnavailable() throws {
        let pending = try decode(#"{"id":"1-5","title":"Fifth signal","note":"S1 E5","availability":"partial"}"#)
        XCTAssertTrue(pending.availabilityPending)
        XCTAssertEqual(pending.unavailableLabel, "Not checked yet")
        let missing = try decode(#"{"id":"1-5","title":"Fifth signal","note":"S1 E5"}"#)
        XCTAssertFalse(missing.availabilityPending)
        XCTAssertEqual(missing.unavailableLabel, "Unavailable")
        let matched = try decode(#"{"id":"1-5","title":"Fifth signal","note":"S1 E5","availability":"partial","watch_path":"/plus/watch/title/tv/42/1/5"}"#)
        XCTAssertFalse(matched.availabilityPending)
        XCTAssertNil(matched.unavailableLabel)
    }

    func testPageKeepsPartialChecksSeparateFromLegacyOrCompletedChecks() throws {
        let decodePage: (String) throws -> LiveLibraryPage = {
            try JSONDecoder().decode(LiveLibraryPage.self, from: Data($0.utf8))
        }
        let page = try decodePage(#"{"items":[],"availability":{"state":"partial","seasons":[1,2],"retry_offset":30}}"#)
        XCTAssertTrue(page.hasUncheckedEpisodes)
        XCTAssertEqual(page.availability?.seasons, [1, 2])
        XCTAssertEqual(page.availability?.retryOffset, 30)
        XCTAssertTrue(try decodePage(#"{"items":[{"id":"1-5","title":"Fifth signal","note":"S1 E5","availability":"partial"}]}"#).hasUncheckedEpisodes)
        XCTAssertFalse(try decodePage(#"{"items":[]}"#).hasUncheckedEpisodes)
        XCTAssertFalse(try decodePage(#"{"items":[],"availability":{"state":"complete"}}"#).hasUncheckedEpisodes)
    }

    func testMediaInfoKeepsExactDimensionsAndDropsUnusableFields() throws {
        let decodeInfo: (String) throws -> LiveMediaInformation = {
            try JSONDecoder().decode(LiveMediaInformation.self, from: Data($0.utf8))
        }
        let info = try decodeInfo(#"{"container":"mkv","width":1920,"height":1080,"video_codec":"hevc","audio_codecs":["eac3","https://private.invalid"],"file_bytes":456670000}"#)
        XCTAssertTrue(info.hasDetails)
        XCTAssertEqual(info.width, 1920)
        XCTAssertEqual(info.height, 1080)
        XCTAssertEqual(info.videoCodec, "hevc")
        XCTAssertEqual(info.audioCodecs, ["eac3"])
        XCTAssertEqual(info.fileBytes, 456_670_000)
        XCTAssertFalse(try decodeInfo(#"{"width":1920,"height":0,"container":"/private.mkv","file_bytes":-1}"#).hasDetails)
    }

    private func decode(_ value: String) throws -> LiveLibraryEntry {
        try JSONDecoder().decode(LiveLibraryEntry.self, from: Data(value.utf8))
    }
}
