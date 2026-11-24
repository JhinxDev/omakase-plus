import CryptoKit
import Foundation
import ImageIO
import QualificationCore
import TVServices
import UIKit

struct TVTopShelfDestination: Identifiable, Sendable {
    let id: UUID
    let kind: LiveLibraryKind
    let action: TVTopShelfActionKind
    let entry: LiveLibraryEntry
}

@MainActor
enum TVTopShelfLibrary {
    struct Request: Identifiable {
        let id = UUID()
        let card: TVTopShelfCard
        let scope: String
    }

    private struct ProfileIdentity: Encodable {
        let profileID: LiveSavedProfile.ID
        let credentialExpiry: Date
    }

    private struct Candidate: Sendable {
        let entry: LiveLibraryEntry
        let kind: LiveLibraryKind
        let action: TVTopShelfActionKind

        var path: String? { entry.featuredPath ?? entry.watchPath }
    }

    private static var refreshGeneration = 0
    private static let maximumAge: TimeInterval = 6 * 60 * 60
    private static let resumeLimit = 6

    static func request(for link: TVTopShelfLink) -> Request? {
        guard let snapshot = TVTopShelfStore.shared()?.snapshot(), let card = snapshot.card(for: link)
        else { return nil }
        return Request(card: card, scope: snapshot.scope)
    }

    static func invalidate(unlessMatching profile: LiveSavedProfile? = nil) {
        guard let store = TVTopShelfStore.shared() else { return }
        if let profile, let current = scope(for: profile), store.snapshot()?.scope == current { return }
        refreshGeneration += 1
        store.clear()
        TVTopShelfContentProvider.topShelfContentDidChange()
    }

    static func resolve(_ request: Request, client: LiveLibraryClient,
                        profile: LiveSavedProfile) async throws -> TVTopShelfDestination? {
        guard client.selectedLibrary == nil, scope(for: profile) == request.scope,
              profile.expiresAt > Date(), let kind = LiveLibraryKind(rawValue: request.card.kind),
              kind != .cloud else { return nil }
        let entries: [Candidate]
        switch request.card.action {
        case .resume:
            entries = try await resumeCandidates(client: client).filter { $0.kind == kind }
        case .details:
            entries = try await savedCandidates(client: client)
        }
        let match = entries.first { target(for: $0) == request.card.match }
        return match.map { TVTopShelfDestination(id: request.id, kind: kind,
                                                 action: request.card.action, entry: $0.entry) }
    }

    static func refresh(client: LiveLibraryClient, profile: LiveSavedProfile,
                        fetchArtwork: Bool = true,
                        stillSelected: () -> Bool) async {
        guard let store = TVTopShelfStore.shared(), let scope = scope(for: profile),
              client.selectedLibrary == nil, profile.expiresAt > Date() else { return }
        refreshGeneration += 1
        let generation = refreshGeneration
        do {
            async let resumes = resumeCandidates(client: client)
            async let saved = savedCandidates(client: client)
            let (resumeRows, savedRows) = try await (resumes, saved)
            try Task.checkCancellation()
            guard refreshGeneration == generation, stillSelected() else { return }
            let candidates = Array(resumeRows.prefix(resumeLimit)) + savedRows
            let previous = store.snapshot()
            var tiles: [(TVTopShelfCard, Candidate)] = []
            var fallbackImages: [UUID: Data] = [:]
            for candidate in candidates {
                guard let match = target(for: candidate) else { continue }
                let title = String(candidate.entry.title.prefix(120)).trimmingCharacters(in: .whitespacesAndNewlines)
                guard !title.isEmpty else { continue }
                let old = previous?.scope == scope ? previous?.cards.first {
                    $0.action == candidate.action && $0.match == match
                } : nil
                let id = old?.id ?? UUID()
                let card = TVTopShelfCard(id: id, action: candidate.action, kind: candidate.kind.rawValue,
                                          match: match, title: title, progress: candidate.entry.progress)
                if !FileManager.default.fileExists(atPath: store.artworkURL(for: card).path) {
                    fallbackImages[id] = renderedArtwork(nil, card: card)
                }
                tiles.append((card, candidate))
            }
            let snapshot = TVTopShelfSnapshot(scope: scope,
                expiresAt: min(Date().addingTimeInterval(maximumAge), profile.expiresAt), cards: tiles.map(\.0))
            try store.publish(snapshot, artwork: fallbackImages)
            TVTopShelfContentProvider.topShelfContentDidChange()
            guard fetchArtwork else { return }

            var images: [UUID: Data] = [:]
            // Keep at most four artwork requests in flight. The local shelf is already usable.
            for start in stride(from: 0, to: tiles.count, by: 4) {
                try Task.checkCancellation()
                let end = min(start + 4, tiles.count)
                let fetched = await withTaskGroup(of: (UUID, Data?).self) { tasks in
                    for (card, candidate) in tiles[start..<end] {
                        let isResume = card.action == .resume
                        let title = card.title
                        tasks.addTask {
                            let raw: Data?
                            if isResume,
                               let banner = try? await client.artwork(candidate.entry, banner: true) {
                                raw = banner
                            } else {
                                raw = try? await client.artwork(candidate.entry, banner: false)
                            }
                            guard !Task.isCancelled else { return (card.id, nil) }
                            return (card.id, renderedArtwork(raw, isResume: isResume, title: title))
                        }
                    }
                    var results: [UUID: Data] = [:]
                    for await (id, data) in tasks { results[id] = data }
                    return results
                }
                for (card, _) in tiles[start..<end] {
                    if let data = fetched[card.id] { images[card.id] = data }
                }
            }
            try Task.checkCancellation()
            guard refreshGeneration == generation, stillSelected(), !images.isEmpty else { return }
            try store.publish(snapshot, artwork: images)
            TVTopShelfContentProvider.topShelfContentDidChange()
        } catch FixtureConnectionError.unauthorized {
            if !Task.isCancelled && refreshGeneration == generation && stillSelected() { invalidate() }
        } catch {
            // An unavailable cache or catalog should never block the library UI.
        }
    }

