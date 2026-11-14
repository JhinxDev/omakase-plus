import Foundation

public enum HDRExpectation: String, Decodable, Sendable {
    case sdr, hdr10, hlg, unknown
    case dolbyVision = "dolby-vision"
}
/// Stable codes only: never attach a decoder error, URL or raw input.
public enum ConfigurationError: Error { case malformed, invalidURL, invalidSample, unsupportedVersion, duplicateID }
public struct QualificationSample: Sendable {
    public let id: String
    public let title: String
    public let sourceURL: URL
    public let fingerprint: String
    public let expectedSubtitles: [String]
    public let expectedHDR: HDRExpectation
    public var media: PlaybackMedia {
        // Both sample constructors validate these identity components.
        PlaybackMedia(url: sourceURL, resumeIdentity: ResumeIdentity(sample: self))
    }
    /// Only the app's fixed Bundle resource loader calls this. Operator JSON cannot select file URLs.
    public static func bundledBaseline(resourceURL: URL, fingerprint: String) -> Self? {
        guard resourceURL.isFileURL, resourceURL.lastPathComponent == "baseline.mp4",
              validFingerprint(fingerprint) else { return nil }
        return Self(id: "synthetic-baseline", title: "Silent synthetic controls test", sourceURL: resourceURL,
                    fingerprint: fingerprint, expectedSubtitles: [], expectedHDR: .sdr)
    }
}
public struct QualificationConfig: Sendable {
    public let samples: [QualificationSample]
    /// Operator-only qualification input. This is not a production authorization API.
    public static func decode(_ data: Data) throws -> Self {
        guard data.count <= 1_048_576 else { throw ConfigurationError.malformed }
        let raw: RawConfig
        do { raw = try JSONDecoder().decode(RawConfig.self, from: data) }
        catch { throw ConfigurationError.malformed }
        guard raw.schemaVersion == 1 else { throw ConfigurationError.unsupportedVersion }
        guard !raw.samples.isEmpty, raw.samples.count <= 100 else { throw ConfigurationError.invalidSample }
        var ids = Set<String>()
        let samples = try raw.samples.map { row -> QualificationSample in
            guard validID(row.id), validFingerprint(row.fingerprint), validLabel(row.title, limit: 80),
                  row.expectedSubtitles.count <= 16,
                  row.expectedSubtitles.allSatisfy({ validLabel($0, limit: 80) }),
                  let hdr = HDRExpectation(rawValue: row.expectedHDR) else { throw ConfigurationError.invalidSample }
            guard ids.insert(row.id).inserted else { throw ConfigurationError.duplicateID }
            guard row.url == row.url.trimmingCharacters(in: .whitespacesAndNewlines),
                  !row.url.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
                  let parts = URLComponents(string: row.url), parts.scheme?.lowercased() == "https",
                  let host = parts.host, !host.isEmpty,
                  parts.user == nil, parts.password == nil, parts.fragment == nil,
                  let url = parts.url else { throw ConfigurationError.invalidURL }
            return QualificationSample(id: row.id, title: row.title, sourceURL: url,
                                       fingerprint: row.fingerprint, expectedSubtitles: row.expectedSubtitles,
                                       expectedHDR: hdr)
        }
        return Self(samples: samples)
    }
}

private struct RawConfig: Decodable { let schemaVersion: Int; let samples: [RawSample] }
private struct RawSample: Decodable {
    let id: String
    let title: String
    let url: String
    let fingerprint: String
    let expectedSubtitles: [String]
    let expectedHDR: String
}
private func validID(_ value: String) -> Bool {
    !value.isEmpty && value.utf8.count <= 64 && value.utf8.allSatisfy {
        (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 95 || $0 == 45
    }
}
private func validFingerprint(_ value: String) -> Bool {
    value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}
private func validLabel(_ value: String, limit: Int) -> Bool {
    !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && value.count <= limit &&
    !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
}
public struct ResumeRecord: Codable, Sendable {
    public let id: String
    public let fingerprint: String
    public let libraryID: String?
    fileprivate let viewerScope: String?
    public let position: Double
    public let duration: Double
    public let reachedEnd: Bool
    public let updatedAt: Double
}
public struct ResumeIdentity: Hashable, Sendable {
    fileprivate let id: String
    fileprivate let fingerprint: String
    fileprivate let libraryID: String?
    fileprivate let viewerScope: String?

    init(sample: QualificationSample) {
        id = sample.id
        fingerprint = sample.fingerprint
        libraryID = nil
        viewerScope = nil
    }

    init?(id: String, fingerprint: String, libraryID: String, viewerScope: String? = nil) {
        guard validID(id), validFingerprint(fingerprint), Self.validLibrary(libraryID),
              viewerScope.map(validFingerprint) ?? true else { return nil }
        self.id = id
        self.fingerprint = fingerprint
        self.libraryID = libraryID
        self.viewerScope = viewerScope
    }

    fileprivate static func validLibrary(_ libraryID: String) -> Bool {
        libraryID.utf8.count == 32 &&
            libraryID.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    fileprivate func matches(_ record: ResumeRecord) -> Bool {
        record.id == id && record.fingerprint == fingerprint && record.libraryID == libraryID
            && record.viewerScope == viewerScope
    }
}

public struct ResumeLedger: Codable, Sendable {
    public static let maxAge: Double = 30 * 24 * 60 * 60
    private var records: [ResumeRecord] = []
    public init() {}
    public var count: Int { records.count }
    public mutating func checkpoint(sample: QualificationSample, position: Double, duration: Double,
                                    reachedEnd: Bool, now: Double) {
        checkpoint(identity: sample.media.resumeIdentity, position: position, duration: duration,
                   reachedEnd: reachedEnd, now: now)
    }
    public mutating func checkpoint(identity: ResumeIdentity, position: Double, duration: Double,
                                    reachedEnd: Bool, now: Double) {
        let value = ResumeRecord(id: identity.id, fingerprint: identity.fingerprint, libraryID: identity.libraryID,
                                 viewerScope: identity.viewerScope,
                                 position: position, duration: duration, reachedEnd: reachedEnd, updatedAt: now)
        guard Self.valid(value, now: now) else { return }
        records.removeAll { identity.matches($0) || !Self.valid($0, now: now) }
        records.append(value)
        if records.count > 100 { records.removeFirst(records.count - 100) }
    }
    public func record(for sample: QualificationSample, now: Double) -> ResumeRecord? {
        record(for: sample.media.resumeIdentity, now: now)
    }
    public func record(for identity: ResumeIdentity, now: Double) -> ResumeRecord? {
        records.last { identity.matches($0) && Self.valid($0, now: now) }
    }
    public func encoded() throws -> Data { try JSONEncoder().encode(self) }
    public static func decode(_ data: Data) -> Self {
        guard data.count <= 1_048_576, let value = try? JSONDecoder().decode(Self.self, from: data),
              value.records.count <= 100 else { return Self() }
        return value
    }
    private static func valid(_ value: ResumeRecord, now: Double) -> Bool {
        validID(value.id) && validFingerprint(value.fingerprint) &&
        (value.viewerScope.map(validFingerprint) ?? true) &&
        (value.libraryID.map(ResumeIdentity.validLibrary) ?? true) && now.isFinite &&
        value.position.isFinite && value.position >= 0 && value.duration.isFinite && value.duration > 0 &&
        value.position <= value.duration && value.updatedAt.isFinite && value.updatedAt >= 0 &&
        value.updatedAt <= now && now - value.updatedAt <= Self.maxAge
    }
}
