import QualificationCore
import SwiftUI

struct PhoneLibraryView: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient

    @State private var selectedKind: LiveLibraryKind = .anime
    @State private var selectedView: LiveLibraryViewFilter = .collection
    @State private var searchText = ""
    @State private var activeQuery = ""
    @State private var page: LiveLibraryPage?
    @State private var items: [LiveLibraryEntry] = []
    @State private var nextOffset: Int?
    @State private var loading = false
    @State private var errorMessage: String?

    private let columns = [GridItem(.adaptive(minimum: 148), spacing: 14, alignment: .top)]

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Library section", selection: $selectedKind) {
                    ForEach([LiveLibraryKind.anime, .movie, .tv]) { kind in
                        Text(kind.label).tag(kind)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.top, 10)
                .padding(.bottom, 6)

                Picker("Library status", selection: $selectedView) {
                    ForEach(LiveLibraryViewFilter.allCases) { view in
                        Text(view.label).tag(view)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.bottom, 10)

                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        if selectedView != .watched,
                           let continueList = page?.continueWatching,
                           !continueList.isEmpty,
                           activeQuery.isEmpty {
                            continueSection(continueList)
                        }
                        if let errorMessage {
                            Text(errorMessage)
                                .font(.subheadline)
                                .foregroundStyle(Color.appWarning)
                                .padding(.horizontal, 16)
                        }
                        if items.isEmpty && loading {
                            ProgressView("Loading \(selectedKind.label)…")
                                .tint(Color.appAccent)
                                .frame(maxWidth: .infinity, minHeight: 240)
                        } else if items.isEmpty {
                            Text(activeQuery.isEmpty ? "No titles in \(selectedKind.label) yet." : "No matching titles.")
                                .font(.subheadline)
                                .foregroundStyle(Color.appMuted)
                                .padding(24)
                                .frame(maxWidth: .infinity)
                        } else {
                            LazyVGrid(columns: columns, spacing: 16) {
                                ForEach(items) { entry in
                                    NavigationLink {
                                        PhoneTitleDetailView(store: store, client: client, kind: selectedKind, entry: entry)
                                    } label: {
                                        posterTile(entry)
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                            .padding(.horizontal, 16)

                            if let nextOffset {
                                Button {
                                    Task { await loadPage(offset: nextOffset, append: true) }
                                } label: {
                                    if loading {
                                        ProgressView().tint(Color.appAccent)
                                    } else {
                                        Text("Load more")
                                    }
                                }
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 12)
                                .buttonStyle(.bordered)
                                .tint(Color.appAccent)
                                .padding(.horizontal, 16)
                            }
                        }
                    }
                    .padding(.vertical, 12)
                }
                .refreshable {
                    await loadPage(offset: 0, append: false)
                }
            }
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Library")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $searchText, prompt: "Filter \(selectedKind.label.lowercased())")
            .onSubmit(of: .search) {
                activeQuery = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
                Task { await loadPage(offset: 0, append: false) }
            }
            .onChange(of: searchText) { _, newValue in
                if newValue.isEmpty && !activeQuery.isEmpty {
                    activeQuery = ""
                    Task { await loadPage(offset: 0, append: false) }
                }
            }
            .task(id: "\(selectedKind.rawValue):\(selectedView.rawValue):\(store.selectedLibrary?.id ?? "personal")") {
                activeQuery = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
                await loadPage(offset: 0, append: false)
            }
        }
    }

    private func continueSection(_ list: [LiveLibraryEntry]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Continue Watching")
                .font(.headline)
                .padding(.horizontal, 16)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 12) {
                    ForEach(list) { entry in
                        Button {
                            Task { await store.play(entry) }
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                PhoneArtworkView(
                                    cacheKey: "lib-cw:\(selectedKind.rawValue):\(entry.id)",
                                    source: .entry(client, entry, true)
                                )
                                .frame(width: 190, height: 107)
                                .clipShape(RoundedRectangle(cornerRadius: 10))
                                Text(entry.title)
                                    .font(.caption.weight(.semibold))
                                    .lineLimit(1)
                                    .foregroundStyle(Color.appText)
                                if !entry.note.isEmpty {
                                    Text(entry.note)
                                        .font(.caption2)
                                        .foregroundStyle(Color.appMuted)
                                        .lineLimit(1)
                                }
                            }
                            .frame(width: 190, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
            }
        }
    }

    private func posterTile(_ entry: LiveLibraryEntry) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            PhoneArtworkView(
                cacheKey: "lib-poster:\(selectedKind.rawValue):\(entry.id)",
                source: .entry(client, entry, false)
            )
            .aspectRatio(2 / 3, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))

            Text(entry.title)
                .font(.subheadline.weight(.semibold))
                .lineLimit(2)
                .foregroundStyle(Color.appText)
            HStack(spacing: 6) {
                if !entry.note.isEmpty {
                    Text(entry.note)
                        .lineLimit(1)
                }
                if let rating = entry.rating {
                    Label(String(format: "%.1f", rating), systemImage: "star.fill")
                        .foregroundStyle(Color.appAccent)
                }
            }
            .font(.caption)
            .foregroundStyle(Color.appMuted)
        }
    }

    private func loadPage(offset: Int, append: Bool) async {
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            let loaded = try await client.page(
                kind: selectedKind,
                title: nil,
                offset: offset,
                query: activeQuery,
                view: selectedView
            )
            page = loaded
            nextOffset = loaded.nextOffset
            if append {
                let existingIDs = Set(items.map(\.id))
                items.append(contentsOf: loaded.items.filter { !existingIDs.contains($0.id) })
            } else {
                items = loaded.items
            }
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
        } catch is CancellationError {
            return
        } catch {
            errorMessage = "Could not load \(selectedKind.label)."
        }
    }
}