    private static func resumeCandidates(client: LiveLibraryClient) async throws -> [Candidate] {
        let home = try await client.homeProjection(as: HomeProjection.self)
        let continueRows: Set<String> = ["continue-anime", "continue-movies", "continue-tv"]
        let cards = home.rows.filter { continueRows.contains($0.id) }.flatMap(\.cards)
        let order = TVTopShelfRecency()
        return cards.compactMap { card -> (String, Date, Candidate)? in
            let entry = card.entry
            guard entry.watchPath != nil, card.domain != .cloud else { return nil }
            return (card.id, order.date(card.lastWatchAt), Candidate(entry: entry, kind: card.domain, action: .resume))
        }
        .sorted { ($0.1, $0.0) > ($1.1, $1.0) }
        .map { $0.2 }
    }

    private static func savedCandidates(client: LiveLibraryClient) async throws -> [Candidate] {
        do {
            return try await client.watchlist().prefix(4).map { Candidate(entry: $0, kind: .anime, action: .details) }
        } catch FixtureConnectionError.unauthorized {
            throw FixtureConnectionError.unauthorized
        } catch {
            return []
        }
    }

    private static func target(for candidate: Candidate) -> String? {
        if candidate.action == .resume && candidate.path == nil { return nil }
        return fingerprint([candidate.kind.rawValue, candidate.entry.id, candidate.path ?? ""])
    }

    private static func scope(for profile: LiveSavedProfile) -> String? {
        fingerprint(ProfileIdentity(profileID: profile.id, credentialExpiry: profile.expiresAt))
    }

    private static func fingerprint<T: Encodable>(_ value: T) -> String? {
        // This secret lives in the host app's private preferences. The extension
        // receives opaque matches, without a server address, account ID or path.
        let keyName = "omakase.tv.top-shelf.local-key"
        let key: SymmetricKey
        if let saved = UserDefaults.standard.data(forKey: keyName), saved.count == 32 {
            key = SymmetricKey(data: saved)
        } else {
            key = SymmetricKey(size: .bits256)
            UserDefaults.standard.set(key.withUnsafeBytes { Data($0) }, forKey: keyName)
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys
        guard let data = try? encoder.encode(value) else { return nil }
        return Data(HMAC<SHA256>.authenticationCode(for: data, using: key)).base64EncodedString()
    }

    nonisolated private static func renderedArtwork(_ data: Data?, isResume: Bool, title: String) -> Data? {
        let size = isResume ? CGSize(width: 960, height: 540) : CGSize(width: 404, height: 606)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let image = data.flatMap { data -> UIImage? in
            guard let source = CGImageSourceCreateWithData(data as CFData, nil),
                  let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                    kCGImageSourceThumbnailMaxPixelSize: 1000,
                  ] as CFDictionary) else { return nil }
            return UIImage(cgImage: thumbnail)
        }
        // Keep a missing or failed artwork fetch usable with a legible Omakase tile.
        let renderer = UIGraphicsImageRenderer(size: size, format: format)
        return renderer.jpegData(withCompressionQuality: 0.82) { context in
            let rect = CGRect(origin: .zero, size: size)
            UIColor(red: 0.071, green: 0.086, blue: 0.125, alpha: 1).setFill()
            context.fill(rect)
            if let image, image.size.width > 0, image.size.height > 0 {
                let widthScale = size.width / image.size.width
                let heightScale = size.height / image.size.height
                let scale = isResume ? min(widthScale, heightScale) : max(widthScale, heightScale)
                let width = image.size.width * scale
                let height = image.size.height * scale
                image.draw(in: CGRect(x: (size.width - width) / 2, y: (size.height - height) / 2,
                                      width: width, height: height))
                return
            }
            let inset: CGFloat = 32
            let accent = UIColor(red: 0.765, green: 0.694, blue: 1, alpha: 1)
            let line = CGRect(x: inset, y: size.height * 0.59, width: 48, height: 5)
            accent.setFill()
            context.fill(line)
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineBreakMode = .byTruncatingTail
            let text = NSAttributedString(string: title, attributes: [
                .font: UIFont.systemFont(ofSize: 36, weight: .semibold),
                .foregroundColor: UIColor.white,
                .paragraphStyle: paragraph,
            ])
            text.draw(with: CGRect(x: inset, y: line.maxY + 16,
                                   width: size.width - inset * 2, height: size.height - line.maxY - inset),
                      options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine], context: nil)
        }
    }
}
