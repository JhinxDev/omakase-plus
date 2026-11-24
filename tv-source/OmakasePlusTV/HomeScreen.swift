import QualificationCore
import SwiftUI
import UIKit

struct HomeArtwork: Decodable, Hashable {
    let landscape: String?
    let poster: String?
}

struct HomeCard: Decodable, Identifiable {
    struct Progress: Decodable {
        let percent: Int?
        let remainingLabel: String

        enum CodingKeys: String, CodingKey {
            case percent, remainingLabel = "remaining_label"
        }
    }

    struct Episode: Decodable {
        let season: Int?
        let episode: Int?
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
    let artwork: HomeArtwork
    let lastWatchAt: String?
    let episode: Episode?

    enum CodingKeys: String, CodingKey {
        case id, domain, title, subtitle, progress, artwork, episode
        case titleID = "title_id", detailLabel = "detail_label"
        case playURL = "play_url", playLabel = "play_label"
        case lastWatchAt = "last_watch_at"
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
            homeTitleID: titleID, title: title, note: subtitle, watchPath: path,
            artworkID: artworkID, posterURL: artwork.poster.flatMap(URL.init(string:)),
            backdropURL: artwork.landscape.flatMap(URL.init(string:)),
            progress: progress.percent.map { Double($0) / 100 }
        )
    }

    var animeEpisodeArtworkPath: String? {
        guard domain == .anime, let season = episode?.season, (1...99).contains(season),
              let number = episode?.episode, (1...5000).contains(number),
              entry.watchPath == "/plus/watch/\(titleID)/\(number)" else { return nil }
        return "episode-artwork/anime/\(titleID)/\(season)/\(number)"
    }
}

struct HomeRow: Decodable, Identifiable {
    let id: String
    let label: String
    let cards: [HomeCard]

    func focusKey(for card: HomeCard) -> String { "home-card-\(id)-\(card.id)" }
}

struct HomeProjection: Decodable {
    struct Banner: Decodable {
        let title: String
        let mediaDetail: String
        let artwork: HomeArtwork

        enum CodingKeys: String, CodingKey {
            case title, artwork, mediaDetail = "media_detail"
        }
    }

    struct Layout: Decodable {
        struct Row: Decodable, Identifiable {
            let id: String
            let label: String
            let hidden: Bool
            let first: Bool
            let last: Bool
        }

        let rows: [Row]
    }

    let banner: Banner
    let rows: [HomeRow]
    let layout: Layout
}

@MainActor
struct HomeScreen: View {
    let client: LiveLibraryClient
    let focus: FocusState<String?>.Binding
    let refreshID: UUID
    let onSelect: (HomeCard, String) -> Void
    let browse: () -> Void
    let unavailable: () -> Void
    let expired: () -> Void

