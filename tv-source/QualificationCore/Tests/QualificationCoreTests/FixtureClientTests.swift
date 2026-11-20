import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import QualificationCore

final class FixtureClientTests: XCTestCase {
    func testOriginAcceptsHTTPSAndLimitsHTTPToExplicitLoopback() throws {
        XCTAssertNoThrow(try FixtureOrigin("https://fixture.example.invalid:8443"))
        XCTAssertThrowsError(try FixtureOrigin("http://localhost:8765"))
        let loopback = try FixtureOrigin("http://localhost:8765", allowLoopbackHTTP: true)
        XCTAssertEqual(loopback.endpoint("play/example/close").absoluteString, "http://localhost:8765/v1/play/example/close")
        for text in ["http://192.0.2.1", "http://localhost.example.invalid", "https://example.invalid/other",
                     "https://user@example.invalid", "https://example.invalid/?secret=x",
                     "https://example.invalid/#fragment", "https://example.invalid:0"] {
            XCTAssertThrowsError(try FixtureOrigin(text, allowLoopbackHTTP: true), text)
        }
    }

    func testAdjacentEpisodeResolvesAnIndependentPlayableCopy() throws {
        let tracks = URL(fileURLWithPath: "/app/track-check.mp4")
        let fixture = try XCTUnwrap(LibraryFixture(baselineURL: URL(fileURLWithPath: "/app/baseline.mp4"),
                                                  tracksURL: tracks))
        for card in fixture.cards where card.kind != .movie {
            let next = try XCTUnwrap(fixture.adjacentEpisode(to: card, step: 1))
            XCTAssertEqual(fixture.adjacentEpisode(to: next, step: -1)?.id, card.id)
            XCTAssertNil(fixture.adjacentEpisode(to: next, step: 1))
            XCTAssertNotEqual(fixture.media(for: card.id)?.resumeIdentity, fixture.media(for: next.id)?.resumeIdentity)
            if card.kind == .tv {
                XCTAssertEqual(fixture.media(for: next.id)?.url, tracks)
                XCTAssertEqual(next.id.mediaVersion, LibraryFixture.tracksVersion)
            }
        }
        let movie = try XCTUnwrap(fixture.cards.first { $0.kind == .movie })
        XCTAssertNil(fixture.adjacentEpisode(to: movie, step: 1))
    }

    func testLiveTestInvitationAndMediaCookieStayOnOneHTTPSOrigin() throws {
        let token = String(repeating: "s", count: 43)
        let mediaID = String(repeating: "a", count: 32)
        let server = "https://fixture.example.invalid"
        XCTAssertNoThrow(try LiveTestClient(invitation: server + "#00112233445566778899"))
        XCTAssertThrowsError(try LiveTestClient(invitation: "http://localhost#00112233445566778899"))
        XCTAssertThrowsError(try LiveTestClient(invitation: server + "/other#00112233445566778899"))
        let media: [String: Any] = [
            "identity": ["library_id": LibraryFixture.libraryID, "domain": "movie",
                         "canonical_id": "tt999999991", "season_number": 0, "episode_number": 0,
                         "media_version": LibraryFixture.mediaVersion],
            "title": "The Violet Hour", "media_id": mediaID, "media_token": token,
            "duration_seconds": 7200, "position_seconds": 3605.125, "expires_in": 7800,
        ]
        let data = try JSONSerialization.data(withJSONObject: media)
        let reply = try JSONDecoder().decode(LiveTestReply.self, from: data)
        let pass = try LiveTestPass(reply: reply, origin: FixtureOrigin(server))
        XCTAssertEqual(pass.position, 3605.125)
        XCTAssertEqual(pass.duration, 7200)
        XCTAssertGreaterThan(pass.expiresAt.timeIntervalSinceNow, 7200)
        XCTAssertNil(pass.nextWatchPath)
        XCTAssertEqual(pass.mediaURL.absoluteString, server + "/plus/api/native-tv-test/v1/media/" + mediaID)
        XCTAssertFalse(pass.mediaURL.absoluteString.contains(token))
        let cookie = try XCTUnwrap(pass.mediaCookies.first)
        XCTAssertEqual(cookie.domain, "fixture.example.invalid")
        XCTAssertEqual(cookie.path, pass.mediaURL.path)
        XCTAssertTrue(cookie.isSecure)

        for (playlist, growing) in [(nil as String?, true), ("event", true), ("vod", false)] {
            var hls = media
            hls["media_format"] = "hls"
            hls["media_playlist"] = playlist
            let data = try JSONSerialization.data(withJSONObject: hls)
            let reply = try JSONDecoder().decode(LiveTestReply.self, from: data)
            let pass = try LiveTestPass(reply: reply, origin: FixtureOrigin(server))
            XCTAssertEqual(pass.isGrowingHLS, growing)
            XCTAssertTrue(pass.isHLS)
            XCTAssertTrue(pass.mediaURL.path.hasSuffix("/index.m3u8"))
        }
    }

