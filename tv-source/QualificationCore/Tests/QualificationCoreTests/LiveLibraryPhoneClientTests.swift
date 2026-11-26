import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import QualificationCore

private let stubToken = String(repeating: "t", count: 43)

/// The iPhone-facing pieces of the shared client: pairing as an iPhone, renewing a
/// device credential, saving a Home layout and fetching request posters.
final class LiveLibraryPhoneClientTests: XCTestCase {
    private var token: String { stubToken }
    private let base = "/plus/api/native-tv-test/v1/library/"

    @MainActor
    func testPairingAsAnIPhoneSendsItsKindAndAppleTVStaysTheDefault() async throws {
        let session = StubProtocol.session(host: "pair") { _ in
            .init(status: 200, body: #"{"device_token":"\#(stubToken)","user_code":"XMKT-YJPR","expires_in":300}"#)
        }
        _ = try await LiveLibraryClient.startPairing(server: "https://pair.example.invalid",
                                                     kind: "iphone", session: session)
        _ = try await LiveLibraryClient.startPairing(server: "https://pair.example.invalid", session: session)
        let seen = StubProtocol.requests(host: "pair")
        XCTAssertEqual(seen.map(\.method), ["POST", "POST"])
        XCTAssertEqual(seen[0].url.query, "kind=iphone")
        XCTAssertNil(seen[1].url.query)
        do {
            _ = try await LiveLibraryClient.startPairing(server: "https://pair.example.invalid",
                                                         kind: "x&y=1", session: session)
            XCTFail("A hostile device kind must not reach the URL")
        } catch FixtureConnectionError.invalidOrigin {}
    }

    @MainActor
    func testRenewalCarriesTheServerExpiryAndKeepsTheSelectedLibrary() async throws {
        let session = StubProtocol.session(host: "renew") { _ in .init(status: 200, body: #"{"expires_in":2000000}"#) }
        let option = try JSONDecoder().decode(
            LiveLibraryOption.self,
            from: Data(#"{"id":"\#(String(repeating: "a", count: 32))","name":"Home","kind":"household"}"#.utf8)
        )
        let old = try client(host: "renew", session: session, lifetime: -60).selecting(option)
        let renewed = try await old.renewed()
        XCTAssertEqual(renewed.credential.token, token)
        XCTAssertEqual(renewed.selectedLibrary, option)
        XCTAssertEqual(renewed.expiresAt.timeIntervalSinceNow, 2_000_000, accuracy: 30)
        let seen = try XCTUnwrap(StubProtocol.requests(host: "renew").first)
        XCTAssertEqual(seen.method, "GET")
        XCTAssertEqual(seen.authorization, "Bearer " + token)
        XCTAssertEqual(seen.url.path, base + "device")
    }

    @MainActor
    func testRenewalFailuresAreDistinguishable() async throws {
        for (host, status, expected) in [("r401", 401, "unauthorized"), ("r202", 202, "rejected")] {
            let session = StubProtocol.session(host: host) { _ in .init(status: status, body: "{}") }
            do {
                _ = try await client(host: host, session: session, lifetime: 100).renewed()
                XCTFail("Renewal should fail for \(status)")
            } catch FixtureConnectionError.unauthorized {
                XCTAssertEqual(expected, "unauthorized")
            } catch FixtureConnectionError.requestRejected {
                XCTAssertEqual(expected, "rejected")
            }
        }
        let silly = StubProtocol.session(host: "rbad") { _ in .init(status: 200, body: #"{"expires_in":99999999}"#) }
        do {
            _ = try await client(host: "rbad", session: silly, lifetime: 100).renewed()
            XCTFail("An implausible expiry must be refused")
        } catch FixtureConnectionError.invalidResponse {}
    }

    @MainActor
    func testSavingAHomeLayoutPostsOrderedRows() async throws {
        let session = StubProtocol.session(host: "layout") { _ in .init(status: 200, body: #"{"layout":[]}"#) }
        try await client(host: "layout", session: session, lifetime: 1000)
            .saveHomeLayout(rows: [("continue-tv", false), ("continue-anime", true)])
        let seen = try XCTUnwrap(StubProtocol.requests(host: "layout").first)
        XCTAssertEqual(seen.method, "POST")
        XCTAssertEqual(seen.url.path, base + "home/layout")
        XCTAssertEqual(seen.authorization, "Bearer " + token)
        let body = try JSONSerialization.jsonObject(with: try XCTUnwrap(seen.body)) as? [String: [[String: Any]]]
        XCTAssertEqual(body?["rows"]?.map { $0["id"] as? String }, ["continue-tv", "continue-anime"])
        XCTAssertEqual(body?["rows"]?.map { $0["hidden"] as? Bool }, [false, true])
    }

    func testRequestsDecodeWithAndWithoutAPoster() throws {
        func decode(_ extra: String) throws -> LiveTitleRequest {
            let json = #"{"kind":"movie","title_id":"tt1","title":"T","state":"active","label":"L","detail":""\#(extra)}"#
            return try JSONDecoder().decode(LiveTitleRequest.self, from: Data(json.utf8))
        }
        XCTAssertNil(try decode("").poster)
        XCTAssertNil(try decode(#","poster":null"#).poster)
        XCTAssertEqual(try decode(#","poster":"artwork/3/poster""#).poster, "artwork/3/poster")
    }

    @MainActor
    func testRequestPostersAreFetchedOnlyFromTrustedTargets() async throws {
        let session = StubProtocol.session(host: "art") { _ in .init(status: 200, body: "IMG") }
        let live = try client(host: "art", session: session, lifetime: 1000)
        func request(_ poster: String?) throws -> LiveTitleRequest {
            let extra = poster.map { #","poster":"\#($0)""# } ?? ""
            let json = #"{"kind":"anime","title_id":"1","title":"T","state":"active","label":"L","detail":""\#(extra)}"#
            return try JSONDecoder().decode(LiveTitleRequest.self, from: Data(json.utf8))
        }
        let trusted = try await live.requestArtwork(request("artwork/12/poster"))
        XCTAssertEqual(trusted, Data("IMG".utf8))
        for hostile in [nil, "https://evil.example/p.jpg", "artwork/0/poster", "http://image.tmdb.org/t/p/w342/a.jpg",
                        "artwork/12/poster/../x"] {
            let result = try await live.requestArtwork(request(hostile))
            XCTAssertNil(result, "\(hostile ?? "nil") must not be fetched")
        }
        XCTAssertEqual(StubProtocol.requests(host: "art").map(\.url.path), [base + "artwork/12/poster"])
    }

    private func client(host: String, session: URLSession, lifetime: TimeInterval) throws -> LiveLibraryClient {
        LiveLibraryClient(origin: try FixtureOrigin("https://\(host).example.invalid"), token: token,
                          session: session, expiresAt: Date().addingTimeInterval(lifetime))
    }
}

private final class StubProtocol: URLProtocol {
    struct Reply { let status: Int; let body: String }
    struct Seen { let method: String; let url: URL; let authorization: String?; let body: Data? }

    private static let lock = NSLock()
    private nonisolated(unsafe) static var handlers: [String: @Sendable (URLRequest) -> Reply] = [:]
    private nonisolated(unsafe) static var histories: [String: [Seen]] = [:]

    static func session(host: String, handler: @escaping @Sendable (URLRequest) -> Reply) -> URLSession {
        lock.withLock {
            handlers[host] = handler
            histories[host] = []
        }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: config)
    }

    static func requests(host: String) -> [Seen] { lock.withLock { histories[host, default: []] } }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let host = url.host?.split(separator: ".").first.map(String.init),
              let handler = Self.lock.withLock({ Self.handlers[host] }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        Self.lock.withLock {
            Self.histories[host, default: []].append(
                Seen(method: request.httpMethod ?? "GET", url: url,
                     authorization: request.value(forHTTPHeaderField: "Authorization"), body: Self.body(of: request))
            )
        }
        let reply = handler(request)
        let response = HTTPURLResponse(url: url, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(reply.body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func body(of request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
