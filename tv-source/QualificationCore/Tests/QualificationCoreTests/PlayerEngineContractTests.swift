import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import QualificationCore

/// Contract details shared by both engines, plus the `direct` media format the
/// server uses for Real-Debrid originals. No network and no Apple framework.
final class PlayerEngineContractTests: XCTestCase {
    func testRangesAreNormalizedSortAndMerge() {
        let ranges = TransportRanges(buffered: [
            MediaRange(start: 20, end: 30),
            MediaRange(start: 0, end: 10),
            MediaRange(start: 10, end: 20),
            MediaRange(start: 5, end: 6),
        ])
        XCTAssertEqual(ranges.buffered, [MediaRange(start: 0, end: 30)])
    }

    func testInvalidRangesAreDropped() {
        let ranges = TransportRanges(seekable: [
            MediaRange(start: .nan, end: 10),
            MediaRange(start: 0, end: .infinity),
            MediaRange(start: 5, end: 5),
            MediaRange(start: -1, end: 4),
            MediaRange(start: 1, end: 4),
        ])
        XCTAssertEqual(ranges.seekable, [MediaRange(start: 1, end: 4)])
        XCTAssertTrue(ranges.isSeekable)
    }

    func testSeekabilityLookupAndSummaryCarryNoURL() {
        let ranges = TransportRanges(buffered: [MediaRange(start: 0, end: 12)],
                                     seekable: [MediaRange(start: 0, end: 600)])
        XCTAssertTrue(ranges.isSeekable(300))
        XCTAssertFalse(ranges.isSeekable(601))
        XCTAssertFalse(ranges.isSeekable(.nan))
        XCTAssertFalse(ranges.summary.contains("http"))
        XCTAssertFalse(TransportRanges().isSeekable)
        XCTAssertEqual(TransportRanges().summary, "buffered=none seekable=none")
    }

    func testFailureCodesAreSanitizedAndTruncated() {
        let failure = EngineFailure(kind: .network, code: "error\r\nforged\u{0}line")
        XCTAssertEqual(failure.code, "errorforgedline")
        let long = EngineFailure(kind: .unknown, code: String(repeating: "x", count: 200))
        XCTAssertEqual(long.code.count, 64)
    }

    func testFailureCodeCannotCarryASignedURL() {
        let failure = EngineFailure(kind: .authorization,
                                    code: "https://debrid.example.invalid/a.mkv?token=secret")
        XCTAssertFalse(failure.code.contains("\n"))
        XCTAssertEqual(failure.kind, .authorization)
    }

    // MARK: - direct media format

    func testMP4StillUsesThePassScopedCookiePath() throws {
        let pass = try makePass()
        XCTAssertEqual(pass.format, .mp4)
        XCTAssertFalse(pass.isHLS)
        XCTAssertFalse(pass.isDirect)
        XCTAssertTrue(pass.mediaHeaders.isEmpty)
        XCTAssertTrue(pass.mediaURL.path.hasPrefix("/plus/api/native-tv-test/v1/media/"))
        XCTAssertEqual(pass.mediaCookies.first?.path, pass.mediaURL.path)
    }

    func testHLSStillResolvesToAPlaylist() throws {
        let pass = try makePass {
            $0["media_format"] = "hls"
            $0["media_playlist"] = "vod"
        }
        XCTAssertTrue(pass.isHLS)
        XCTAssertFalse(pass.isGrowingHLS)
        XCTAssertFalse(pass.isDirect)
        XCTAssertEqual(pass.mediaURL.lastPathComponent, "index.m3u8")
    }

    func testDirectUsesRangeURLAndHeaders() throws {
        let pass = try makePass {
            $0["media_format"] = "direct"
            $0["media_range_url"] = "https://debrid.example.invalid/original.mkv?sig=secret"
            $0["media_headers"] = ["Authorization": "Bearer media-pass"]
        }
        XCTAssertEqual(pass.format, .direct)
        XCTAssertTrue(pass.isDirect)
        XCTAssertFalse(pass.isHLS)
        XCTAssertFalse(pass.isGrowingHLS)
        XCTAssertEqual(pass.mediaURL.host, "debrid.example.invalid")
        XCTAssertEqual(pass.mediaHeaders, ["authorization": "Bearer media-pass"])
    }