    @State private var projection: HomeProjection?
    @State private var settled: HomeCard?
    @State private var failed = false
    @State private var loading = false
    @State private var editing = false
    @State private var saving = false
    @State private var editError = false
    @FocusState private var editFocus: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        GeometryReader { viewport in
            if let projection {
                let heroHeight = min(540, max(260, viewport.size.height * 0.62))
                ScrollView {
                    VStack(alignment: .leading, spacing: 30) {
                        backdrop(projection, height: heroHeight)
                        if projection.rows.allSatisfy({ $0.cards.isEmpty }) {
                            VStack(alignment: .leading, spacing: 16) {
                                Text("Nothing in progress yet.").font(.title3.bold())
                                action("Browse library", key: "home-browse", run: browse)
                            }
                            .padding(.horizontal, 88)
                        }
                        ForEach(projection.rows.filter { !$0.cards.isEmpty }) { row in
                            shelf(row)
                        }
                    }
                    .padding(.bottom, 40)
                }
            } else {
                VStack(spacing: 22) {
                    if failed {
                        Text("Couldn’t load Home.").font(.title3.bold())
                        HStack {
                            action("Try again", key: "home-retry") { Task { await load() } }
                            action("Browse library", key: "home-browse", run: browse)
                        }
                    } else {
                        ProgressView("Loading Home").tint(Color.tvAccent)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task(id: refreshID) { await load() }
        .task(id: focus.wrappedValue) { await settleArtwork() }
        .fullScreenCover(isPresented: $editing, onDismiss: { focus.wrappedValue = "home-edit" }) {
            layoutEditor
        }
        .accessibilityIdentifier("native-home")
    }

    private func action(_ label: String, key: String, run: @escaping () -> Void) -> some View {
        Button(label, action: run)
            .buttonStyle(TVActionStyle(focused: focus.wrappedValue == key, compact: true))
            .focusEffectDisabled()
            .focused(focus, equals: key)
            .accessibilityIdentifier(key)
    }

    private func backdrop(_ projection: HomeProjection, height: CGFloat) -> some View {
        let selected = settled
        return ZStack(alignment: .bottomLeading) {
            LinearGradient(colors: [.tvRaised, .tvBackground], startPoint: .topTrailing, endPoint: .bottomLeading)
            NativeHomeArtwork(paths: [selected?.artwork.landscape ?? projection.banner.artwork.landscape],
                              client: client)
            LinearGradient(colors: [.tvBackground, .clear], startPoint: .leading, endPoint: .trailing)
            LinearGradient(colors: [.clear, .tvBackground], startPoint: .center, endPoint: .bottom)
            VStack(alignment: .leading, spacing: 14) {
                Text(selected?.title ?? projection.banner.title)
                    .font(.system(size: 66, weight: .bold))
                    .lineLimit(2)
                    .accessibilityAddTraits(.isHeader)
                let metadata = selected.map {
                    [$0.subtitle, $0.progress.remainingLabel].filter { !$0.isEmpty }.joined(separator: " · ")
                } ?? projection.banner.mediaDetail
                if !metadata.isEmpty {
                    Text(metadata).font(.title3).foregroundStyle(Color.tvMuted).lineLimit(1)
                }
                if let selected, selected.entry.watchPath != nil {
                    Label(selected.playLabel, systemImage: "play.fill")
                        .font(.callout.weight(.semibold)).foregroundStyle(Color.tvAccent)
                }
                action("Edit Home", key: "home-edit") {
                    editError = false
                    editing = true
                }
            }
            .padding(.horizontal, 88)
            .padding(.bottom, 20)
        }
        .frame(maxWidth: .infinity)
        .frame(height: height)
        .clipped()
        .focusSection()
    }

    private func shelf(_ row: HomeRow) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(row.label).font(.title3.bold())
                .padding(.horizontal, 88)
                .accessibilityAddTraits(.isHeader)
            ScrollView(.horizontal) {
                LazyHStack(alignment: .top, spacing: 26) {
                    ForEach(row.cards) { card in
                        let key = row.focusKey(for: card)
                        let focused = focus.wrappedValue == key
                        let animeResume = row.id == "continue-anime" && card.domain == .anime
                        let framing: NativeHomeArtwork.Framing = if animeResume {
                            .episode(card.animeEpisodeArtworkPath)
                        } else if row.id.hasPrefix("continue-") {
                            .fit
                        } else {
                            .fill
                        }
                        Button { onSelect(card, key) } label: {
                            VStack(alignment: .leading, spacing: 10) {
                                ZStack(alignment: .bottomLeading) {
                                    RoundedRectangle(cornerRadius: 14).fill(Color.tvSurface)
                                    NativeHomeArtwork(
                                        paths: animeResume ? [card.artwork.poster] : [card.artwork.landscape, card.artwork.poster],
                                        client: client, framing: framing
                                    )
                                    if let percent = card.progress.percent, percent > 0 {
                                        ProgressView(value: Double(min(percent, 100)), total: 100)
                                            .progressViewStyle(.linear).tint(Color.tvAccent)
                                            .padding(14)
                                            .accessibilityHidden(true)
                                    }
                                }
                                .frame(width: 380, height: 214)
                                .clipShape(RoundedRectangle(cornerRadius: 14))
                                .overlay {
                                    RoundedRectangle(cornerRadius: 14)
                                        .strokeBorder(focused ? Color.tvFocus : Color.clear, lineWidth: 4)
                                }
                                Text(card.title).font(.callout.weight(.semibold)).lineLimit(1)
                                let detail = [card.subtitle, card.detailLabel].filter { !$0.isEmpty }
                                    .joined(separator: " · ")
                                if !detail.isEmpty {
                                    Text(detail).font(.caption).foregroundStyle(Color.tvMuted).lineLimit(1)
                                }
                            }
                            .frame(width: 380, alignment: .leading)
                        }
                        .buttonStyle(TVBareButtonStyle())
                        .focusEffectDisabled()
                        .focused(focus, equals: key)
                        .scaleEffect(focused && !reduceMotion ? 1.03 : 1)
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.16), value: focused)
                        .accessibilityIdentifier(key)
                    }
                }
                .padding(.horizontal, 88)
                .padding(.vertical, 9)
            }
        }
        .focusSection()
    }

    private var layoutEditor: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack {
                Text("Edit Home").font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
                Spacer()
                editAction("Reset", key: "reset") { save("reset") }
                editAction("Done", key: "done") { editing = false }
            }
            if editError { Text("Couldn’t save this Home layout.").foregroundStyle(Color.tvWarning) }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(projection?.layout.rows ?? []) { row in
                        HStack(spacing: 18) {
                            Text(row.label).font(.callout.weight(.semibold))
                            Spacer()
                            editAction("Move up", key: "up-\(row.id)") { save("move-up", row.id) }
                                .disabled(row.first)
                            editAction("Move down", key: "down-\(row.id)") { save("move-down", row.id) }
                                .disabled(row.last)
                            editAction(row.hidden ? "Show" : "Hide", key: "toggle-\(row.id)") {
                                save(row.hidden ? "add" : "remove", row.id)
                            }
                        }
                        .padding(18)
                        .background(Color.tvSurface, in: RoundedRectangle(cornerRadius: 14))
                    }
                }
            }
        }
        .padding(.horizontal, 88).padding(.vertical, 54)
        .background(Color.tvBackground.ignoresSafeArea())
        .defaultFocus($editFocus, "done")
        .onExitCommand { editing = false }
    }

    private func editAction(_ label: String, key: String, run: @escaping () -> Void) -> some View {
        Button(label, action: run)
            .buttonStyle(TVActionStyle(focused: editFocus == key, compact: true))
            .focusEffectDisabled()
            .focused($editFocus, equals: key)
            .disabled(saving)
    }

    private func save(_ action: String, _ row: String = "") {
        Task {
            guard !saving else { return }
            saving = true
            editError = false
            defer { saving = false }
            do {
                try await client.editHome(action: action, target: row)
                projection = try await client.homeProjection(as: HomeProjection.self)
                if let selected = settled, findCard(focus.wrappedValue)?.id != selected.id {
                    settled = nil
                }
            } catch FixtureConnectionError.unauthorized {
                editing = false
                expired()
            } catch {
                editError = true
            }
        }
    }

    private func load() async {
        guard !loading else { return }
        loading = true
        failed = false
        defer { loading = false }
        do {
            let loaded = try await client.homeProjection(as: HomeProjection.self)
            guard !Task.isCancelled else { return }
            projection = loaded
            if let card = findCard(focus.wrappedValue) {
                settled = card
                return
            }
            let first = loaded.rows.lazy.compactMap { row in
                row.cards.first(where: { $0.entry.watchPath != nil }).map {
                    (card: $0, key: row.focusKey(for: $0))
                }
            }.first
            settled = first?.card
            if focus.wrappedValue == nil || focus.wrappedValue == "home-entry" {
                await Task.yield()
                guard !Task.isCancelled else { return }
                focus.wrappedValue = first?.key ?? "home-edit"
            }
        } catch LiveLibraryError.copyUnavailable {
            if !Task.isCancelled { unavailable() }
        } catch FixtureConnectionError.unauthorized {
            if !Task.isCancelled { expired() }
        } catch is CancellationError {
            return
        } catch {
            failed = true
        }
    }

    private func findCard(_ key: String?) -> HomeCard? {
        guard let key else { return nil }
        for row in projection?.rows ?? [] {
            if let card = row.cards.first(where: { row.focusKey(for: $0) == key }) {
                return card
            }
        }
        return nil
    }

    private func settleArtwork() async {
        let key = focus.wrappedValue
        guard let card = findCard(key) else { return }
        do { try await Task.sleep(for: .milliseconds(220)) }
        catch { return }
        if !Task.isCancelled, focus.wrappedValue == key { settled = card }
    }
}

