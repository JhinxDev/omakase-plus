import QualificationCore
import SwiftUI

struct PhoneDiscoverView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient

    @State private var category: LiveRecommendationCategory = .anime
    @State private var page: LiveRecommendationsPage?
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
        VStack(alignment: .leading, spacing: 12) {
            Text(row.title)
                .font(.title3.bold())
                .padding(.horizontal, 16)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 14) {
                    ForEach(row.cards) { card in
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

                            if let status = card.request {
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

    private func reload(keepFeedback: Bool = false) async {
        loading = true
        if !keepFeedback {
            requestFeedback = nil
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
            requestFeedback = "\(result.title): \(result.displayStatus)"
            await reload(keepFeedback: true)
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
                                requestRow(item)
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
                source: item.poster != nil ? .request(client, item) : nil,
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
                    Text(item.displayStatus)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Color.appAccent)
                }
                if !item.detail.isEmpty {
                    Text(item.detail)
                        .font(.caption)
                        .foregroundStyle(Color.appMuted)
                }
                if let stages = item.stages, !stages.isEmpty {
                    let doneCount = stages.filter { $0.state == "done" }.count
                    Text("\(doneCount) of \(stages.count) stages complete")
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                }
                if let fallback = item.fallbackSearch {
                    Text(fallback.headline)
                        .font(.caption2)
                        .foregroundStyle(Color.appMuted)
                }
                if item.canCheck == true {
                    Button {
                        Task { await check(item) }
                    } label: {
                        Text(checkingID == item.id ? "Checking…" : "Check status")
                            .font(.caption.weight(.semibold))
                    }
                    .buttonStyle(.bordered)
                    .tint(Color.appAccent)
                    .disabled(checkingID != nil)
                }
            }
        }
        .padding(.vertical, 4)
        .listRowBackground(Color.appSurface)
    }

    private func loadRequests() async {
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            page = try await client.requests()
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
            _ = try await client.checkRequest(item)
            await loadRequests()
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch {
            errorMessage = "Status check failed for \(item.title)."
        }
    }
}
