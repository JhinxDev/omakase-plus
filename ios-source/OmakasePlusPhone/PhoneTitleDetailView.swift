import QualificationCore
import SwiftUI

struct PhoneTitleDetailView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient
    let kind: LiveLibraryKind
    let entry: LiveLibraryEntry

    @State private var episodePage: LiveLibraryPage?
    @State private var episodes: [LiveLibraryEntry] = []
    @State private var selectedSeason: Int?
    @State private var loading = false
    @State private var errorMessage: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                headerBanner
                summaryBlock
                if kind != .movie {
                    episodesBlock
                }
            }
            .padding(.bottom, 28)
        }
        .background(Color.appBackground.ignoresSafeArea())
        .foregroundStyle(Color.appText)
        .navigationTitle(entry.title)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if kind != .movie {
                await loadEpisodes(season: nil)
            }
        }
    }

    private var headerBanner: some View {
        ZStack(alignment: .bottomLeading) {
            PhoneArtworkView(
                cacheKey: "detail-banner:\(kind.rawValue):\(entry.id)",
                source: .entry(client, entry, true)
            )
            .frame(height: 220)
            .frame(maxWidth: .infinity)
            .overlay(
                LinearGradient(
                    colors: [Color.clear, Color.appBackground.opacity(0.65), Color.appBackground],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )

            VStack(alignment: .leading, spacing: 8) {
                Text(entry.title)
                    .font(.title2.bold())
                if !entry.note.isEmpty {
                    Text(entry.note)
                        .font(.subheadline)
                        .foregroundStyle(Color.appMuted)
                }
                if let primaryTarget = playableHeroTarget {
                    Button {
                        Task { await store.play(primaryTarget.entry, featured: primaryTarget.featured) }
                    } label: {
                        Label(
                            entry.progress ?? 0 > 0 ? "Resume" : "Play",
                            systemImage: "play.fill"
                        )
                        .font(.subheadline.weight(.semibold))
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.appAccent)
                    .foregroundStyle(Color.appBackground)
                    .disabled(store.openingPlayback)
                } else if let unavailable = entry.unavailableLabel {
                    Text(unavailable)
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.appSurface, in: Capsule())
                        .foregroundStyle(Color.appMuted)
                }
            }
            .padding(16)
        }
        .frame(maxWidth: .infinity, alignment: .bottomLeading)
        .clipped()
    }

    private var playableHeroTarget: (entry: LiveLibraryEntry, featured: Bool)? {
        if entry.featuredPath != nil {
            return (entry, true)
        }
        if entry.watchPath != nil {
            return (entry, false)
        }
        if let featured = episodePage?.featured, featured.watchPath != nil {
            return (featured, false)
        }
        if let first = episodes.first(where: { $0.watchPath != nil }) {
            return (first, false)
        }
        return nil
    }

    @ViewBuilder
    private var summaryBlock: some View {
        let synopsis = episodePage?.titleSummary ?? entry.summary
        if let synopsis, !synopsis.isEmpty {
            Text(synopsis)
                .font(.subheadline)
                .foregroundStyle(Color.appMuted)
                .padding(.horizontal, 16)
        }
    }

    private var episodesBlock: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Episodes")
                    .font(.headline)
                Spacer()
                if let seasons = episodePage?.availability?.seasons, seasons.count > 1 {
                    Menu {
                        ForEach(seasons, id: \.self) { season in
                            Button("Season \(season)") {
                                selectedSeason = season
                                Task { await loadEpisodes(season: season) }
                            }
                        }
                    } label: {
                        Label("Season \(selectedSeason ?? seasons.first ?? 1)", systemImage: "chevron.down")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Color.appAccent)
                    }
                }
            }
            .padding(.horizontal, 16)

            if let errorMessage {
                Text(errorMessage)
                    .font(.subheadline)
                    .foregroundStyle(Color.appWarning)
                    .padding(.horizontal, 16)
            }

            if loading && episodes.isEmpty {
                ProgressView("Loading episodes…")
                    .tint(Color.appAccent)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 24)
            } else if episodes.isEmpty {
                Text("No episodes available.")
                    .font(.subheadline)
                    .foregroundStyle(Color.appMuted)
                    .padding(.horizontal, 16)
            } else {
                LazyVStack(spacing: 10) {
                    ForEach(episodes) { episode in
                        episodeRow(episode)
                    }
                }
                .padding(.horizontal, 16)
            }
        }
    }

    private func episodeRow(_ episode: LiveLibraryEntry) -> some View {
        Button {
            guard episode.watchPath != nil else { return }
            Task { await store.play(episode) }
        } label: {
            HStack(spacing: 12) {
                PhoneArtworkView(
                    cacheKey: "ep:\(episode.id):\(episode.episodeArtwork ?? "")",
                    source: .entry(client, episode, true),
                    fallbackIcon: "play.rectangle"
                )
                .frame(width: 112, height: 64)
                .clipShape(RoundedRectangle(cornerRadius: 8))

                VStack(alignment: .leading, spacing: 4) {
                    Text(episode.episodeDisplayTitle)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                        .foregroundStyle(Color.appText)
                    HStack(spacing: 8) {
                        if !episode.note.isEmpty {
                            Text(episode.note)
                        }
                        if let duration = episode.durationLabel {
                            Text(duration)
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(Color.appMuted)
                    .lineLimit(1)
                    if let pct = episode.progress, pct > 0 {
                        ProgressView(value: min(1, max(0, pct)))
                            .tint(Color.appAccent)
                    }
                }
                Spacer()
                if episode.watched == true {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(Color.appAccent)
                } else if episode.watchPath != nil {
                    Image(systemName: "play.circle.fill")
                        .font(.title3)
                        .foregroundStyle(Color.appAccent)
                } else {
                    Text(episode.unavailableLabel ?? "Unavailable")
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                }
            }
            .padding(10)
            .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(episode.watchPath == nil || store.openingPlayback)
    }

    private func loadEpisodes(season: Int?) async {
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            let loaded = try await client.page(
                kind: kind,
                title: entry,
                offset: 0,
                season: season
            )
            episodePage = loaded
            episodes = loaded.items
            if selectedSeason == nil, let firstSeason = loaded.availability?.seasons?.first {
                selectedSeason = firstSeason
            }
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch is CancellationError {
            return
        } catch {
            errorMessage = "Could not load episodes."
        }
    }
}
