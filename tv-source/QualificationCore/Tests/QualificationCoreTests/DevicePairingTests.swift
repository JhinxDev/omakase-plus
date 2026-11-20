import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import QualificationCore

final class DevicePairingTests: XCTestCase {
    @MainActor
    func testPairingKeepsTheSameCodeAfterConnectionLossAndServiceUnavailable() async throws {
        let challenge = try await start(host: "recover")
        let client = try await challenge.waitForApproval(pollInterval: .milliseconds(1))
        XCTAssertEqual(challenge.code, "XMKT-YJPR")
        XCTAssertEqual(challenge.verificationURL.query, "code=XMKT-YJPR")
        XCTAssertEqual(client.credential.token, PairingFixtureProtocol.token)
        XCTAssertEqual(PairingFixtureProtocol.requests(for: "recover"), ["POST", "GET", "GET", "GET", "GET"])
    }

    @MainActor
    func testServerRejectionStopsPolling() async throws {
        let challenge = try await start(host: "expired")
        do {
            _ = try await challenge.waitForApproval(pollInterval: .milliseconds(1))
            XCTFail("An expired pairing cannot produce a linked client")
        } catch FixtureConnectionError.unauthorized {}
        XCTAssertEqual(PairingFixtureProtocol.requests(for: "expired"), ["POST", "GET"])
    }

    @MainActor
    func testCertificateErrorStopsPolling() async throws {
        let challenge = try await start(host: "untrusted")
        do {
            _ = try await challenge.waitForApproval(pollInterval: .milliseconds(1))
            XCTFail("Pairing should report a certificate error")
        } catch let error as URLError {
            XCTAssertEqual(error.code, .serverCertificateUntrusted)
        }
        XCTAssertEqual(PairingFixtureProtocol.requests(for: "untrusted"), ["POST", "GET"])
    }

    @MainActor
    private func start(host: String) async throws -> LiveLibraryClient.DeviceChallenge {
        PairingFixtureProtocol.reset(host: host)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PairingFixtureProtocol.self]
        return try await LiveLibraryClient.startPairing(server: "https://\(host).example.invalid",
                                                        session: URLSession(configuration: config))
    }
}

private final class PairingFixtureProtocol: URLProtocol {
    static let token = String(repeating: "q", count: 43)
    private static let lock = NSLock()
    private nonisolated(unsafe) static var histories: [String: [String]] = [:]

    static func reset(host: String) {
        lock.withLock { histories[host] = [] }
    }

    static func requests(for host: String) -> [String] {
        lock.withLock { histories[host, default: []] }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            let url = try XCTUnwrap(request.url)
            let host = try XCTUnwrap(url.host?.split(separator: ".").first.map(String.init))
            let method = try XCTUnwrap(request.httpMethod)
            let index = Self.lock.withLock {
                Self.histories[host, default: []].append(method)
                return Self.histories[host, default: []].count
            }
            XCTAssertEqual(url.path, "/plus/api/native-tv-test/v1/library/device")
            if index == 1 {
                XCTAssertEqual(method, "POST")
                XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
                try reply(url, status: 200,
                          body: #"{"device_token":"\#(Self.token)","user_code":"XMKT-YJPR","expires_in":300}"#)
                return
            }
            XCTAssertEqual(method, "GET")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer " + Self.token)
            switch (host, index) {
            case ("recover", 2): try reply(url, status: 202)
            case ("recover", 3): throw URLError(.networkConnectionLost)
            case ("recover", 4): try reply(url, status: 503)
            case ("recover", 5): try reply(url, status: 200, body: #"{"expires_in":1200}"#)
            case ("untrusted", 2): throw URLError(.serverCertificateUntrusted)
            default: try reply(url, status: 401)
            }
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    private func reply(_ url: URL, status: Int, body: String = "{}") throws {
        let response = try XCTUnwrap(HTTPURLResponse(url: url, statusCode: status,
                                                     httpVersion: "HTTP/1.1", headerFields: nil))
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
