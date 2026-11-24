import Foundation
import ImageIO
import UIKit

@MainActor
final class TVArtworkCache {
    static let shared = TVArtworkCache()

    private let cache: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>()
        cache.countLimit = 80
        cache.totalCostLimit = 40 * 1024 * 1024
        return cache
    }()

    func cached(for key: String) -> UIImage? {
        cache.object(forKey: key as NSString)
    }

    func decodeAndStore(_ data: Data, key: String, maxPixelSize: Int) async -> UIImage? {
        if let existing = cache.object(forKey: key as NSString) {
            return existing
        }
        let cgImage: CGImage? = await Task.detached(priority: .utility) {
            Self.downsample(data: data, maxPixelSize: maxPixelSize)
        }.value
        let image: UIImage?
        if let cgImage {
            image = UIImage(cgImage: cgImage)
        } else {
            image = UIImage(data: data)
        }
        guard let image else { return nil }
        let cost = Int(image.size.width * image.scale * image.size.height * image.scale * 4)
        cache.setObject(image, forKey: key as NSString, cost: max(cost, 1))
        return image
    }

    func clear() {
        cache.removeAllObjects()
    }

    nonisolated private static func downsample(data: Data, maxPixelSize: Int) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(
            data as CFData,
            [kCGImageSourceShouldCache: false] as CFDictionary
        ) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: max(64, maxPixelSize),
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }
}
