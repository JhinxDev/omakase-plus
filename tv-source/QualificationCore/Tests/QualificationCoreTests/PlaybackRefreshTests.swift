import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import QualificationCore

final class PlaybackRefreshTests: XCTestCase {
    @MainActor
    func testRetryGetsFreshGrantForTheSameCopyAndUsesSavedPosition() async throws {
        let (client, original) = try playback(host: "same")
        XCTAssertFalse(original.supportsAccessCheck)
        let replacement = try await client.refreshPlayback(original)

        XCTAssertNotEqual(replacement.mediaID, original.mediaID)
        XCTAssertEqual(replacement.card.identity, original.card.identity)
        XCTAssertEqual(replacement.position, 111)
        XCTAssertTrue(replacement.hasServerResume)
        XCTAssertEqual(replacement.watchPath, original.watchPath)
        XCTAssertEqual(replacement.mediaCookies.first?.path, replacement.mediaURL.path)
        XCTAssertNotEqual(replacement.mediaCookies.first?.value, original.mediaCookies.first?.value)
    }

    @MainActor
    func testDirectRetryNegotiatesAndDecodesTheServerRangePass() async throws {
        let (client, original) = try playback(host: "direct", accessCheck: true)
        XCTAssertTrue(original.isDirect)

        let replacement = try await client.refreshPlayback(original)

        XCTAssertTrue(replacement.isDirect)
        XCTAssertTrue(replacement.supportsAccessCheck)
        XCTAssertEqual(replacement.mediaURL.scheme, "https")
        XCTAssertEqual(replacement.mediaURL.host, "direct.example.invalid")
        XCTAssertEqual(replacement.mediaURL.path.split(separator: "/").dropLast().last,
                       Substring(replacement.mediaID))
        XCTAssertTrue(replacement.mediaHeaders.isEmpty)
    }

    @MainActor
    func testChangedCopyNeedsExplicitReloadAndCannotInheritTheOldPosition() async throws {
        let (client, original) = try playback(host: "changed")
        do {
            _ = try await client.refreshPlayback(original)
            XCTFail("Retry accepted a different copy")
        } catch LiveLibraryError.copyChanged {
            // The viewer can explicitly reload the replacement after this error.
        }
        let replacement = try await client.refreshPlayback(original, acceptUpdatedCopy: true)
        XCTAssertEqual(replacement.card.identity.target, original.card.identity.target)
        XCTAssertNotEqual(replacement.card.identity, original.card.identity)
        XCTAssertEqual(replacement.position, 0)
    }

    @MainActor
    func testExplicitReloadCannotChangeTheHouseholdViewerOrEpisode() async throws {
        for host in ["viewer", "episode"] {
            let (client, original) = try playback(host: host)
            do {
                _ = try await client.refreshPlayback(original, acceptUpdatedCopy: true)
                XCTFail("Reload accepted another viewer or episode")
            } catch FixtureConnectionError.invalidResponse {
                // Reload only authorizes a new copy of the original episode.
            }
        }
    }

    @MainActor
    func testPlaybackAccessCheckWorksWithoutResumeSync() async throws {
        let outcomes: [(String, LivePlaybackAccessLoss?)] = [
            ("allowed", nil), ("unlinked", .profile), ("removed", .media),
        ]
        for (host, expected) in outcomes {
            let (client, pass) = try playback(host: host, accessCheck: true)
            XCTAssertTrue(pass.supportsAccessCheck)
            XCTAssertFalse(pass.supportsResumeSync)
            let result = try await client.playbackAccessLoss(for: pass)
            XCTAssertEqual(result, expected)
        }
    }

    @MainActor
    func testReclaimedPlanIsReportedSeparatelyFromRevocation() async throws {
        // A paused viewer keeps an authorized pass after the server reclaims the
        // bounded timeline. That is recoverable, and it is not a revocation.
        let (client, pass) = try playback(host: "expired", accessCheck: true)
        let loss = try await client.playbackAccessLoss(for: pass)
        XCTAssertEqual(loss, .planExpired)
    }

    @MainActor
    func testTemporaryAccessCheckFailureDoesNotReportRevocation() async throws {
        let (client, pass) = try playback(host: "busy", accessCheck: true)
        do {
            _ = try await client.playbackAccessLoss(for: pass)
            XCTFail("Expected a temporary request failure")
        } catch FixtureConnectionError.requestRejected {}
    }

