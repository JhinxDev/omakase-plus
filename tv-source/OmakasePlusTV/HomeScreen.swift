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

    enum CodingKeys: String, CodingKey {
        case id, domain, title, subtitle, progress, artwork
        case titleID = "title_id", detailLabel = "detail_label"
        case playURL = "play_url", playLabel = "play_label"
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
                        Button { onSelect(card, key) } label: {
                            VStack(alignment: .leading, spacing: 10) {
                                ZStack(alignment: .bottomLeading) {
                                    RoundedRectangle(cornerRadius: 14).fill(Color.tvSurface)
                                    NativeHomeArtwork(paths: [card.artwork.landscape, card.artwork.poster],
                                                      client: client)
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
                        .buttonStyle(.plain)
                        .focusEffectDisabled()
                        .focused(focus, equals: key)
                        .scaleEffect(focused ? 1.03 : 1)
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
    let paths: [String?]
    let client: LiveLibraryClient
    @State private var image: UIImage?

    var body: some View {
        GeometryReader { geometry in
            if let image {
                Image(uiImage: image)
                    .resizable().scaledToFill()
                    .frame(width: geometry.size.width, height: geometry.size.height)
                    .clipped()
            }
        }
        .accessibilityHidden(true)
        .task(id: paths) {
            image = nil
            for path in paths.compactMap({ $0 }) {
                guard !Task.isCancelled else { return }
                if let data = try? await client.homeArtwork(path), let loaded = UIImage(data: data) {
                    if !Task.isCancelled { image = loaded }
                    return
                }
            }
        }
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
                        VStack(alignment: .leading, spacing: 14) {
                            ForEach(requests) { request in
                                let current = statusByID[request.id] ?? request
                                let key = "requests-row-\(current.id)"
                                Button {
                                    selected = current
                                    error = nil
                                    focus = "requests-back"
                                } label: {
                                    HStack {
                                        Text(current.title).foregroundStyle(Color.tvText)
                                        Spacer()
                                        Text(current.label).foregroundStyle(Color.tvAccent)
                                    }
                                    .font(.callout.weight(.semibold))
                                    .padding(22)
                                    .background(Color.tvSurface, in: RoundedRectangle(cornerRadius: 12))
                                    .overlay {
                                        RoundedRectangle(cornerRadius: 12)
                                            .strokeBorder(focus == key ? Color.tvFocus : Color.clear, lineWidth: 3)
                                    }
                                }
                                .buttonStyle(.plain).focusEffectDisabled().focused($focus, equals: key)
                                .accessibilityIdentifier(key)
                            }
                        }
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
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(item.title).font(.title.bold()).accessibilityAddTraits(.isHeader)
                Text(item.label).font(.title3.bold()).foregroundStyle(Color.tvAccent)
                if !item.detail.isEmpty { Text(item.detail) }
                if let reason = item.reasonText, !reason.isEmpty, reason != item.detail {
                    Text(reason).foregroundStyle(Color.tvMuted)
                }
                if let checked = formattedDate(item.lastCheckedAt) {
                    Text("Last checked \(checked)").foregroundStyle(Color.tvMuted)
                }
                ForEach(item.stages ?? []) { stage in
                    Label {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(stage.label)
                            if let date = formattedDate(stage.occurredAt) {
                                Text(date).font(.caption).foregroundStyle(Color.tvMuted)
                            }
                        }
                    } icon: {
                        Image(systemName: stage.occurredAt == nil ? "circle" : "checkmark.circle")
                            .foregroundStyle(Color.tvAccent)
                    }
                }
                if item.canCheck == true {
                    button("Check status", key: "requests-check") { Task { await check(item) } }
                        .disabled(loading)
                }
                if let next = item.nextAction, let reason = next.reason, !reason.isEmpty {
                    Text(reason).foregroundStyle(Color.tvMuted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
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
            if selected?.id == item.id { selected = updated }
        } catch is CancellationError {
            return
        } catch {
            self.error = "Couldn’t check this request."
        }
    }
}

