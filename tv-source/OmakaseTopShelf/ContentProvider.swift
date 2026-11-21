import Foundation
import TVServices

final class ContentProvider: TVTopShelfContentProvider {
    override func loadTopShelfContent(completionHandler: @escaping (TVTopShelfContent?) -> Void) {
        guard let store = TVTopShelfStore.shared(), let snapshot = store.snapshot() else {
            completionHandler(nil)
            return
        }

        let sections: [TVTopShelfItemCollection<TVTopShelfSectionedItem>] =
            TVTopShelfActionKind.allCases.compactMap { action in
                let items: [TVTopShelfSectionedItem] = snapshot.cards.filter { $0.action == action }.compactMap { card in
                    let artwork = store.artworkURL(for: card.id)
                    guard let url = card.link.url, FileManager.default.fileExists(atPath: artwork.path)
                    else { return nil }
                    let item = TVTopShelfSectionedItem(identifier: card.id.uuidString)
                    item.title = card.title
                    item.imageShape = action == .resume ? .square : .poster
                    item.setImageURL(artwork, for: [.screenScale1x, .screenScale2x])
                    item.expirationDate = snapshot.expiresAt
                    let destination = TVTopShelfAction(url: url)
                    item.displayAction = destination
                    if action == .resume {
                        item.playAction = destination
                        if let progress = card.progress, (0...1).contains(progress) {
                            item.playbackProgress = progress
                        }
                    }
                    return item
                }
                guard !items.isEmpty else { return nil }
                let section = TVTopShelfItemCollection(items: items)
                section.title = action.sectionTitle
                return section
            }
        completionHandler(sections.isEmpty ? nil : TVTopShelfSectionedContent(sections: sections))
    }
}
