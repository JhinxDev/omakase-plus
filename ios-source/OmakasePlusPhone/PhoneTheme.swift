import QualificationCore
import SwiftUI
import UIKit

extension Color {
    static let appBackground = Color(omakaseHex: 0x090B12)
    static let appSurface = Color(omakaseHex: 0x121620)
    static let appRaised = Color(omakaseHex: 0x1B2130)
    static let appKeyline = Color(omakaseHex: 0x282F40)
    static let appText = Color(omakaseHex: 0xF4F3F8)
    static let appMuted = Color(omakaseHex: 0xBDC4D1)
    static let appAccent = Color(omakaseHex: 0xC3B1FF)
    static let appWarning = Color(omakaseHex: 0xE9B565)

    private init(omakaseHex value: UInt32) {
        self.init(
            .sRGB,
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}

@MainActor
final class PhoneArtworkCache {
    static let shared = PhoneArtworkCache()

    private let cache: NSCache<NSString, UIImage> = {
        let store = NSCache<NSString, UIImage>()
        store.countLimit = 180
        store.totalCostLimit = 64 * 1024 * 1024
        return store
    }()

    func image(for key: String) -> UIImage? {
        cache.object(forKey: key as NSString)
    }

    func store(_ image: UIImage, for key: String) {
        let bytes = Int(image.size.width * image.size.height * image.scale * image.scale * 4)
        cache.setObject(image, forKey: key as NSString, cost: max(1, bytes))
    }

    func clear() {
        cache.removeAllObjects()
    }
}

enum PhoneArtworkSource: Sendable {
    case home(LiveLibraryClient, String)
    case entry(LiveLibraryClient, LiveLibraryEntry, Bool)
    case details(LiveLibraryClient, String)
    case recommendation(LiveLibraryClient, LiveRecommendation)

    func fetch() async throws -> Data? {
        switch self {
        case .home(let client, let path):
            return try await client.homeArtwork(path)
        case .entry(let client, let entry, let banner):
            return try await client.artwork(entry, banner: banner)
        case .details(let client, let path):
            return try await client.detailsArtwork(path: path)
        case .recommendation(let client, let card):
            return try await client.recommendationArtwork(card)
        }
    }
}

struct PhoneArtworkView: View {
    let cacheKey: String
    let source: PhoneArtworkSource?
    var fallbackIcon = "film"

    @State private var image: UIImage?

    var body: some View {
        Rectangle()
            .fill(Color.appRaised)
            .overlay {
                if let image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFill()
                } else {
                    Image(systemName: fallbackIcon)
                        .font(.title2)
                        .foregroundStyle(Color.appMuted.opacity(0.65))
                }
            }
            .clipped()
            .task(id: cacheKey) {
            if let cached = PhoneArtworkCache.shared.image(for: cacheKey) {
                image = cached
                return
            }
            image = nil
            guard let source else { return }
            guard let data = try? await source.fetch(), !Task.isCancelled,
                  let decoded = UIImage(data: data) else { return }
            PhoneArtworkCache.shared.store(decoded, for: cacheKey)
            image = decoded
        }
    }
}