    func testServerDirectPathsRejectForeignHostsAndIncorrectGrantEvidence() throws {
        let id = String(repeating: "d", count: 32)
        let token = String(repeating: "h", count: 43)
        let canonical = "/plus/api/native-tv-test/v1/direct/\(id)/\(token)"
        for path in ["https://foreign.example.invalid" + canonical, "//foreign.example.invalid" + canonical,
                     canonical + "?x=1", canonical + "#fragment", canonical + "/..",
                     canonical + "i", canonical.replacingOccurrences(of: "/direct/", with: "/media/")] {
            XCTAssertThrowsError(try makePass {
                $0["media_format"] = "direct"
                $0["media_url"] = path
            })
        }
        XCTAssertThrowsError(try makePass {
            $0["media_format"] = "direct"
            $0["media_url"] = canonical
            $0["media_range_url"] = "https://external.example.invalid/test.mkv"
        })
    }

    func testDirectRequiresHTTPS() throws {
        for scheme in ["http", "file", "ftp"] {
            XCTAssertThrowsError(try makePass {
                $0["media_format"] = "direct"
                $0["media_range_url"] = "\(scheme)://debrid.example.invalid/original.mkv"
            }, "\(scheme) must be refused")
        }
        XCTAssertThrowsError(try makePass { $0["media_format"] = "direct" })
    }

    func testDirectRejectsCredentialsFragmentAndPlaylist() throws {
        XCTAssertThrowsError(try makePass {
            $0["media_format"] = "direct"
            $0["media_range_url"] = "https://user:pass@debrid.example.invalid/a.mkv"
        })
        XCTAssertThrowsError(try makePass {
            $0["media_format"] = "direct"
            $0["media_range_url"] = "https://debrid.example.invalid/a.mkv#frag"
        })
        XCTAssertThrowsError(try makePass {
            $0["media_format"] = "direct"
            $0["media_range_url"] = "https://debrid.example.invalid/index.m3u8"
            $0["media_playlist"] = "event"
        })
    }

    func testDirectRejectsHeaderInjectionAndReservedNames() throws {
        for headers: [String: String] in [
            ["Authorization": "Bearer ok\r\nX-Forged: 1"],
            ["Range": "bytes=0-1"],
            ["Cookie": "omakase_tv_test_media=abc"],
            ["Host": "elsewhere.invalid"],
            ["X-Bad Name": "value"],
            ["Authorization": ""],
        ] {
            XCTAssertThrowsError(try makePass {
                $0["media_format"] = "direct"
                $0["media_range_url"] = "https://debrid.example.invalid/a.mkv"
                $0["media_headers"] = headers
            })
        }
        XCTAssertThrowsError(try makePass {
            $0["media_format"] = "direct"
            $0["media_range_url"] = "https://debrid.example.invalid/a.mkv"
            $0["media_headers"] = ["X-A": "1", "X-B": "2", "X-C": "3", "X-D": "4",
                                   "X-E": "5", "X-F": "6", "X-G": "7", "X-H": "8", "X-I": "9"]
        })
    }

    func testNonDirectFormatsRejectRangeMaterial() throws {
        XCTAssertThrowsError(try makePass {
            $0["media_range_url"] = "https://debrid.example.invalid/a.mkv"
        })
        XCTAssertThrowsError(try makePass {
            $0["media_format"] = "hls"
            $0["media_playlist"] = "vod"
            $0["media_headers"] = ["Authorization": "Bearer x"]
        })
    }

    func testUnknownFormatIsRefused() throws {
        XCTAssertThrowsError(try makePass { $0["media_format"] = "mkv" })
    }

    func testDirectWithoutAccessCheckStillCarriesNoResumeSyncClaims() throws {
        let pass = try makePass {
            $0["media_format"] = "direct"
            $0["media_range_url"] = "https://debrid.example.invalid/a.mkv"
        }
        XCTAssertFalse(pass.supportsResumeSync)
        XCTAssertFalse(pass.supportsAccessCheck)
    }

    private func makePass(_ mutate: (inout [String: Any]) -> Void = { _ in }) throws -> LiveTestPass {
        let origin = try FixtureOrigin("https://library.example.invalid")
        var payload: [String: Any] = [
            "identity": [
                "library_id": String(repeating: "a", count: 32),
                "domain": "tv",
                "canonical_id": "42",
                "season_number": 1,
                "episode_number": 2,
                "media_version": String(repeating: "b", count: 64),
            ],
            "title": "Harbor Signal",
            "duration_seconds": 1_800,
            "media_id": String(repeating: "d", count: 32),
            "media_token": String(repeating: "h", count: 43),
            "position_seconds": 104,
            "expires_in": 120,
        ]
        mutate(&payload)
        let data = try JSONSerialization.data(withJSONObject: payload)
        let reply = try JSONDecoder().decode(LiveTestReply.self, from: data)
        return try LiveTestPass(reply: reply, origin: origin)
    }
}
