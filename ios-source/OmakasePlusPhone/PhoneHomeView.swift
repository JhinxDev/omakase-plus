import QualificationCore
import SwiftUI

struct PhoneHomeArtwork: Decodable, Hashable {
    let landscape: String?
    let poster: String?
}

struct PhoneHomeCard: Decodable, Identifiable {
    struct Progress: Decodable {
        let percent: Int?
        let remainingLabel: String

        enum CodingKeys: String, CodingKey {
            case percent
            case remainingLabel = "remaining_label"
        }
    }

    let id: String
    let domain: LiveLibraryKind
    let title: String
    let titleID: String
    let subtitle: String
    let detailLabel: String
    let progress: Progress
    let playURL: String?
    let playLabel: String
    let artwork: PhoneHomeArtwork

    enum CodingKeys: String, CodingKey {
        case id, domain, title, subtitle, progress, artwork
        case titleID = "title_id"
        case detailLabel = "detail_label"
        case playURL = "play_url"
        case playLabel = "play_label"
    }

    var entry: LiveLibraryEntry {
        let components = playURL.flatMap { URLComponents(string: $0) }
        let path = components.flatMap { target -> String? in
            guard target.scheme == nil, target.host == nil, target.fragment == nil,
                  target.path.range(
                      of: #"^/plus/watch/(?:[1-9][0-9]{0,17}/[0-9]{1,4}|title/movie/tt[0-9]{7,10}|title/tv/[1-9][0-9]{0,9}/[0-9]{1,3}/[1-9][0-9]{0,3})$"#,
                      options: .regularExpression
                  ) != nil else { return nil }
            return target.path
        }
        let artworkID = artwork.landscape.flatMap { value in
            value.hasPrefix("artwork/") ? value.split(separator: "/").dropFirst().first.map(String.init) : nil
        }
        return LiveLibraryEntry(
            homeTitleID: titleID,
            title: title,
            note: subtitle,
            watchPath: path,
            artworkID: artworkID,
            posterURL: artwork.poster.flatMap(URL.init(string:)),
            backdropURL: artwork.landscape.flatMap(URL.init(string:)),
            progress: progress.percent.map { Double($0) / 100 }
        )
    }
}

struct PhoneHomeRow: Decodable, Identifiable {
    let id: String
    let label: String
    let cards: [PhoneHomeCard]
}

struct PhoneHomeProjection: Decodable {
    struct Banner: Decodable {
        let title: String
        let mediaDetail: String
        let artwork: PhoneHomeArtwork

        enum CodingKeys: String, CodingKey {
            case title, artwork
            case mediaDetail = "media_detail"
        }
    }

    let banner: Banner
    let rows: [PhoneHomeRow]
}

