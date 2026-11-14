import XCTest
import QualificationCore

/// Redaction and payload-shape tests for the AVPlayer diagnostic reporting path.
///
/// Everything here uses invented hosts (`*.example.test`) and invented secrets
/// (`SYNTHETIC_TOKEN_*`). No real provider URL, token or credential is used.
final class PlaybackDiagnosticTests: XCTestCase {
    private let providerURL =
        "https://media.example.test/live/SYNTHETIC_TOKEN_1/master.m3u8"
        + "?token=SYNTHETIC_TOKEN_2&expires=1700000000"

    // MARK: - Redaction cases

    /// A token in the path must not survive just because the query was dropped.
    /// This is the case that failed before the fix.
    func testProviderPathTokenAndQueryAreRemoved() {
        let redacted = PlaybackDiagnostic.redactedText("open failed: \(providerURL)")
        XCTAssertEqual(redacted, "open failed: <url>",
                       "path or user-info survived redaction: \(redacted)")
        XCTAssertFalse(redacted.contains("media.example.test"))
        XCTAssertFalse(redacted.contains("SYNTHETIC_TOKEN"))
        XCTAssertFalse(redacted.contains("/live/"))
    }

    func testUserInfoAndPasswordAreRemoved() {
        let url = "https://viewer:SYNTHETIC_TOKEN_3@media.example.test/private/playlist"
        let redacted = PlaybackDiagnostic.redactedText(url)
        XCTAssertEqual(redacted, "<url>", "user-info or path survived redaction: \(redacted)")
        XCTAssertFalse(redacted.contains("viewer"))
        XCTAssertFalse(redacted.contains("SYNTHETIC_TOKEN_3"))
        XCTAssertFalse(redacted.contains("/private/playlist"))
    }

    func testTwoURLsInOneStringAreBothRemoved() {
        let text = "try \(providerURL) then "
            + "http://backup.example.test/b/SYNTHETIC_TOKEN_4"
        let redacted = PlaybackDiagnostic.redactedText(text)
        XCTAssertEqual(redacted, "try <url> then <url>",
                       "not every URL was removed: \(redacted)")
        XCTAssertFalse(redacted.contains("example.test"))
        XCTAssertFalse(redacted.contains("SYNTHETIC_TOKEN"))
    }

    func testProviderShapedErrorStringDropsURL() {
        let text = "Error Domain=AVFoundationErrorDomain Code=-11850 \"Cannot Open\""
            + " UserInfo={NSURLErrorFailingURLStringErrorKey=\(providerURL)}"
        let redacted = PlaybackDiagnostic.redactedText(text)
        XCTAssertFalse(redacted.contains("media.example.test"), "host survived: \(redacted)")
        XCTAssertFalse(redacted.contains("SYNTHETIC_TOKEN"), "token survived: \(redacted)")
        XCTAssertFalse(redacted.contains("/live/"), "path survived: \(redacted)")
        XCTAssertTrue(redacted.contains("AVFoundationErrorDomain"),
                      "stable domain label was lost: \(redacted)")
    }

    func testNonURLStringPassesThroughUnchanged() {
        let values = [
            "AVFoundationErrorDomain",
            "AVPlayerWaitingToMinimizeStallsReason",
            "The operation could not be completed.",
            "network:-1009",
        ]
        for value in values {
            XCTAssertEqual(PlaybackDiagnostic.redactedText(value), value,
                           "non-URL text was rewritten: \(value)")
        }
    }

    // MARK: - Structured payload