    func testHouseholdResumeIsolatedByViewerOnTheSameCopy() throws {
        let origin = try FixtureOrigin("https://fixture.example.invalid")
        let reply: [String: Any] = [
            "identity": ["library_id": LibraryFixture.libraryID, "domain": "movie",
                         "canonical_id": "tt999999991", "season_number": 0, "episode_number": 0,
                         "media_version": LibraryFixture.mediaVersion],
            "title": "Shared film", "media_id": String(repeating: "a", count: 32),
            "media_token": String(repeating: "s", count: 43),
            "duration_seconds": 1800, "position_seconds": 0, "expires_in": 1200,
        ]
        func pass(_ scope: String?) throws -> LiveTestPass {
            var payload = reply
            payload["resume_scope"] = scope
            let data = try JSONSerialization.data(withJSONObject: payload)
            let result = try JSONDecoder().decode(LiveTestReply.self, from: data)
            return try LiveTestPass(reply: result, origin: origin)
        }
        let owner = try pass(String(repeating: "1", count: 64)).card.identity.resumeIdentity
        let member = try pass(String(repeating: "2", count: 64)).card.identity.resumeIdentity
        let personal = try pass(nil).card.identity.resumeIdentity
        var ledger = ResumeLedger()
        ledger.checkpoint(identity: owner, position: 32, duration: 1800, reachedEnd: false, now: 100)
        ledger = ResumeLedger.decode(try ledger.encoded())
        XCTAssertEqual(ledger.record(for: owner, now: 101)?.position, 32)
        XCTAssertNil(ledger.record(for: member, now: 101))
        XCTAssertNil(ledger.record(for: personal, now: 101))
        XCTAssertThrowsError(try pass("bad scope"))
    }

    func testLiveNextPathRequiresTheExactSuccessor() throws {
        let origin = try FixtureOrigin("https://fixture.example.invalid")
        for (domain, canonicalID, episode, valid, invalid) in [
            ("tv", "42", 2, "/plus/watch/title/tv/42/1/3", "/plus/watch/title/tv/42/2/3"),
            ("anime", "9", 2, "/plus/watch/8/3", "/plus/watch/8/4"),
        ] {
            for (path, expected) in [(valid, valid), (invalid, nil), (valid + "\n", nil)] {
                let data = try JSONSerialization.data(withJSONObject: [
                    "identity": ["library_id": LibraryFixture.libraryID, "domain": domain,
                                 "canonical_id": canonicalID, "season_number": 1, "episode_number": episode,
                                 "media_version": LibraryFixture.mediaVersion],
                    "title": "Fixture", "media_id": String(repeating: "a", count: 32),
                    "media_token": String(repeating: "s", count: 43), "next_watch_path": path,
                    "duration_seconds": 1800, "position_seconds": 0, "expires_in": 1200,
                ])
                let reply = try JSONDecoder().decode(LiveTestReply.self, from: data)
                XCTAssertEqual(try LiveTestPass(reply: reply, origin: origin).nextWatchPath, expected)
            }
        }
        let current = LibraryTarget.tv(tmdbID: 42, season: 1, episode: 2)
        XCTAssertTrue(current.isImmediatelyBefore(.tv(tmdbID: 42, season: 1, episode: 3)))
        XCTAssertFalse(current.isImmediatelyBefore(.tv(tmdbID: 43, season: 1, episode: 3)))
        XCTAssertFalse(current.isImmediatelyBefore(.tv(tmdbID: 42, season: 2, episode: 3)))
        XCTAssertFalse(current.isImmediatelyBefore(.tv(tmdbID: 42, season: 1, episode: 4)))
    }

    @MainActor
    func testLiveResumeWriterRetriesOneCheckpointAndStopsOnSupersession() async {
        var sent: [String] = []
        let writer = LiveResumeWriter(onAccessLost: { _ in
            XCTFail("A newer resume position does not end playback access")
        }) { seq, position in
            sent.append("\(seq):\(position)")
            if sent.count == 1 { throw URLError(.networkConnectionLost) }
            if seq == 2 { throw LiveLibraryError.resumeSuperseded }
        }
        writer.offer(10)
        writer.offer(12)
        await writer.flush()
        XCTAssertEqual(sent, ["1:12.0", "1:12.0"])
        writer.offer(12)
        writer.offer(18)
        await writer.flush()
        writer.offer(22)
        await writer.flush(retryUnsaved: true)
        XCTAssertEqual(sent, ["1:12.0", "1:12.0", "2:18.0"])
        XCTAssertEqual(writer.localPosition, 22)
    }