@MainActor
private struct NativeHomeArtwork: View {
    enum Framing {
        case fill, fit, episode(String?)
    }

    let paths: [String?]
    let client: LiveLibraryClient
    var framing = Framing.fill
    @State private var image: UIImage?
    @State private var episodeImage: (path: String, image: UIImage)?

    private var episodePath: String? {
        if case let .episode(path) = framing { return path }
        return nil
    }

    var body: some View {
        GeometryReader { geometry in
            if let episodeImage, episodeImage.path == episodePath {
                picture(episodeImage.image, size: geometry.size, contentMode: .fill)
            } else if let image {
                switch framing {
                case .fill:
                    picture(image, size: geometry.size, contentMode: .fill)
                case .fit:
                    picture(image, size: geometry.size, contentMode: .fit)
                case .episode:
                    ZStack {
                        LinearGradient(colors: [.tvRaised, .tvBackground],
                                       startPoint: .topLeading, endPoint: .bottomTrailing)
                        Image(uiImage: image).resizable().scaledToFit().padding(.vertical, 10)
                    }
                }
            }
        }
        .accessibilityHidden(true)
        .task(id: paths) {
            image = nil
            for path in paths.compactMap({ $0 }) {
                guard !Task.isCancelled else { return }
                let key = "home:\(path)"
                if let cached = TVArtworkCache.shared.cached(for: key) {
                    image = cached
                    return
                }
                if let data = try? await client.homeArtwork(path),
                   let loaded = await TVArtworkCache.shared.decodeAndStore(data, key: key, maxPixelSize: 640) {
                    if !Task.isCancelled { image = loaded }
                    return
                }
            }
        }
        .task(id: episodePath) {
            episodeImage = nil
            guard let path = episodePath else { return }
            let key = "details:\(path)"
            if let cached = TVArtworkCache.shared.cached(for: key) {
                episodeImage = (path, cached)
                return
            }
            guard let data = try? await client.detailsArtwork(path: path),
                  !Task.isCancelled,
                  let still = await TVArtworkCache.shared.decodeAndStore(data, key: key, maxPixelSize: 960),
                  !Task.isCancelled else { return }
            episodeImage = (path, still)
        }
    }