    func testStructuredPayloadForSyntheticProviderURLHasNoURLLikeContent() throws {
        let syntheticErrorComment = "failed to open \(providerURL)"
        let input = AVPlayerDiagnosticInput(
            itemStatus: "failed",
            errorDomain: "AVFoundationErrorDomain",
            errorCode: -11800,
            timeControlStatus: "waiting",
            waitingReason: "AVPlayerWaitingToMinimizeStallsReason",
            currentTime: 42.5,
            rate: 0,
            loadedRanges: [.init(start: 40, duration: 5)],
            seekableRanges: [.init(start: 0, duration: 100)],
            accessEvents: [.init(bytesTransferred: 12_345, mediaRequests: 7, uri: providerURL)],
            errorEvents: [.init(statusCode: -1009, domain: "NSURLErrorDomain",
                                comment: syntheticErrorComment, uri: providerURL)]
        )
        let mediaID = String(repeating: "a", count: 32)
        let payload = PlaybackDiagnostic.avplayer(input, trigger: .error, mediaID: mediaID)
        let data = try payload.encoded()
        let json = try XCTUnwrap(String(data: data, encoding: .utf8))

        for marker in ["media.example.test", "SYNTHETIC_TOKEN", "http", "://", "?token",
                       "NSURLErrorDomain", "failed to open"] {
            XCTAssertFalse(json.localizedCaseInsensitiveContains(marker),
                           "payload leaked \(marker): \(json)")
        }

        let decoded = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(decoded["schema"] as? String, "playback_diagnostic_v1")
        XCTAssertEqual(decoded["engine"] as? String, "avplayer")
        XCTAssertEqual(decoded["trigger"] as? String, "error")
        XCTAssertEqual(decoded["media_id"] as? String, mediaID)
        XCTAssertEqual(decoded["item_status"] as? String, "failed")
        XCTAssertEqual(decoded["time_control"] as? String, "waiting")
        XCTAssertEqual(decoded["current_time"] as? Double, 42.5)
        XCTAssertEqual(decoded["rate"] as? Double, 0)
        XCTAssertEqual(decoded["waiting_reason"] as? String,
                       "AVPlayerWaitingToMinimizeStallsReason")

        let itemError = try XCTUnwrap(decoded["item_error"] as? [String: Any])
        XCTAssertEqual(itemError["domain"] as? String, "AVFoundationErrorDomain")
        XCTAssertEqual(itemError["code"] as? Int, -11800)

        let loaded = try XCTUnwrap(decoded["loaded_ranges"] as? [[String: Any]])
        XCTAssertEqual(loaded.first?["start"] as? Double, 40)
        XCTAssertEqual(loaded.first?["duration"] as? Double, 5)

        let events = try XCTUnwrap(decoded["events"] as? [[String: Any]])
        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events[0]["bytes_transferred"] as? Int64, 12_345)
        XCTAssertEqual(events[0]["media_requests"] as? Int, 7)
        XCTAssertNil(events[0]["status_code"])
        XCTAssertEqual(events[1]["status_code"] as? Int, -1009)

        // Structural proof: the payload type has nowhere to put a URI or comment.
        let eventKeys = Set(events.flatMap { $0.keys })
        XCTAssertEqual(eventKeys, Set(["bytes_transferred", "media_requests", "status_code"]))

        // The redactor is applied to the text fields the payload keeps.
        XCTAssertEqual(payload.waitingReason,
                       "AVPlayerWaitingToMinimizeStallsReason")
        XCTAssertEqual(payload.itemError?.domain, "AVFoundationErrorDomain")
    }

    func testMediaIdentifierRejectsNonHexAndUnrelatedText() {
        let payload = PlaybackDiagnostic(engine: .avplayer, trigger: .state,
                                         mediaID: "\(providerURL)",
                                         itemStatus: "failed")
        XCTAssertNil(payload.mediaID, "a URL must never survive as a media id")
        XCTAssertNil(payload.attaching(mediaID: "\(providerURL)").mediaID,
                     "attaching must not accept a URL")
        XCTAssertNil(payload.attaching(mediaID: "A" + String(repeating: "a", count: 31)).mediaID,
                     "uppercase hex is not the server's id shape")
        XCTAssertEqual(payload.attaching(mediaID: String(repeating: "b", count: 32)).mediaID,
                       String(repeating: "b", count: 32))
    }
}
