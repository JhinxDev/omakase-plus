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

    private func decode(_ value: String) throws -> LiveLibraryEntry {
        try JSONDecoder().decode(LiveLibraryEntry.self, from: Data(value.utf8))
    }
}
