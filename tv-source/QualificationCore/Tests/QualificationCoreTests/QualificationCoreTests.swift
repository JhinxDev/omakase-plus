import Foundation
import XCTest
@testable import QualificationCore

final class QualificationCoreTests: XCTestCase {
    private let fingerprint = String(repeating: "a", count: 64)

    private func config(url: String = "https://example.invalid/sample.mp4", id: String = "sample_01",
                        fingerprint: String? = nil, hdr: String = "hdr10", version: Int = 1) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schemaVersion": version,
            "samples": [["id": id, "title": "Synthetic sample", "url": url,
                         "fingerprint": fingerprint ?? self.fingerprint,
                         "expectedSubtitles": ["English embedded"], "expectedHDR": hdr]]
        ])
    }

    private func sample(id: String = "sample_01", fingerprint: String? = nil) throws -> QualificationSample {
        try QualificationConfig.decode(config(id: id, fingerprint: fingerprint)).samples[0]
    }

    func testValidSyntheticConfigKeepsExplicitExpectations() throws {
        let value = try QualificationConfig.decode(config())
        XCTAssertEqual(value.samples.count, 1)
        XCTAssertEqual(value.samples[0].id, "sample_01")
        XCTAssertEqual(value.samples[0].fingerprint, fingerprint)
        XCTAssertEqual(value.samples[0].expectedHDR, .hdr10)
        XCTAssertEqual(value.samples[0].expectedSubtitles, ["English embedded"])
    }

    func testUnsafeURLsAreRejectedWithoutLeakingInput() throws {
        for url in ["http://example.invalid/a", "file:///tmp/a", "https://user:secret@example.invalid/a",
                    "https://user@example.invalid/a", "https://example.invalid/a#secret", "https:///a",
                    "https://example.invalid/a#", " https://example.invalid/a", "https://example.invalid/a\n"] {
            XCTAssertThrowsError(try QualificationConfig.decode(config(url: url))) { error in
                XCTAssertEqual(error as? ConfigurationError, .invalidURL)
                XCTAssertEqual(String(describing: error), "invalidURL")
            }
        }
    }

    func testSignedQueryAllowedOnlyAsOperatorInput() throws {
        let sample = try QualificationConfig.decode(config(url: "https://example.invalid/a?signature=LOCAL_ONLY")).samples[0]
        XCTAssertEqual(sample.sourceURL.query, "signature=LOCAL_ONLY")
    }

    func testFingerprintAndIDAreStrict() throws {
        for value in ["", "abc", String(repeating: "A", count: 64), String(repeating: "g", count: 64)] {
            XCTAssertThrowsError(try QualificationConfig.decode(config(fingerprint: value)))
        }
        for id in ["", "sample/one", "sample|one", String(repeating: "x", count: 65)] {
            XCTAssertThrowsError(try QualificationConfig.decode(config(id: id)))
        }
    }

    func testUnknownHDRAndVersionFailClosed() throws {
        XCTAssertThrowsError(try QualificationConfig.decode(config(hdr: "automatic")))
        XCTAssertThrowsError(try QualificationConfig.decode(config(version: 2)))
    }

    func testMalformedAndDuplicateConfigExposeOnlyCodes() throws {
        XCTAssertThrowsError(try QualificationConfig.decode(Data("{private".utf8))) { error in
            XCTAssertEqual(error as? ConfigurationError, .malformed)
        }
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: config()) as? [String: Any])
        let rows = try XCTUnwrap(json["samples"] as? [[String: Any]])
        json["samples"] = rows + rows
        XCTAssertThrowsError(try QualificationConfig.decode(JSONSerialization.data(withJSONObject: json))) { error in
            XCTAssertEqual(error as? ConfigurationError, .duplicateID)
        }
    }

    func testResumeIsIsolatedByIDAndExactFingerprint() throws {
        let original = try sample()
        let changedMedia = try sample(fingerprint: String(repeating: "b", count: 64))
        let otherID = try sample(id: "sample_02")
        var ledger = ResumeLedger()
        ledger.checkpoint(sample: original, position: 42, duration: 100, reachedEnd: false, now: 1000)
        XCTAssertEqual(ledger.record(for: original, now: 1001)?.position, 42)
        XCTAssertNil(ledger.record(for: changedMedia, now: 1001))
        XCTAssertNil(ledger.record(for: otherID, now: 1001))
    }

    func testResumeRejectsCorruptStaleFutureAndNonfiniteValues() throws {
        let value = try sample()
        var ledger = ResumeLedger()
        for position in [-1.0, .nan, .infinity, 101] {
            ledger.checkpoint(sample: value, position: position, duration: 100, reachedEnd: false, now: 1000)
            XCTAssertNil(ledger.record(for: value, now: 1001))
        }
        ledger.checkpoint(sample: value, position: 40, duration: .infinity, reachedEnd: false, now: 1000)
        XCTAssertNil(ledger.record(for: value, now: 1001))
        ledger.checkpoint(sample: value, position: 40, duration: 100, reachedEnd: false, now: 1000)
        XCTAssertNil(ledger.record(for: value, now: 999))
        XCTAssertNil(ledger.record(for: value, now: 1000 + ResumeLedger.maxAge + 1))
        XCTAssertEqual(ResumeLedger.decode(Data("broken".utf8)).count, 0)
    }

    func testResumeRoundTripNeverContainsURLOrMarksWatched() throws {
        let value = try QualificationConfig.decode(config(url: "https://example.invalid/a?signature=LOCAL_ONLY")).samples[0]
        var ledger = ResumeLedger()
        ledger.checkpoint(sample: value, position: 99, duration: 100, reachedEnd: false, now: 1000)
        XCTAssertEqual(ledger.record(for: value, now: 1001)?.reachedEnd, false)
        let data = try ledger.encoded()
        let text = String(decoding: data, as: UTF8.self)
        for forbidden in ["https", "signature", "LOCAL_ONLY", "watched", "example.invalid"] {
            XCTAssertFalse(text.contains(forbidden))
        }
        XCTAssertEqual(ResumeLedger.decode(data).record(for: value, now: 1001)?.position, 99)
        ledger.checkpoint(sample: value, position: 100, duration: 100, reachedEnd: true, now: 1002)
        XCTAssertEqual(ledger.record(for: value, now: 1003)?.reachedEnd, true)
    }

    func testCorruptPersistedRecordDoesNotBecomeResume() throws {
        let value = try sample()
        let data = Data("{\"records\":[{\"id\":\"sample_01\",\"fingerprint\":\"\(fingerprint)\",\"position\":500,\"duration\":100,\"reachedEnd\":false,\"updatedAt\":1000}]}".utf8)
        XCTAssertNil(ResumeLedger.decode(data).record(for: value, now: 1001))
    }

    func testBundledBaselineIsSeparateFromOperatorURLConfig() throws {
        let local = URL(fileURLWithPath: "/app/Resources/baseline.mp4")
        let value = try XCTUnwrap(QualificationSample.bundledBaseline(resourceURL: local, fingerprint: fingerprint))
        XCTAssertEqual(value.id, "synthetic-baseline")
        XCTAssertTrue(value.sourceURL.isFileURL)
        XCTAssertEqual(value.expectedHDR, .sdr)
        XCTAssertTrue(value.expectedSubtitles.isEmpty)
        XCTAssertNil(QualificationSample.bundledBaseline(resourceURL: URL(string: "https://example.invalid/baseline.mp4")!, fingerprint: fingerprint))
        XCTAssertNil(QualificationSample.bundledBaseline(resourceURL: URL(fileURLWithPath: "/other.mp4"), fingerprint: fingerprint))
        XCTAssertNil(QualificationSample.bundledBaseline(resourceURL: local, fingerprint: "bad"))
        XCTAssertThrowsError(try QualificationConfig.decode(config(url: local.absoluteString))) { error in
            XCTAssertEqual(error as? ConfigurationError, .invalidURL)
        }
    }
}