struct PhoneHomeView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient
    var onOpenSearch: () -> Void = {}

    @State private var projection: PhoneHomeProjection?
    @State private var loading = false
    @State private var loadError: String?
    @State private var selectedEntry: PhoneDetailSelection?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if let projection {
                        heroSection(projection)
                        let visibleRows = projection.rows.filter { !$0.cards.isEmpty }
                        if visibleRows.isEmpty {
                            emptyHomeCard
                        } else {
                            ForEach(visibleRows) { row in
                                shelfSection(row)
                            }
                        }
                    } else if loading {
                        ProgressView("Loading Home…")
                            .tint(Color.appAccent)
                            .frame(maxWidth: .infinity, minHeight: 280)
                    } else if let loadError {
                        VStack(spacing: 14) {
                            Text(loadError)
                                .foregroundStyle(Color.appWarning)
                            Button("Retry") {
                                Task { await loadHome() }
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(Color.appAccent)
                        }
                        .frame(maxWidth: .infinity, minHeight: 260)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.bottom, 28)
            }
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Home")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable {
                await loadHome()
            }
            .task(id: store.selectedLibrary?.id) {
                await loadHome()
            }
            .navigationDestination(item: $selectedEntry) { item in
                PhoneTitleDetailView(store: store, client: client, kind: item.kind, entry: item.entry)
            }
        }
    }

    private var heroCard: PhoneHomeCard? {
        projection?.rows.lazy.compactMap { $0.cards.first(where: { $0.entry.watchPath != nil }) }.first
    }

    private func heroSection(_ projection: PhoneHomeProjection) -> some View {
        let featured = heroCard
        let artPath = featured?.artwork.landscape ?? projection.banner.artwork.landscape
        return ZStack(alignment: .bottomLeading) {
            PhoneArtworkView(
                cacheKey: "home:\(artPath ?? projection.banner.title)",
                source: artPath.map { .home(client, $0) }
            )
            .frame(height: 260)
            .frame(maxWidth: .infinity)
            .overlay(
                LinearGradient(
                    colors: [Color.clear, Color.appBackground.opacity(0.65), Color.appBackground],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )

            VStack(alignment: .leading, spacing: 8) {
                Text(featured?.title ?? projection.banner.title)
                    .font(.title2.bold())
                    .lineLimit(2)
                    .accessibilityIdentifier("phone.home.heroTitle")
                let subtitle = featured.map { card in
                    let rawSubtitle = card.subtitle.trimmingCharacters(in: .whitespacesAndNewlines)
                    let remaining = card.progress.remainingLabel.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !remaining.isEmpty && !rawSubtitle.localizedCaseInsensitiveContains(remaining) {
                        return [rawSubtitle, remaining].filter { !$0.isEmpty }.joined(separator: " · ")
                    }
                    return rawSubtitle
                } ?? projection.banner.mediaDetail
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Color.appMuted)
                        .lineLimit(1)
                        .accessibilityIdentifier("phone.home.heroSubtitle")
                }
                if let featured, featured.entry.watchPath != nil {
                    Button {
                        Task { await store.play(featured.entry) }
                    } label: {
                        Label(featured.playLabel, systemImage: "play.fill")
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.appAccent)
                    .foregroundStyle(Color.appBackground)
                    .disabled(store.openingPlayback)
                }
            }
            .padding(16)
        }
        .frame(maxWidth: .infinity, alignment: .bottomLeading)
        .clipped()
    }

    private var emptyHomeCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Nothing in progress yet")
                .font(.headline)
            Text("Explore Anime, Movies, or TV Shows in your Library or Discover to start watching.")
                .font(.subheadline)
                .foregroundStyle(Color.appMuted)
            Button("Search & Discover", action: onOpenSearch)
                .buttonStyle(.borderedProminent)
                .tint(Color.appAccent)
                .foregroundStyle(Color.appBackground)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .padding(.horizontal, 16)
    }

    private func shelfSection(_ row: PhoneHomeRow) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(row.label)
                .font(.title3.bold())
                .padding(.horizontal, 16)
                .accessibilityIdentifier("phone.home.shelf.\(row.id)")
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 14) {
                    ForEach(row.cards) { card in
                        let entry = card.entry
                        let artPath = card.artwork.landscape ?? card.artwork.poster
                        Button {
                            if row.id.hasPrefix("continue-"), entry.watchPath != nil {
                                Task { await store.play(entry) }
                            } else {
                                selectedEntry = PhoneDetailSelection(kind: card.domain, entry: entry)
                            }
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                ZStack(alignment: .bottom) {
                                    PhoneArtworkView(
                                        cacheKey: "home-card:\(card.id):\(artPath ?? "")",
                                        source: artPath.map { .home(client, $0) }
                                    )
                                    .frame(width: 210, height: 118)
                                    .clipShape(RoundedRectangle(cornerRadius: 12))

                                    if let pct = card.progress.percent, pct > 0 {
                                        ProgressView(value: Double(min(pct, 100)), total: 100)
                                            .tint(Color.appAccent)
                                            .padding(.horizontal, 8)
                                            .padding(.bottom, 6)
                                    }
                                }
                                Text(card.title)
                                    .font(.subheadline.weight(.semibold))
                                    .lineLimit(1)
                                    .foregroundStyle(Color.appText)
                                let caption = [card.subtitle, card.detailLabel].filter { !$0.isEmpty }.joined(separator: " · ")
                                if !caption.isEmpty {
                                    Text(caption)
                                        .font(.caption)
                                        .foregroundStyle(Color.appMuted)
                                        .lineLimit(1)
                                }
                            }
                            .frame(width: 210, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
            }
        }
    }

    private func loadHome() async {
        loading = true
        loadError = nil
        defer { loading = false }
        do {
            projection = try await client.homeProjection(as: PhoneHomeProjection.self)
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch is CancellationError {
            return
        } catch {
            loadError = "Could not load Home."
        }
    }
}

struct PhoneDetailSelection: Hashable {
    let kind: LiveLibraryKind
    let entry: LiveLibraryEntry

    func hash(into hasher: inout Hasher) {
        hasher.combine(kind.rawValue)
        hasher.combine(entry.id)
    }

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.kind == rhs.kind && lhs.entry.id == rhs.entry.id
    }
}