    private func picture(_ image: UIImage, size: CGSize, contentMode: ContentMode) -> some View {
        Image(uiImage: image).resizable().aspectRatio(contentMode: contentMode)
            .frame(width: size.width, height: size.height)
            .clipped()
    }
}

@MainActor
struct TVRequestsScreen: View {
    let client: LiveLibraryClient
    let close: () -> Void

    @State private var page: LiveRequestsPage?
    @State private var selected: LiveTitleRequest?
    @State private var category = "active"
    @State private var error: String?
    @State private var loading = false
    @State private var statusByID: [String: LiveTitleRequest] = [:]
    @FocusState private var focus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 24) {
                Text("Requests").font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
                Spacer()
                if selected == nil {
                    button("Refresh", key: "requests-refresh") { Task { await load() } }
                        .disabled(loading)
                }
                button(selected == nil ? "Done" : "Back", key: "requests-back", run: back)
            }
            if let error { Text(error).foregroundStyle(Color.tvWarning) }
            if let selected {
                detail(selected)
            } else if let page {
                HStack(spacing: 20) {
                    ForEach(page.categories) { item in
                        button(item.title, key: "requests-\(item.key)", selected: category == item.key) {
                            category = item.key
                        }
                    }
                }
                .focusSection()
                let requests = page.categories.first { $0.key == category }?.requests ?? []
                if requests.isEmpty {
                    Text("There are no requests here.").foregroundStyle(Color.tvMuted)
                    Spacer()
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 14) {
                            ForEach(requests) { request in
                                let current = statusByID[request.id] ?? request
                                let key = "requests-row-\(current.id)"
                                Button {
                                    selected = current
                                    error = nil
                                    Task { @MainActor in
                                        await Task.yield()
                                        if selected?.id == current.id {
                                            focus = current.canCheck == true ? "requests-check" : "requests-back"
                                        }
                                    }
                                } label: {
                                    HStack {
                                        Text(current.title).foregroundStyle(Color.tvText)
                                        Spacer()
                                        Text(current.displayStatus).foregroundStyle(Color.tvAccent)
                                    }
                                    .font(.callout.weight(.semibold))
                                    .padding(22)
                                    .background(Color.tvSurface, in: RoundedRectangle(cornerRadius: 12))
                                    .overlay {
                                        RoundedRectangle(cornerRadius: 12)
                                            .strokeBorder(focus == key ? Color.tvFocus : Color.clear, lineWidth: 3)
                                    }
                                }
                                .buttonStyle(TVBareButtonStyle()).focusEffectDisabled().focused($focus, equals: key)
                                .accessibilityIdentifier(key)
                            }
                        }
                        .focusSection()
                    }
                }
            } else {
                if loading { ProgressView("Loading Requests") }
                Spacer()
            }
        }
        .padding(.horizontal, 88).padding(.vertical, 54)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .foregroundStyle(Color.tvText)
        .background(Color.tvBackground.ignoresSafeArea())
        .accessibilityIdentifier("native-requests")
        .defaultFocus($focus, "requests-back")
        .task {
            await load()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(60)) }
                catch { return }
                if selected == nil, category != "history" { await load() }
            }
        }
        .onExitCommand(perform: back)
    }

    private func detail(_ item: LiveTitleRequest) -> some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(alignment: .firstTextBaseline, spacing: 28) {
                Text(item.title).font(.title.bold()).accessibilityAddTraits(.isHeader)
                Spacer(minLength: 24)
                if item.canCheck == true {
                    button(loading ? "Checking…" : "Check status", key: "requests-check") {
                        Task { await check(item) }
                    }
                    .accessibilityHint(item.kind == "movie"
                        ? "Check Riven and, if needed, the configured sources for this movie"
                        : "Refresh this request")
                }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 30) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(item.displayStatus).font(.title3.bold()).foregroundStyle(Color.tvAccent)
                        if !item.detail.isEmpty { Text(item.detail) }
                        if let checked = formattedDate(item.lastCheckedAt) {
                            Text("Status checked \(checked)").font(.callout).foregroundStyle(Color.tvMuted)
                        }
                    }
                    if item.fallbackSearch != nil || item.fallbackTransfer != nil {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Fallback sources").font(.callout.bold()).foregroundStyle(Color.tvAccent)
                                .accessibilityAddTraits(.isHeader)
                            if let search = item.fallbackSearch {
                                Text(search.headline)
                                if let checked = formattedDate(search.checkedAt) {
                                    Text("Searched \(checked)").font(.callout).foregroundStyle(Color.tvMuted)
                                }
                            }
                            if let transfer = item.fallbackTransfer {
                                Text(transfer.headline).foregroundStyle(Color.tvText)
                            }
                        }
                    } else if let next = item.nextAction, next.kind != "check", next.label != item.detail {
                        Text(next.label).font(.callout).foregroundStyle(Color.tvMuted)
                    }
                }
                .frame(maxWidth: 1240, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func formattedDate(_ value: String?) -> String? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        let parts = formatter.formatOptions
        formatter.formatOptions = parts.union(.withFractionalSeconds)
        let fractional = formatter.date(from: value)
        formatter.formatOptions = parts
        let date = fractional ?? formatter.date(from: value)
        return date?.formatted(date: .abbreviated, time: .shortened)
    }

    private func button(_ title: String, key: String, selected: Bool = false,
                        run: @escaping () -> Void) -> some View {
        Button(title, action: run)
            .buttonStyle(TVActionStyle(focused: focus == key, compact: true, selected: selected))
            .focusEffectDisabled()
            .focused($focus, equals: key)
            .accessibilityIdentifier(key)
    }

    private func back() {
        guard let selected else { close(); return }
        let key = "requests-row-\(selected.id)"
        self.selected = nil
        error = nil
        Task {
            await Task.yield()
            if self.selected == nil { focus = key }
        }
    }

    private func load() async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        do {
            let fetched = try await client.requests()
            guard !Task.isCancelled else { return }
            page = fetched
            statusByID = [:]
            error = nil
        } catch is CancellationError {
            return
        } catch {
            self.error = "Couldn’t load Requests."
        }
    }

    private func check(_ item: LiveTitleRequest) async {
        guard !loading else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            let updated = try await client.checkRequest(item)
            guard !Task.isCancelled else { return }
            statusByID[item.id] = updated
            if selected?.id == item.id {
                selected = updated
                if updated.canCheck != true { focus = "requests-back" }
            }
        } catch is CancellationError {
            return
        } catch {
            self.error = "Couldn’t check this request."
        }
    }
}