    private func playback(host: String, accessCheck: Bool = false) throws -> (LiveLibraryClient, LiveTestPass) {
        let origin = try FixtureOrigin("https://\(host).example.invalid")
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RefreshFixtureProtocol.self]
        let option = LiveLibraryOption(id: RefreshFixtureProtocol.library, name: "Household", kind: .household)
        let client = LiveLibraryClient(origin: origin, token: RefreshFixtureProtocol.deviceToken,
                                       session: URLSession(configuration: config),
                                       expiresAt: Date().addingTimeInterval(300)).selecting(option)
        let payload = try RefreshFixtureProtocol.payload(original: true, host: host == "direct" ? host : "same", accessCheck: accessCheck)
        let reply = try JSONDecoder().decode(LiveTestReply.self, from: payload)
        return (client, try LiveTestPass(reply: reply, origin: origin, watchPath: RefreshFixtureProtocol.watchPath))
    }
}

private final class RefreshFixtureProtocol: URLProtocol {
    static let library = String(repeating: "b", count: 32)
    static let deviceToken = String(repeating: "q", count: 43)
    static let watchPath = "/plus/watch/title/tv/42/1/2"
    private static let api = "/plus/api/native-tv-test/v1/library"
    private static let originalID = String(repeating: "d", count: 32)
    private static let replacementID = String(repeating: "e", count: 32)

    static func payload(original: Bool, host: String = "same", accessCheck: Bool = false) throws -> Data {
        let version = host == "changed" ? "f" : "a"
        let position = host == "changed" ? 0 : 111
        var payload: [String: Any] = [
            "identity": ["library_id": library, "domain": "tv", "canonical_id": "42",
                         "season_number": 1, "episode_number": host == "episode" ? 3 : 2,
                         "media_version": String(repeating: version, count: 64)],
            "title": "Harbor Signal", "duration_seconds": 1_800,
            "media_id": original ? originalID : replacementID,
            "media_token": String(repeating: original ? "h" : "i", count: 43),
            "position_seconds": original ? 104 : position,
            "resume_scope": String(repeating: host == "viewer" ? "3" : "2", count: 64),
            "resume_sync": !accessCheck, "resume_updated_at": original ? 1 : 2, "expires_in": 120,
        ]
        if accessCheck || host == "direct" { payload["access_check"] = true }
        if host == "direct" {
            let id = original ? originalID : replacementID
            let token = String(repeating: original ? "h" : "i", count: 43)
            payload["media_format"] = "direct"
            payload["media_url"] = "/plus/api/native-tv-test/v1/direct/\(id)/\(token)"
            payload["direct"] = ["bytes": 12_345, "container": "mkv", "etag": "fixture"]
        }
        return try JSONSerialization.data(withJSONObject: payload)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            let url = try XCTUnwrap(request.url)
            let body: Data
            let status: Int
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer " + Self.deviceToken)
            switch url.path {
            case Self.api + "/play":
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertEqual(url.query, "library_id=" + Self.library)
                let raw = try requestBody()
                let selection = try JSONDecoder().decode([String: String].self, from: raw)
                let host = try XCTUnwrap(url.host?.split(separator: ".").first)
                // A refresh keeps the transport the item opened on: the native
                // timeline for the system player, the original file for direct.
                let expected = host == "direct"
                    ? ["watch_path": Self.watchPath, "engine": "direct"]
                    : ["watch_path": Self.watchPath, "engine": "native"]
                XCTAssertEqual(selection, expected)
                body = try Self.payload(original: false, host: String(host))
                status = 200
            case Self.api + "/media/\(Self.replacementID)/close":
                XCTAssertEqual(request.httpMethod, "POST")
                body = Data(#"{"closed":true}"#.utf8)
                status = 200
            case Self.api + "/media/\(Self.originalID)/access":
                XCTAssertEqual(request.httpMethod, "GET")
                XCTAssertNil(request.httpBody)
                let host = try XCTUnwrap(url.host?.split(separator: ".").first)
                status = switch host {
                case "allowed": 200
                case "unlinked": 401
                case "removed": 404
                case "expired": 200
                default: 503
                }
                body = switch host {
                case "expired": Data(#"{"allowed":true,"stream":"expired","recover":"reopen"}"#.utf8)
                case "allowed": Data(#"{"allowed":true,"stream":"live"}"#.utf8)
                default: Data()
                }
            default:
                throw FixtureConnectionError.invalidResponse
            }
            let response = try XCTUnwrap(HTTPURLResponse(url: url, statusCode: status,
                                                         httpVersion: "HTTP/1.1", headerFields: nil))
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    private func requestBody() throws -> Data {
        if let body = request.httpBody { return body }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open()
        defer { stream.close() }
        var body = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while true {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count == 0 { return body }
            if count < 0 { throw stream.streamError ?? FixtureConnectionError.invalidResponse }
            body.append(contentsOf: buffer.prefix(count))
        }
    }
}