    @MainActor
    func testLiveResumeWriterRecoversNewestOfflineCheckpointUsingItsOriginalSequence() async {
        var connected = true
        var sent: [String] = []
        let writer = LiveResumeWriter { sequence, position in
            sent.append("\(sequence):\(position)")
            if !connected { throw URLError(.notConnectedToInternet) }
        }
        writer.offer(5)
        await writer.flush()
        connected = false
        writer.offer(12)
        await writer.flush()
        writer.offer(16)
        await writer.flush()
        XCTAssertEqual(sent, ["1:5.0", "2:12.0", "2:12.0", "3:16.0", "3:16.0"])

        connected = true
        await writer.flush(retryUnsaved: true)
        await writer.flush(retryUnsaved: true)
        XCTAssertEqual(sent, ["1:5.0", "2:12.0", "2:12.0", "3:16.0", "3:16.0", "3:16.0"])
        writer.offer(21)
        await writer.flush()
        XCTAssertEqual(sent.last, "4:21.0")
    }

    @MainActor
    func testRecoveredCheckpointDefersToProgressFromAnotherDevice() async {
        var connected = false
        var sent: [Int] = []
        let writer = LiveResumeWriter { sequence, _ in
            sent.append(sequence)
            if !connected { throw URLError(.notConnectedToInternet) }
            throw LiveLibraryError.resumeSuperseded
        }
        writer.offer(22)
        await writer.flush()
        connected = true
        await writer.flush(retryUnsaved: true)
        writer.offer(24)
        await writer.flush(retryUnsaved: true)
        XCTAssertEqual(sent, [1, 1, 1])
        XCTAssertEqual(writer.localPosition, 24)
    }

    @MainActor
    func testLiveResumeWriterReportsAccessLossOnceAndStopsFurtherWrites() async {
        let cases: [(any Error, LivePlaybackAccessLoss)] = [
            (FixtureConnectionError.unauthorized, .profile),
            (LiveLibraryError.copyUnavailable, .media),
        ]
        for (denial, expected) in cases {
            var writes = 0
            var reported: [LivePlaybackAccessLoss] = []
            let writer = LiveResumeWriter(onAccessLost: { reported.append($0) }) { _, _ in
                writes += 1
                throw denial
            }
            writer.offer(10)
            await writer.flush()
            writer.offer(15)
            await writer.flush()
            XCTAssertEqual(writes, 1)
            XCTAssertEqual(reported, [expected])
        }
    }

    func testDeviceUnlinkRequiresAnExplicitServerAcknowledgment() throws {
        XCTAssertNoThrow(try LiveLibraryClient.confirmUnlink(Data(#"{"unlinked":true}"#.utf8)))
        for body in [#"{"unlinked":false}"#, #"{"status":"ok"}"#, "<html>Sign in</html>"] {
            XCTAssertThrowsError(try LiveLibraryClient.confirmUnlink(Data(body.utf8))) {
                XCTAssertEqual($0 as? FixtureConnectionError, .invalidResponse)
            }
        }
    }

    @MainActor
    func testLiveFixtureRoundTrip() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let endpoint = env["OMAKASE_FIXTURE_ORIGIN"], let code = env["OMAKASE_FIXTURE_CODE"] else {
            throw XCTSkip("Needs an isolated fixture server and a fresh synthetic pairing code.")
        }
        let client = FixtureClient(origin: try FixtureOrigin(endpoint, allowLoopbackHTTP: true))
        let device = try await client.pair(code: code)
        let cards = try await client.catalog(device: device)
        let card = try XCTUnwrap(cards.first)
        let initial = try await client.start(card, resume: false, device: device)
        var request = URLRequest(url: initial.url)
        request.setValue("bytes=0-127", forHTTPHeaderField: "Range")
        let (bytes, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 206)
        XCTAssertEqual(bytes.count, 128)
        try await client.close(initial, position: 17.125, device: device)
        let resumed = try await client.start(card, resume: true, device: device)
        XCTAssertEqual(resumed.position, 17.125)
        try await client.close(resumed, position: nil, device: device)
        try await client.logout(device: device)
        do {
            _ = try await client.catalog(device: device)
            XCTFail("Revoked credential accepted")
        } catch FixtureConnectionError.unauthorized {
            // Expected.
        }
    }
}
