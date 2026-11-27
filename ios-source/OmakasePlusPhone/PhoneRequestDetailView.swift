import QualificationCore
import SwiftUI

struct PhoneRequestDetailView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient
    let initialItem: LiveTitleRequest
    var onUpdated: ((LiveTitleRequest) -> Void)?

    @State private var item: LiveTitleRequest
    @State private var checking = false
    @State private var errorMessage: String?

    init(
        store: PhoneSessionStore,
        client: LiveLibraryClient,
        item: LiveTitleRequest,
        onUpdated: ((LiveTitleRequest) -> Void)? = nil
    ) {
        self.store = store
        self.client = client
        self.initialItem = item
        self.onUpdated = onUpdated
        _item = State(initialValue: item)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                headerCard
                if let errorMessage {
                    Text(errorMessage)
                        .font(.subheadline)
                        .foregroundStyle(Color.appWarning)
                }
                if let ratio = item.episodeProgressRatio,
                   let ready = item.episodesReady,
                   let expected = item.episodesExpected {
                    episodeProgressCard(ready: ready, expected: expected, ratio: ratio)
                }
                actionButtons
                if let stages = item.stages, !stages.isEmpty {
                    stagesCard(stages)
                }
                if item.fallbackSearch != nil || item.fallbackTransfer != nil {
                    fallbackCard
                }
            }
            .padding(16)
        }
        .background(Color.appBackground.ignoresSafeArea())
        .foregroundStyle(Color.appText)
        .navigationTitle(item.title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private var headerCard: some View {
        HStack(alignment: .top, spacing: 14) {
            PhoneArtworkView(
                cacheKey: "req-detail:\(item.id):\(item.poster ?? "")",
                source: (item.poster?.isEmpty == false) ? .request(client, item) : nil,
                fallbackIcon: "tray"
            )
            .frame(width: 96, height: 144)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))

            VStack(alignment: .leading, spacing: 8) {
                Text(item.title)
                    .font(.title3.bold())
                HStack(spacing: 8) {
                    Text(kindLabel)
                    if let year = item.releaseYear {
                        Text(String(year))
                    }
                }
                .font(.caption.weight(.medium))
                .foregroundStyle(Color.appMuted)

                Text(item.displayStatus)
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.appAccent.opacity(0.18), in: Capsule())
                    .foregroundStyle(Color.appAccent)

                if !item.detail.isEmpty {
                    Text(item.detail)
                        .font(.subheadline)
                        .foregroundStyle(Color.appText)
                }
                if let reason = item.reasonText, !reason.isEmpty, reason != item.detail {
                    Text(reason)
                        .font(.caption)
                        .foregroundStyle(Color.appWarning)
                }
                if let checked = formattedDate(item.lastCheckedAt) {
                    Text("Last checked \(checked)")
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    private func episodeProgressCard(ready: Int, expected: Int, ratio: Double) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Episode availability")
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Text("\(ready) of \(expected) episodes ready")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(Color.appAccent)
            }
            ProgressView(value: ratio)
                .tint(Color.appAccent)
        }
        .padding(14)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    @ViewBuilder
    private var actionButtons: some View {
        HStack(spacing: 12) {
            if item.canCheck == true {
                Button {
                    Task { await checkStatus() }
                } label: {
                    Label(checking ? "Checking…" : "Check status", systemImage: "arrow.clockwise")
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                }
                .buttonStyle(.borderedProminent)
                .tint(Color.appAccent)
                .foregroundStyle(Color.appBackground)
                .disabled(checking)
                .accessibilityIdentifier("phone.request.check")
            }

            if item.isReadyInLibrary, let libraryEntry {
                NavigationLink {
                    PhoneTitleDetailView(
                        store: store,
                        client: client,
                        kind: libraryKind,
                        entry: libraryEntry
                    )
                } label: {
                    Label("Open in Library", systemImage: "play.rectangle.fill")
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                }
                .buttonStyle(.bordered)
                .tint(Color.appAccent)
                .accessibilityIdentifier("phone.request.openLibrary")
            }
        }
    }

    private func stagesCard(_ stages: [LiveTitleRequest.Stage]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Progress stages")
                .font(.headline)
            ForEach(stages) { stage in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: iconName(for: stage.state))
                        .foregroundStyle(iconColor(for: stage.state))
                        .frame(width: 20)
                        .padding(.top, 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(stage.label)
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(stage.state == "unknown" ? Color.appMuted : Color.appText)
                        if let occurred = formattedDate(stage.occurredAt) {
                            Text(occurred)
                                .font(.caption2)
                                .foregroundStyle(Color.appMuted)
                        }
                    }
                    Spacer()
                }
            }
        }
        .padding(14)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    private var fallbackCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Fallback sources")
                .font(.headline)
            if let search = item.fallbackSearch {
                Text(search.headline)
                    .font(.subheadline)
                if let checked = formattedDate(search.checkedAt) {
                    Text("Searched \(checked)")
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                }
            }
            if let transfer = item.fallbackTransfer {
                Text(transfer.headline)
                    .font(.subheadline)
                    .foregroundStyle(Color.appAccent)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    private var kindLabel: String {
        switch item.kind {
        case "anime": "Anime"
        case "tv": "TV Show"
        default: "Movie"
        }
    }

    private var libraryKind: LiveLibraryKind {
        switch item.kind {
        case "anime": .anime
        case "tv": .tv
        default: .movie
        }
    }

    private var libraryEntry: LiveLibraryEntry? {
        guard let libraryID = item.libraryID, !libraryID.isEmpty else { return nil }
        let tmdb = item.kind == "tv" ? Int(item.titleID) : nil
        let anilist = item.kind == "anime" ? Int(item.titleID) : nil
        let artworkID = item.kind == "anime" ? libraryID : nil
        let posterURL: URL? = if let poster = item.poster, poster.hasPrefix("https://") {
            URL(string: poster)
        } else {
            nil
        }
        let watchPath = item.kind == "movie" ? "/plus/watch/title/movie/\(libraryID)" : nil
        return LiveLibraryEntry(
            homeTitleID: libraryID,
            title: item.title,
            note: item.displayStatus,
            watchPath: watchPath,
            artworkID: artworkID,
            posterURL: posterURL,
            backdropURL: nil,
            progress: nil,
            summary: item.detail,
            year: item.releaseYear,
            anilistID: anilist,
            tmdbID: tmdb
        )
    }

    private func iconName(for state: String) -> String {
        switch state {
        case "done": "checkmark.circle.fill"
        case "current": "clock.fill"
        case "stopped": "exclamationmark.triangle.fill"
        default: "circle"
        }
    }

    private func iconColor(for state: String) -> Color {
        switch state {
        case "done": Color.appAccent
        case "current": Color.appText
        case "stopped": Color.appWarning
        default: Color.appMuted.opacity(0.6)
        }
    }

    private func formattedDate(_ value: String?) -> String? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        let base = formatter.formatOptions
        formatter.formatOptions = base.union(.withFractionalSeconds)
        let fractional = formatter.date(from: value)
        formatter.formatOptions = base
        let parsed = fractional ?? formatter.date(from: value)
        return parsed?.formatted(date: .abbreviated, time: .shortened)
    }

    private func checkStatus() async {
        checking = true
        errorMessage = nil
        defer { checking = false }
        do {
            let updated = try await client.checkRequest(item)
            item = updated
            onUpdated?(updated)
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            errorMessage = "Could not refresh request status."
        }
    }
}
