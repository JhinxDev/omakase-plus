import QualificationCore
import SwiftUI

struct PhoneDiscoverView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient

    @State private var category: LiveRecommendationCategory = .anime
    @State private var page: LiveRecommendationsPage?
    @State private var statusByID: [String: LiveTitleRequest] = [:]
    @State private var dismissedKeys: Set<String> = []
    @State private var searchText = ""
    @State private var searching = false
    @State private var loading = false
    @State private var requestingID: String?
    @State private var requestFeedback: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if !searching {
                    Picker("Discover category", selection: $category) {
                        ForEach(LiveRecommendationCategory.allCases) { item in
                            Text(item.label).tag(item)
                        }
                    }
                    .pickerStyle(.segmented)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                }

                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        if let requestFeedback {
                            Text(requestFeedback)
                                .font(.footnote)
                                .foregroundStyle(Color.appAccent)
                                .padding(.horizontal, 16)
                        }
                        if let warnings = page?.warnings, !warnings.isEmpty {
                            ForEach(warnings) { warning in
                                Label(warning.detail, systemImage: "info.circle")
                                    .font(.caption)
                                    .foregroundStyle(Color.appWarning)
                                    .padding(.horizontal, 16)
                            }
                        }
                        if loading && page == nil {
                            ProgressView("Loading recommendations…")
                                .tint(Color.appAccent)
                                .frame(maxWidth: .infinity, minHeight: 240)
                        } else if let page {
                            ForEach(page.rows) { row in
                                rowSection(row)
                            }
                            if page.rows.isEmpty {
                                Text("No recommendations found.")
                                    .foregroundStyle(Color.appMuted)
                                    .padding(24)
                                    .frame(maxWidth: .infinity)
                            }
                        }
                    }
                    .padding(.vertical, 12)
                }
                .refreshable {
                    await reload()
                }
            }
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Discover")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $searchText, prompt: "Search anime, movies, or TV")
            .onSubmit(of: .search) {
                let trimmed = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !trimmed.isEmpty else { return }
                searching = true
                Task { await runSearch(trimmed) }
            }
            .onChange(of: searchText) { _, newValue in
                if newValue.isEmpty && searching {
                    searching = false
                    Task { await reload() }
                }
            }
            .task(id: category) {
                if !searching {
                    await reload()
                }
            }
        }
    }

    private func rowSection(_ row: LiveRecommendationsPage.Row) -> some View {
        let visibleCards = row.cards.filter { !dismissedKeys.contains($0.key) }
        return Group {
            if !visibleCards.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    Text(row.title)
                        .font(.title3.bold())
                        .padding(.horizontal, 16)
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(alignment: .top, spacing: 14) {
                            ForEach(visibleCards) { card in
                                let currentStatus = statusByID[card.key] ?? card.request
                                VStack(alignment: .leading, spacing: 8) {
                                    NavigationLink {
                                        PhoneCatalogDetailView(
                                            store: store,
                                            client: client,
                                            card: card,
                                            initialRequest: currentStatus,
                                            onStatusUpdated: { statusByID[card.key] = $0 },
                                            onDismissed: { dismissedKeys.insert(card.key) }
                                        )
                                    } label: {
                                        VStack(alignment: .leading, spacing: 8) {
                                            PhoneArtworkView(
                                                cacheKey: "rec:\(card.key)",
                                                source: .recommendation(client, card)
                                            )
                                            .frame(width: 140, height: 210)
                                            .clipShape(RoundedRectangle(cornerRadius: 12))
                                            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))

                                            Text(card.title)
                                                .font(.caption.weight(.semibold))
                                                .lineLimit(2)
                                                .foregroundStyle(Color.appText)
                                                .multilineTextAlignment(.leading)

                                            if let subtitle = cardSubtitle(card) {
                                                Text(subtitle)
                                                    .font(.caption2)
                                                    .foregroundStyle(Color.appMuted)
                                                    .lineLimit(1)
                                            }
                                        }
                                        .frame(width: 140, alignment: .leading)
                                    }
                                    .buttonStyle(.plain)

                                    if let status = currentStatus {
                                        Text(status.displayStatus)
                                            .font(.caption2)
                                            .foregroundStyle(Color.appAccent)
                                    } else if card.canRequest {
                                        Button {
                                            Task { await submitRequest(card) }
                                        } label: {
                                            Text(requestingID == card.key ? "Requesting…" : "Request")
                                                .font(.caption2.weight(.semibold))
                                                .frame(maxWidth: .infinity)
                                        }
                                        .buttonStyle(.bordered)
                                        .tint(Color.appAccent)
                                        .disabled(requestingID != nil)
                                    }
                                }
                                .frame(width: 140, alignment: .leading)
                            }
                        }
                        .padding(.horizontal, 16)
                    }
                }
            }
        }
    }

    private func cardSubtitle(_ card: LiveRecommendation) -> String? {
        var parts: [String] = []
        if let year = card.year {
            parts.append(String(year))
        }
        if let score = card.score {
            let formatted = score == floor(score) ? "\(Int(score))%" : String(format: "%.1f%%", score)
            parts.append(formatted)
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func reload(keepFeedback: Bool = false) async {
        loading = true
        if !keepFeedback {
            requestFeedback = nil
            statusByID = [:]
            dismissedKeys = []
        }
        defer { loading = false }
        do {
            page = try await client.recommendations(category)
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch is CancellationError {
            return
        } catch {
            requestFeedback = "Could not load Discover."
        }
    }

    private func runSearch(_ query: String) async {
        loading = true
        requestFeedback = nil
        statusByID = [:]
        dismissedKeys = []
        defer { loading = false }
        do {
            page = try await client.searchCatalog(query)
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch is CancellationError {
            return
        } catch {
            requestFeedback = "Search failed. Check your connection."
        }
    }

    private func submitRequest(_ card: LiveRecommendation) async {
        requestingID = card.key
        requestFeedback = nil
        defer { requestingID = nil }
        do {
            let result = try await client.requestTitle(card)
            statusByID[card.key] = result
            requestFeedback = "\(result.title): \(result.displayStatus)"
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            requestFeedback = "Could not submit request for \(card.title)."
        }
    }
}

struct PhoneRequestsView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient

    @State private var page: LiveRequestsPage?
    @State private var statusByID: [String: LiveTitleRequest] = [:]
    @State private var loading = false
    @State private var checkingID: String?
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage {
                    Text(errorMessage)
                        .foregroundStyle(Color.appWarning)
                }
                if loading && page == nil {
                    ProgressView("Loading Requests…")
                        .tint(Color.appAccent)
                } else if let page {
                    ForEach(page.categories.filter { !$0.requests.isEmpty }) { category in
                        Section(category.title) {
                            ForEach(category.requests) { item in
                                let current = statusByID[item.id] ?? item
                                NavigationLink {
                                    PhoneRequestDetailView(
                                        store: store,
                                        client: client,
                                        item: current
                                    ) { updated in
                                        statusByID[item.id] = updated
                                    }
                                } label: {
                                    requestRow(current)
                                }
                                .listRowBackground(Color.appSurface)
                                .swipeActions(edge: .trailing) {
                                    if current.canCheck == true {
                                        Button {
                                            Task { await check(current) }
                                        } label: {
                                            Label("Check", systemImage: "arrow.clockwise")
                                        }
                                        .tint(Color.appAccent)
                                    }
                                }
                            }
                        }
                    }
                    if page.categories.allSatisfy({ $0.requests.isEmpty }) {
                        Text("No active or past requests yet.")
                            .foregroundStyle(Color.appMuted)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .scrollIndicators(.hidden)
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Requests")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable {
                await loadRequests()
            }
            .task(id: store.selectedLibrary?.id) {
                await loadRequests()
            }
        }
    }

    private func requestRow(_ item: LiveTitleRequest) -> some View {
        HStack(alignment: .top, spacing: 12) {
            PhoneArtworkView(
                cacheKey: "req:\(item.id):\(item.poster ?? "")",
                source: (item.poster?.isEmpty == false) ? .request(client, item) : nil,
                fallbackIcon: "tray"
            )
            .frame(width: 56, height: 84)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.appKeyline, lineWidth: 1))

            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(item.title)
                        .font(.headline)
                        .lineLimit(2)
                    Spacer()
                    Text(checkingID == item.id ? "Checking…" : item.displayStatus)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.appAccent)
                }
                if !item.detail.isEmpty {
                    Text(item.detail)
                        .font(.caption)
                        .foregroundStyle(Color.appMuted)
                        .lineLimit(2)
                }
                if let stages = item.stages, !stages.isEmpty {
                    let doneCount = stages.filter { $0.state == "done" }.count
                    let noun = stages.count == 1 ? "stage" : "stages"
                    Text("\(doneCount) of \(stages.count) \(noun) complete")
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                }
                if let fallback = item.fallbackSearch {
                    Text(fallback.headline)
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func loadRequests() async {
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            page = try await client.requests()
            statusByID = [:]
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch is CancellationError {
            return
        } catch {
            errorMessage = "Could not load Requests."
        }
    }

    private func check(_ item: LiveTitleRequest) async {
        checkingID = item.id
        errorMessage = nil
        defer { checkingID = nil }
        do {
            let updated = try await client.checkRequest(item)
            statusByID[item.id] = updated
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            errorMessage = "Status check failed for \(item.title)."
        }
    }
}
