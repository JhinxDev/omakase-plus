import QualificationCore
import SwiftUI

struct PhoneCatalogDetailView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient
    let card: LiveRecommendation
    var onStatusUpdated: ((LiveTitleRequest) -> Void)?
    var onDismissed: (() -> Void)?

    @Environment(\.dismiss) private var dismiss
    @State private var requestState: LiveTitleRequest?
    @State private var submitting = false
    @State private var checking = false
    @State private var dismissing = false
    @State private var showTrackSheet = false
    @State private var showTrailer = false
    @State private var feedbackMessage: String?
    @State private var errorMessage: String?

    init(
        store: PhoneSessionStore,
        client: LiveLibraryClient,
        card: LiveRecommendation,
        initialRequest: LiveTitleRequest? = nil,
        onStatusUpdated: ((LiveTitleRequest) -> Void)? = nil,
        onDismissed: (() -> Void)? = nil
    ) {
        self.store = store
        self.client = client
        self.card = card
        self.onStatusUpdated = onStatusUpdated
        self.onDismissed = onDismissed
        _requestState = State(initialValue: initialRequest ?? card.request)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                heroSection
                actionSection
                if let feedbackMessage {
                    Text(feedbackMessage)
                        .font(.subheadline)
                        .foregroundStyle(Color.appAccent)
                }
                if let errorMessage {
                    Text(errorMessage)
                        .font(.subheadline)
                        .foregroundStyle(Color.appWarning)
                }
                if let summary = card.summary, !summary.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Overview")
                            .font(.headline)
                        Text(summary)
                            .font(.subheadline)
                            .foregroundStyle(Color.appMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
                }
                if let requestState {
                    requestStatusCard(requestState)
                }
            }
            .padding(16)
        }
        .background(Color.appBackground.ignoresSafeArea())
        .foregroundStyle(Color.appText)
        .navigationTitle(card.title)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showTrackSheet) {
            PhoneTrackSheet(
                store: store,
                client: client,
                kind: card.kind,
                mediaID: card.trackMediaID,
                imdbID: card.kind == "movie" ? card.id : nil,
                title: card.title,
                initialStatus: card.watchStatus,
                initialProgress: nil,
                episodeTotal: card.episodes,
                initialRating: nil,
                allowDismiss: true
            ) { result in
                feedbackMessage = result.detail
                if result.removeFromForYou == true {
                    onDismissed?()
                }
            }
        }
        .sheet(isPresented: $showTrailer) {
            if let trailerURL = card.youtubeTrailerURL {
                PhoneSafariView(url: trailerURL)
                    .ignoresSafeArea()
            }
        }
    }

    private var heroSection: some View {
        HStack(alignment: .top, spacing: 14) {
            PhoneArtworkView(
                cacheKey: "catalog-detail:\(card.key)",
                source: .recommendation(client, card),
                fallbackIcon: "film"
            )
            .frame(width: 112, height: 168)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))

            VStack(alignment: .leading, spacing: 8) {
                Text(card.title)
                    .font(.title3.bold())

                let facts = metadataChips
                if !facts.isEmpty {
                    Text(facts.joined(separator: " · "))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Color.appMuted)
                }

                if let genres = card.genres, !genres.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(genres, id: \.self) { genre in
                                Text(genre)
                                    .font(.caption2.weight(.semibold))
                                    .padding(.horizontal, 8)
                                    .padding(.vertical, 4)
                                    .background(Color.appRaised, in: Capsule())
                                    .foregroundStyle(Color.appMuted)
                            }
                        }
                    }
                }

                if let scoreText = formattedScore {
                    Label(scoreText, systemImage: "star.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.appAccent)
                }

                if card.owned == true && requestState == nil {
                    Text("In your library")
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(Color.appAccent.opacity(0.18), in: Capsule())
                        .foregroundStyle(Color.appAccent)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var actionSection: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                if requestState == nil && card.canRequest {
                    Button {
                        Task { await requestTitle() }
                    } label: {
                        Label(submitting ? "Requesting…" : "Request", systemImage: "plus.circle.fill")
                            .font(.subheadline.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.appAccent)
                    .foregroundStyle(Color.appBackground)
                    .disabled(submitting || dismissing)
                    .accessibilityIdentifier("phone.catalog.request")
                }

                Button {
                    showTrackSheet = true
                } label: {
                    Label("Track & rate", systemImage: "checklist")
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                }
                .buttonStyle(.bordered)
                .tint(Color.appAccent)
                .disabled(submitting || dismissing)
                .accessibilityIdentifier("phone.catalog.track")
            }

            HStack(spacing: 10) {
                if card.youtubeTrailerURL != nil {
                    Button {
                        showTrailer = true
                    } label: {
                        Label("Watch Trailer", systemImage: "play.rectangle")
                            .font(.subheadline.weight(.medium))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(.bordered)
                    .tint(Color.appText)
                    .accessibilityIdentifier("phone.catalog.trailer")
                }

                Button(role: .destructive) {
                    Task { await dismissRecommendation() }
                } label: {
                    Label(dismissing ? "Hiding…" : "Not for me", systemImage: "eye.slash")
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                }
                .buttonStyle(.bordered)
                .tint(Color.appWarning)
                .disabled(submitting || dismissing || (card.trackMediaID == nil && card.kind != "movie"))
                .accessibilityIdentifier("phone.catalog.dismiss")
            }
        }
    }

    private func requestStatusCard(_ status: LiveTitleRequest) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Request status")
                    .font(.headline)
                Spacer()
                Text(status.displayStatus)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.appAccent)
            }
            if !status.detail.isEmpty {
                Text(status.detail)
                    .font(.subheadline)
                    .foregroundStyle(Color.appMuted)
            }
            HStack(spacing: 10) {
                if status.canCheck == true {
                    Button {
                        Task { await checkStatus(status) }
                    } label: {
                        Label(checking ? "Checking…" : "Check status", systemImage: "arrow.clockwise")
                            .font(.caption.weight(.semibold))
                    }
                    .buttonStyle(.bordered)
                    .tint(Color.appAccent)
                    .disabled(checking)
                }
                NavigationLink {
                    PhoneRequestDetailView(
                        store: store,
                        client: client,
                        item: status,
                        onUpdated: { updated in
                            requestState = updated
                            onStatusUpdated?(updated)
                        }
                    )
                } label: {
                    Label("View stages", systemImage: "list.bullet.rectangle")
                        .font(.caption.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .tint(Color.appText)
            }
        }
        .padding(14)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    private var metadataChips: [String] {
        var chips: [String] = []
        if let format = card.format, !format.isEmpty {
            chips.append(format)
        } else {
            chips.append(card.kind == "anime" ? "Anime" : card.kind == "tv" ? "TV Show" : "Movie")
        }
        if let year = card.year {
            chips.append(String(year))
        }
        if let episodes = card.episodes, episodes > 0 {
            chips.append(episodes == 1 ? "1 episode" : "\(episodes) episodes")
        }
        if card.airing == true {
            chips.append("Airing")
        }
        return chips
    }

    private var formattedScore: String? {
        guard let score = card.score else { return nil }
        let value = score == floor(score) ? String(Int(score)) : String(format: "%.1f", score)
        if let label = card.scoreLabel, !label.isEmpty {
            return "\(value)% · \(label)"
        }
        return "\(value)%"
    }

    private func requestTitle() async {
        submitting = true
        errorMessage = nil
        feedbackMessage = nil
        defer { submitting = false }
        do {
            let result = try await client.requestTitle(card)
            requestState = result
            feedbackMessage = "\(result.title): \(result.displayStatus)"
            onStatusUpdated?(result)
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            errorMessage = "Could not submit request."
        }
    }

    private func checkStatus(_ status: LiveTitleRequest) async {
        checking = true
        errorMessage = nil
        defer { checking = false }
        do {
            let updated = try await client.checkRequest(status)
            requestState = updated
            onStatusUpdated?(updated)
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            errorMessage = "Status check failed."
        }
    }

    private func dismissRecommendation() async {
        guard card.trackMediaID != nil || card.kind == "movie" else { return }
        dismissing = true
        errorMessage = nil
        defer { dismissing = false }
        do {
            _ = try await client.trackTitle(
                kind: card.kind,
                mediaID: card.trackMediaID,
                imdbID: card.kind == "movie" ? card.id : nil,
                title: card.title,
                status: "not_for_me"
            )
            onDismissed?()
            dismiss()
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            errorMessage = "Could not hide this title."
        }
    }
}
