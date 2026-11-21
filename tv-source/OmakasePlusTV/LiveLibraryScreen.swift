import QualificationCore
import SwiftUI
import UIKit

@MainActor
struct LiveLibraryScreen: View {
    let client: LiveLibraryClient
    let isPresented: Bool
    let select: (LiveTestPass, LiveLibraryEntry, LiveLibraryEntry?, Bool) -> Void
    let disconnect: () -> Void
    let openProfiles: () -> Void
    let chooseLibrary: (LiveLibraryOption?) -> Void
    @Binding var returnSeries: LiveLibraryEntry?
    @Binding var topShelfDestination: TVTopShelfDestination?
    @State private var catalogPresentation: CatalogPresentation?
    @State private var homeOpen = true
    @State private var homeSupported = true
    @State private var homeRefresh = UUID()
    @State private var requestsOpen = false

    private var showingHome: Bool { homeOpen && homeSupported && client.selectedLibrary == nil }

    @Binding var kind: LiveLibraryKind
    @State private var title: LiveLibraryEntry?
    @State private var catalog: LiveLibraryPage?
    @State private var episodes: LiveLibraryPage?
    @State private var catalogOffset = 0
    @State private var episodeOffset = 0
    @State private var checkingEpisodes = false
    @State private var synopsisOpen = false
    @State private var detailsEntry: LiveLibraryEntry?
    @State private var detailsPayload: LiveLibraryDetails?
    @State private var detailsLoading = false
    @State private var detailsReturnKey: String?
    @State private var suppressEpisodePlay: String?
    @State private var searchOpen = false
    @State private var catalogSearchQuery: String?

    private struct CatalogPresentation: Identifiable {
        let query: String?
        var id: String { query ?? "" }
    }
    @State private var search = ""
    @State private var searchKeyboardRequest = false
    @State private var query = ""
    @State private var busy = false
    @State private var spin = false
    @State private var openingVideo = false
    @State private var expired = false
    @State private var accountToolsOpen = false
    @State private var accountPage: AccountPage?
    @State private var accountDefaultTool: AccountTool = .profiles
    @State private var libraryOptions: [LiveLibraryOption] = []
    @State private var libraryOptionsFailed = false
    @State private var confirmsDisconnect = false
    @Namespace private var libraryFocusScope
    @Environment(\.resetFocus) private var resetFocus
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var returnFocus: String?
    @State private var catalogReturnFocus: String?
    @State private var error: String?
    @State private var lastRefresh = Date.distantPast
    @AppStorage("omakase.tv.subtitles-always") private var subtitlesAlways = true
    @AppStorage("omakase.tv.audio-language") private var audioLanguage = "auto"
    @AppStorage("omakase.tv.subtitle-language") private var subtitleLanguage = "en"
    @FocusState private var focus: String?
    @FocusState private var accountToolFocus: AccountTool?
    @FocusState private var settingsFocus: SettingsAction?
    @FocusState private var libraryFocus: String?
    @FocusState private var searchFieldFocused: Bool

    private enum AccountPage: Equatable {
        case tools, libraries, settings, profiles
    }

    private enum AccountTool: String, Hashable {
        case profiles, libraries, settings, refresh, done
    }

    private enum SettingsAction: Hashable {
        case done, disconnect, cancel, confirmDisconnect, audio, subtitle
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            navigation.disabled(detailsEntry != nil)
            if let error {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(Color.tvWarning)
                    .padding(.horizontal, 88).padding(.vertical, 12)
                    .accessibilityIdentifier("library.error")
            }
            if !expired && showingHome {
                HomeScreen(client: client, focus: $focus, refreshID: homeRefresh,
                           onSelect: openHomeCard, browse: { changeCategory(kind) },
                           unavailable: {
                               homeSupported = false
                               changeCategory(kind)
                           }, expired: linkExpired)
                    .disabled(busy || detailsEntry != nil)
            } else if !expired {
                GeometryReader { viewport in
                    ScrollViewReader { scroll in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 30) {
                                if let title {
                                    titleDetail(title)
                                        .id("detail-start")
                                } else if let pending = returnSeries {
                                    titleDetail(pending)
                                        .id("detail-start")
                                } else {
                                    shelves(minimumCatalogHeight: max(0, viewport.size.height - 60))
                                        .id("catalog-start")
                                }
                                if busy && !openingVideo { ProgressView().frame(maxWidth: .infinity) }
                            }
                            .padding(.bottom, 50)
                        }
                        // Leave space for the focused artwork at both scrolling edges.
                        .safeAreaPadding(.vertical, 36)
                        .overlay(alignment: .top) {
                            LinearGradient(colors: [.tvBackground, .clear],
                                           startPoint: .top, endPoint: .bottom)
                                .frame(height: 36)
                                .allowsHitTesting(false)
                                .accessibilityHidden(true)
                        }
                        .overlay(alignment: .bottom) {
                            LinearGradient(colors: [.clear, .tvBackground],
                                           startPoint: .top, endPoint: .bottom)
                                .frame(height: 36)
                                .allowsHitTesting(false)
                                .accessibilityHidden(true)
                        }
                        .padding(.bottom, 60)
                        .id(title?.id ?? "catalog-\(kind.rawValue)-\(catalogOffset)-\(query)")
                        .onChange(of: focus) { previous, current in
                            guard let current else { return }
                            if title != nil {
                                if previous?.hasPrefix("episode-") == true && isNavigationFocus(current) {
                                    scrollToShelf("detail-start", using: scroll)
                                }
                                return
                            }
                            let previousShelf = shelfAnchor(for: previous)
                            if let section = shelfAnchor(for: current), section != previousShelf {
                                scrollToShelf(current == catalogReturnFocus ? current : section, using: scroll)
                            } else if previousShelf != nil && isNavigationFocus(current) {
                                // The toolbar can win focus when the hero is fully above the viewport.
                                scrollToShelf("catalog-start", using: scroll)
                            }
                        }
                        .onChange(of: episodeOffset) { _, _ in
                            guard title != nil, let episodes else { return }
                            scrollToShelf("episode-grid", using: scroll)
                            focus = episodes.items.first(where: { $0.watchPath != nil })
                                .map { "episode-\($0.id)" } ?? "details-secondary"
                        }
                        .task(id: title?.id) {
                            guard title == nil, let origin = catalogReturnFocus else { return }
                            await Task.yield()
                            guard !Task.isCancelled, title == nil else { return }
                            scroll.scrollTo(origin, anchor: .top)
                            await Task.yield()
                            guard !Task.isCancelled, title == nil else { return }
                            resetFocus(in: libraryFocusScope)
                            focus = origin
                        }
                    }
                }
                .disabled(detailsEntry != nil)
            }
        }
        .background(Color.tvBackground.ignoresSafeArea())
        .overlay {
            if isPresented && openingVideo {
                TVPlaybackLoading()
            }
        }
        .overlay {
            if let entry = detailsEntry {
                EpisodeDetailsOverlay(entry: entry, details: detailsPayload,
                                      loading: detailsLoading, client: client,
                                      canPlay: entry.watchPath != nil || entry.featuredPath != nil,
                                      play: { playFromEpisodeDetails(entry) },
                                      close: closeEpisodeDetails)
            }
        }
        .foregroundStyle(Color.tvText)
        .focusScope(libraryFocusScope)
        .defaultFocus($focus, returnFocus ?? (showingHome ? "home-entry" : kind.id))
        .task {
            async let options = try? client.libraries()
            if !showingHome { await load(0) }
            libraryOptions = await options ?? []
        }
        .onChange(of: isPresented) { _, visible in
            guard visible, !expired else { return }
            Task {
                if topShelfDestination != nil {
                    await followTopShelf()
                    return
                }
                let origin = returnFocus
                if showingHome {
                    if let origin { focus = origin }
                    homeRefresh = UUID()
                    return
                }
                if let origin, origin == kind.id {
                    // A Top Shelf link has no in-app source card to restore.
                    // Restore its category before background catalog work.
                    await Task.yield()
                    guard !expired, isPresented, returnFocus == origin, topShelfDestination == nil else { return }
                    resetFocus(in: libraryFocusScope)
                    focus = origin
                    await refresh(preservingContents: true)
                    return
                }
                await refresh(preservingContents: true)
                guard !expired, let origin, returnFocus == origin, topShelfDestination == nil else { return }
                focus = nil
                await Task.yield()
                guard returnFocus == origin, topShelfDestination == nil else { return }
                resetFocus(in: libraryFocusScope)
                focus = origin
            }
        }
        .onChange(of: topShelfDestination?.id, initial: true) { _, destination in
            if destination != nil { Task { await followTopShelf() } }
        }
        .onChange(of: busy) { _, loading in
            if !loading && topShelfDestination != nil { Task { await followTopShelf() } }
        }
        .onChange(of: returnSeries?.id) { _, _ in adoptReturnSeries() }
        .task { adoptReturnSeries() }
        .onExitCommand {
            if detailsEntry != nil {
                closeEpisodeDetails()
                return
            }
            guard isPresented, !accountToolsOpen, !searchOpen, !synopsisOpen,
                  title != nil, !busy else { return }
            back()
        }
        .fullScreenCover(isPresented: $accountToolsOpen, onDismiss: dismissAccountTools) {
            switch accountPage {
            case .libraries:
                libraryPicker
            case .settings:
                deviceSettings
            case .tools, .profiles, nil:
                accountTools
            }
        }
        .fullScreenCover(item: $catalogPresentation, onDismiss: {
            focus = showingHome && returnFocus?.hasPrefix("home-card-") == true
                ? returnFocus : (catalogSearchQuery == nil ? "for-you" : "search")
            catalogSearchQuery = nil
        }) { presentation in
            TVForYouScreen(client: client.selecting(nil), query: presentation.query) {
                catalogPresentation = nil
            }
        }
        .fullScreenCover(isPresented: $requestsOpen, onDismiss: { focus = "requests" }) {
            TVRequestsScreen(client: client.selecting(nil)) { requestsOpen = false }
        }
        .sheet(isPresented: $searchOpen, onDismiss: {
            if let catalogSearchQuery {
                catalogPresentation = CatalogPresentation(query: catalogSearchQuery)
            } else {
                focus = "search"
            }
        }) { searchEditor }
        .fullScreenCover(isPresented: $synopsisOpen, onDismiss: { focus = "details-secondary" }) {
            if let title, let synopsis = synopsis(for: title) {
                TVSynopsisScreen(title: title.title, synopsis: synopsis) { synopsisOpen = false }
            }
        }
    }

    private var navigation: some View {
        HStack(spacing: 12) {
            TVBrandMark(size: 36)
                .fixedSize()
                .padding(.trailing, 14)
            if homeSupported && client.selectedLibrary == nil {
                navigationButton("Home", key: "home", selected: showingHome, action: goHome)
                    .disabled(expired || busy)
            }
            ForEach(LiveLibraryKind.allCases.filter { $0 != .cloud }) { tab in
                let selected = !showingHome && tab == kind
                navigationButton(tab.label, key: tab.id, selected: selected) {
                    changeCategory(tab)
                }
                // A passive refresh must not disable a Top Shelf return target.
                .disabled(expired || (busy && returnFocus != tab.id))
            }
            navigationButton("For You", key: "for-you") {
                catalogSearchQuery = nil
                catalogPresentation = CatalogPresentation(query: nil)
            }
            .disabled(expired || busy)
            if client.selectedLibrary == nil {
                navigationButton("Requests", key: "requests") { requestsOpen = true }
                    .accessibilityIdentifier("library.requests")
                    .disabled(expired || busy)
            }
            Spacer(minLength: 20)
            navigationButton("Search", key: "search", icon: "magnifyingglass",
                             selected: !query.isEmpty) {
                search = query
                searchOpen = true
            }
            .accessibilityValue(query.isEmpty ? "" : "Filtering for \(query)")
            .accessibilityIdentifier("library.search")
            .disabled(expired || busy)
            navigationButton("Account & tools", key: "account-tools", icon: "person.crop.circle") {
                accountDefaultTool = .profiles
                accountToolFocus = nil
                accountPage = .tools
                accountToolsOpen = true
            }
            .accessibilityValue(spin ? "Refreshing library" : "")
            .accessibilityIdentifier("library.account-tools")
            .disabled(busy && !spin)
        }
        .padding(.horizontal, 88)
        .padding(.top, 24)
        .padding(.bottom, 18)
        .focusSection()
    }

    private func navigationButton(_ label: String, key: String, icon: String? = nil,
                                  selected: Bool = false, action: @escaping () -> Void) -> some View {
        let focused = focus == key
        return Button(action: action) {
            Group {
                if let icon {
                    Image(systemName: icon)
                        .font(.system(size: 29, weight: .medium))
                } else {
                    Text(label)
                        .font(.callout.weight(.semibold))
                }
            }
            .foregroundStyle(focused ? Color.tvText : selected ? Color.tvAccent : Color.tvText)
            .padding(.horizontal, 16)
            .frame(minWidth: 64, minHeight: 64)
            .background(focused ? Color.tvFocusSurface : .clear, in: .rect(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(focused ? Color.tvFocusBorder : .clear, lineWidth: 3)
                    .allowsHitTesting(false)
            }
            .overlay(alignment: .bottom) {
                if selected && !focused {
                    Capsule().fill(Color.tvAccent).frame(height: 3).padding(.horizontal, 16)
                }
            }
            .contentShape(Rectangle())
            .scaleEffect(focused && !reduceMotion ? 1.045 : 1)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: focused)
        }
        .buttonStyle(TVBareButtonStyle())
        .fixedSize()
        .focusEffectDisabled()
        .focused($focus, equals: key)
        .accessibilityLabel(label)
        .accessibilityIdentifier("nav-\(key)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var accountTools: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("Account & tools")
                .font(.largeTitle.bold())
                .accessibilityAddTraits(.isHeader)
            accountButton("Profiles", icon: "person.crop.circle", key: .profiles) {
                accountPage = .profiles
                accountToolsOpen = false
            }
            if !expired && (libraryOptions.count > 1 || client.selectedLibrary != nil) {
                accountButton("Switch library", icon: "rectangle.stack", key: .libraries) {
                    accountDefaultTool = .libraries
                    accountPage = .libraries
                }
                .accessibilityValue(client.selectedLibrary?.name ?? "Personal library")
            }
            accountButton(expired ? "Reconnect" : "Settings",
                          icon: expired ? "link" : "gearshape", key: .settings) {
                if expired {
                    Task { await unlink() }
                } else {
                    accountDefaultTool = .settings
                    accountPage = .settings
                }
            }
            if !expired {
                accountButton("Refresh library", icon: "arrow.clockwise", key: .refresh) {
                    if showingHome {
                        homeRefresh = UUID()
                    } else {
                        Task { await refresh(preservingContents: true) }
                    }
                    accountToolsOpen = false
                }
                .disabled(spin)
            }
            if expired, let error {
                Text(error).foregroundStyle(Color.tvWarning)
            }
            accountButton("Done", icon: "xmark", key: .done) { accountToolsOpen = false }
        }
        .padding(70)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
        .background(Color.tvBackground.ignoresSafeArea())
        .foregroundStyle(Color.tvText)
        .defaultFocus($accountToolFocus, accountDefaultTool)
        .disabled(busy)
        .onExitCommand {
            guard !busy else { return }
            accountToolsOpen = false
        }
    }

    private func accountButton(_ label: String, icon: String, key: AccountTool,
                               action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(label, systemImage: icon)
                .frame(width: 380, alignment: .leading)
        }
        .buttonStyle(TVActionStyle(focused: accountToolFocus == key, compact: true))
        .focusEffectDisabled()
        .focused($accountToolFocus, equals: key)
        .accessibilityLabel(label)
        .accessibilityIdentifier("account-tools.\(key.rawValue)")
    }

    private func dismissAccountTools() {
        let destination = accountPage
        accountPage = nil
        confirmsDisconnect = false
        guard destination != nil, isPresented else { return }
        if destination == .profiles {
            openProfiles()
        } else {
            focus = "account-tools"
        }
    }

    private var searchTerm: String {
        String(search.trimmingCharacters(in: .whitespacesAndNewlines).prefix(100))
    }

    private var searchEditor: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text("Search titles")
                .font(.largeTitle.bold())
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("library.search.editor")
            Button { searchKeyboardRequest.toggle() } label: {
                LibrarySearchPrompt(text: search)
            }
            .buttonStyle(TVLibraryTileStyle())
            .focusEffectDisabled()
            .focused($searchFieldFocused)
            .accessibilityLabel("Title")
            .accessibilityValue(search)
            .accessibilityHint("Opens the Apple TV keyboard")
            .accessibilityIdentifier("library.search.field")
            .background {
                TVSystemKeyboard(text: $search, requested: searchKeyboardRequest, prompt: "Title", returnKey: .search) {
                    searchCatalog()
                }
                .frame(width: 1, height: 1)
                .clipped()
                .accessibilityHidden(true)
            }
            HStack(spacing: 20) {
                Button("Search catalog") { searchCatalog() }
                    .buttonStyle(TVActionStyle(tint: .tvAccent))
                    .focusEffectDisabled()
                    .disabled(searchTerm.count < 2)
                Button("Search library") { applySearch(searchTerm) }
                    .buttonStyle(TVActionStyle())
                    .focusEffectDisabled()
                    .disabled(searchTerm.isEmpty)
                if !query.isEmpty {
                    Button("Clear filter") { applySearch("") }
                        .buttonStyle(TVActionStyle())
                        .focusEffectDisabled()
                }
                Button("Cancel") { searchOpen = false }
                    .buttonStyle(TVActionStyle())
                    .focusEffectDisabled()
            }
        }
        .padding(70)
        .frame(width: 880, alignment: .leading)
        .background(Color.tvBackground.ignoresSafeArea())
        .foregroundStyle(Color.tvText)
        .defaultFocus($searchFieldFocused, true)
    }

    private var libraryPicker: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("Choose a library")
                .font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            Text("Your viewing progress follows the current profile.")
                .foregroundStyle(Color.tvMuted)
            ForEach(libraryOptions) { option in
                let current = client.selectedLibrary?.id == option.id ||
                    (client.selectedLibrary == nil && option.kind == .personal)
                Button {
                    if current {
                        accountPage = .tools
                    } else {
                        accountToolsOpen = false
                        chooseLibrary(option)
                    }
                } label: {
                    Label(option.name, systemImage: current ? "checkmark.circle.fill" : "rectangle.stack")
                }
                .buttonStyle(TVActionStyle(focused: libraryFocus == option.id))
                .focusEffectDisabled()
                .focused($libraryFocus, equals: option.id)
                .accessibilityAddTraits(current ? .isSelected : [])
            }
            if client.selectedLibrary != nil && !libraryOptions.contains(where: { $0.kind == .personal }) {
                Button("Personal library") {
                    accountToolsOpen = false
                    chooseLibrary(nil)
                }
                .buttonStyle(TVActionStyle(focused: libraryFocus == "personal"))
                .focusEffectDisabled()
                .focused($libraryFocus, equals: "personal")
            }
            if libraryOptionsFailed {
                Text("Couldn’t refresh available libraries. Try again after checking the connection.")
                    .font(.callout).foregroundStyle(Color.tvWarning)
            }
            Button("Done") { accountPage = .tools }
                .buttonStyle(TVActionStyle(focused: libraryFocus == "done"))
                .focusEffectDisabled()
                .focused($libraryFocus, equals: "done")
        }
        .padding(70)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.tvBackground.ignoresSafeArea())
        .foregroundStyle(Color.tvText)
        .onExitCommand { accountPage = .tools }
        .defaultFocus($libraryFocus,
                      libraryOptions.first { $0.id == client.selectedLibrary?.id }?.id ??
                      libraryOptions.first?.id ?? (client.selectedLibrary == nil ? "done" : "personal"))
        .task {
            libraryOptionsFailed = false
            do {
                libraryOptions = try await client.libraries()
            } catch is CancellationError {
                return
            } catch {
                libraryOptionsFailed = true
            }
        }
    }

    private var deviceSettings: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 16) {
                TVBrandMark(size: 32)
                Text("Omakase Plus").font(.headline).foregroundStyle(Color.tvAccent)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Omakase Plus")
            Text(confirmsDisconnect ? "Disconnect this profile?" : "This Apple TV")
                .font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            Text(confirmsDisconnect
                 ? "Link this profile again to watch its library on this TV."
                 : "Manage this profile’s connection in Devices on the web.")
                .foregroundStyle(Color.tvMuted)
            if !confirmsDisconnect {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Sources are managed on the web", systemImage: "link.badge.plus")
                        .font(.headline)
                        .foregroundStyle(Color.tvAccent)
                    Text("Open Omakase Plus in a browser, then Settings → Connections to review Real-Debrid and Plex. Cloud files live there instead of a TV tab.")
                        .foregroundStyle(Color.tvMuted)
                }
                .frame(maxWidth: 980, alignment: .leading)
            }
            if let error { Text(error).foregroundStyle(Color.tvWarning) }
            Text("Playback")
                .font(.headline).foregroundStyle(Color.tvAccent)
                .accessibilityAddTraits(.isHeader)
            Button {
                subtitlesAlways.toggle()
            } label: {
                Label(
                    subtitlesAlways ? "Subtitles: Always on" : "Subtitles: Only when chosen",
                    systemImage: subtitlesAlways ? "captions.bubble.fill" : "captions.bubble"
                )
            }
            .buttonStyle(TVActionStyle(focused: settingsFocus == .subtitle, compact: true, selected: subtitlesAlways))
            .focusEffectDisabled()
            .focused($settingsFocus, equals: .subtitle)
            .accessibilityIdentifier("device.settings.subtitles")
            .accessibilityValue(subtitlesAlways ? "On" : "Off")
            .accessibilityHint("Turns subtitles on automatically for every video that includes text subtitles")
            Text("About")
                .font(.headline).foregroundStyle(Color.tvAccent)
                .accessibilityAddTraits(.isHeader)
            Text(versionLabel)
                .font(.callout.monospacedDigit())
                .foregroundStyle(Color.tvMuted)
                .accessibilityIdentifier("device.settings.version")
            HStack(spacing: 24) {
                if confirmsDisconnect {
                    settingsButton("Cancel", key: .cancel) { confirmsDisconnect = false }
                    settingsButton(busy ? "Disconnecting…" : "Disconnect", key: .confirmDisconnect,
                                   role: .destructive) {
                        Task { await unlink() }
                    }
                } else {
                    settingsButton("Done", key: .done) { accountPage = .tools }
                    settingsButton("Disconnect this profile", key: .disconnect, role: .destructive) {
                        confirmsDisconnect = true
                    }
                }
            }
            .focusSection()
            .disabled(busy)
        }
        .padding(70)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.tvBackground.ignoresSafeArea())
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("device.settings.screen")
        .defaultFocus($settingsFocus, confirmsDisconnect ? .cancel : .done)
        .onChange(of: confirmsDisconnect) { _, confirming in
            settingsFocus = confirming ? .cancel : .done
        }
        .onExitCommand {
            guard !busy else { return }
            if confirmsDisconnect {
                confirmsDisconnect = false
            } else {
                accountPage = .tools
            }
        }
    }

    private func settingsButton(_ title: String, key: SettingsAction, role: ButtonRole? = nil,
                                action: @escaping () -> Void) -> some View {
        Button(title, role: role, action: action)
            .buttonStyle(TVActionStyle(focused: settingsFocus == key, tint: role == .destructive ? .red : .tvText))
            .focusEffectDisabled()
            .focused($settingsFocus, equals: key)
    }

    private func shelves(minimumCatalogHeight: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 36) {
            if let featured = catalog?.featured, query.isEmpty, catalogOffset == 0 {
                TVCinemaFeature(entry: featured, client: client,
                                label: catalog?.featuredRecent == true ? "Last played" : "") {
                    HStack(spacing: 18) {
                        if featured.featuredPath != nil || featured.watchPath != nil {
                            control(featuredAction(featured), key: "feature-play", icon: "play.fill", accent: true) {
                                Task { await open(featured, origin: "feature-play", featured: true) }
                            }
                            .disabled(busy)
                        }
                        control("More info", key: "feature-details", icon: "info.circle") {
                            show(featured, origin: "feature-details")
                        }
                        .disabled(busy)
                        .id("feature-details")
                    }
                }
                .padding(.horizontal, 88)
                .id("featured-shelf")
            }
            if let watching = catalog?.continueWatching, !watching.isEmpty, query.isEmpty, catalogOffset == 0 {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Continue watching").font(.title2.bold()).accessibilityAddTraits(.isHeader)
                        .padding(.horizontal, 88)
                    ScrollView(.horizontal) {
                        LazyHStack(alignment: .top, spacing: 24) {
                            ForEach(watching) { entry in
                                poster(entry, namespace: "continue", wide: true)
                                    .frame(width: 308)
                            }
                        }
                        .padding(.horizontal, 88)
                        .padding(.vertical, 16)
                    }
                    .scrollIndicators(.hidden)
                }
                .focusSection()
                .id("continue-shelf")
            }
            if let catalog {
                VStack(alignment: .leading, spacing: 20) {
                    Text(query.isEmpty ? "All \(kind.label)" : "Search results")
                        .font(.title2.bold()).accessibilityAddTraits(.isHeader)
                    if !query.isEmpty {
                        Text(query).foregroundStyle(Color.tvMuted).lineLimit(2)
                    }
                    if catalog.items.isEmpty {
                        Text(query.isEmpty ? "This section is empty." : "No titles match your search.")
                            .foregroundStyle(.secondary)
                            .padding(.vertical, 12)
                    } else if kind == .cloud {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            ForEach(catalog.items) { entry in cloudRow(entry, namespace: "catalog") }
                        }
                        pagination(offset: catalogOffset, next: catalog.nextOffset)
                    } else {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 205), spacing: 24)],
                                  alignment: .leading, spacing: 28) {
                            ForEach(catalog.items) { entry in
                                poster(entry, namespace: "catalog", wide: false)
                            }
                        }
                        pagination(offset: catalogOffset, next: catalog.nextOffset)
                    }
                }
                // A sparse last shelf still needs enough scroll extent to leave
                // the complete hero above the viewport, just like a long catalog.
                .frame(minHeight: minimumCatalogHeight, alignment: .topLeading)
                .padding(.horizontal, 88)
                .focusSection()
                .id("catalog-shelf")
            }
        }
    }

    @ViewBuilder
    private func titleDetail(_ entry: LiveLibraryEntry) -> some View {
        if kind == .cloud {
            VStack(alignment: .leading, spacing: 20) {
                control("Back to Cloud", key: "details-secondary", icon: "arrow.left", action: back)
                Text(entry.title).font(.title.bold()).accessibilityAddTraits(.isHeader)
                if let episodes {
                    if episodes.items.isEmpty { Text("No files available").foregroundStyle(Color.tvMuted) }
                    ForEach(episodes.items) { file in cloudRow(file, namespace: "episode") }
                    pagination(offset: episodeOffset, next: episodes.nextOffset)
                } else if busy {
                    ProgressView("Loading files…")
                }
            }
            .padding(.horizontal, 88)
        } else {
            standardTitleDetail(entry)
        }
    }

    private func cloudRow(_ entry: LiveLibraryEntry, namespace: String) -> some View {
        let key = "\(namespace)-\(entry.id)"
        let focused = focus == key
        return Button {
            if entry.isFolder == true {
                show(entry, origin: key)
            } else {
                Task { await open(entry, origin: key, resumeImmediately: true) }
            }
        } label: {
            HStack(spacing: 18) {
                Image(systemName: entry.isFolder == true ? "folder" : "film")
                    .frame(width: 44)
                VStack(alignment: .leading, spacing: 6) {
                    Text(entry.title).font(.callout.weight(.semibold)).lineLimit(2)
                    Text(entry.note).font(.caption)
                }
                Spacer(minLength: 0)
            }
            .padding(16)
            .foregroundStyle(focused ? Color.tvFocus : Color.tvText)
        }
        .buttonStyle(TVLibraryTileStyle(focused: focused))
        .focusEffectDisabled()
        .focused($focus, equals: key)
        .id(key)
        .accessibilityIdentifier(key)
        .disabled(busy || (entry.isFolder != true && entry.watchPath == nil))
    }

    private func standardTitleDetail(_ entry: LiveLibraryEntry) -> some View {
        VStack(alignment: .leading, spacing: 30) {
            TVCinemaFeature(entry: entry, client: client, label: kind.label) {
                HStack(spacing: 18) {
                    if entry.featuredPath != nil || entry.watchPath != nil {
                        control(featuredAction(entry), key: "detail-play", icon: "play.fill", accent: true) {
                            Task {
                                await open(entry, origin: "detail-play",
                                           featured: entry.featuredPath != nil,
                                           resumeImmediately: true)
                            }
                        }
                        .disabled(busy)
                    }
                    if synopsis(for: entry) != nil {
                        control(kind == .movie ? "Movie details" : "About this series", key: "details-secondary", icon: "text.alignleft") {
                            synopsisOpen = true
                        }
                        .disabled(busy)
                    } else {
                        control("Back to \(kind.label)", key: "details-secondary", icon: "arrow.left", action: back)
                            .disabled(busy)
                    }
                }
            }
            if kind != .movie, episodes != nil { episodeGrid.id("episode-grid") }
        }
        .padding(.horizontal, 88)
    }

    private let languages = ["auto", "en", "fr", "es", "de", "it", "pt", "ja"]
    private let languageNames = [
        "auto": "Automatic", "en": "English", "fr": "Français", "es": "Español",
        "de": "Deutsch", "it": "Italiano", "pt": "Português", "ja": "日本語",
    ]

    private func languageLabel(_ code: String) -> String {
        languageNames[code] ?? code.uppercased()
    }

    private func nextLanguage(_ code: String) -> String {
        let current = languages.firstIndex(of: code) ?? 0
        return languages[(current + 1) % languages.count]
    }

    private var versionLabel: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown"
        return "Version \(version) (\(build))"
    }

    private func synopsis(for entry: LiveLibraryEntry) -> String? {
        guard let text = episodes?.titleSummary ?? entry.summary,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return text
    }

    private var episodeGrid: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Episodes").font(.title2.bold()).accessibilityAddTraits(.isHeader)
            if episodes?.hasUncheckedEpisodes == true {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Some episodes have not been checked yet.")
                        .font(.callout).foregroundStyle(Color.tvWarning)
                    control(checkingEpisodes ? "Checking…" : "Check again", key: "episode-recheck", icon: "arrow.clockwise", compact: true) {
                        Task { await recheckEpisodes() }
                    }
                    .accessibilityHint("Check the current episode page for available files")
                    .accessibilityIdentifier("library.episode-recheck")
                }
                .padding(.vertical, 12)
            }
            if let episodes, !episodes.items.isEmpty {
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 26, alignment: .topLeading),
                                            count: 4), alignment: .leading, spacing: 30) {
                    ForEach(episodes.items) { episode in episodeCard(episode) }
                }
                pagination(offset: episodeOffset, next: episodes.nextOffset)
            } else if !busy {
                Text(episodes?.hasUncheckedEpisodes == true ? "Episode availability is still being checked." : "No episodes are ready to play.")
                    .foregroundStyle(Color.tvMuted)
            }
        }
        .focusSection()
    }

    private func recheckEpisodes() async {
        guard !busy, !checkingEpisodes, let selectedTitle = title else { return }
        let selectedKind = kind
        checkingEpisodes = true
        defer { checkingEpisodes = false }
        await load(episodeOffset)
        guard !expired, isPresented, title?.id == selectedTitle.id, kind == selectedKind else { return }
        await Task.yield()
        if episodes?.hasUncheckedEpisodes == true {
            focus = "episode-recheck"
        } else if let firstPlayable = episodes?.items.first(where: { $0.watchPath != nil }) {
            focus = "episode-\(firstPlayable.id)"
        } else {
            focus = "details-secondary"
        }
    }

    private func scrollToShelf(_ id: String, using scroll: ScrollViewProxy) {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.18)) {
            scroll.scrollTo(id, anchor: .top)
        }
    }

    private func isNavigationFocus(_ key: String) -> Bool {
        LiveLibraryKind.allCases.contains { $0.id == key } ||
            ["home", "for-you", "requests", "search", "account-tools"].contains(key)
    }

    private func control(_ label: String, key: String, icon: String? = nil, accent: Bool = false,
                         compact: Bool = false, selected: Bool = false,
                         action: @escaping () -> Void) -> some View {
        Button(action: action) {
            if let icon { Label(label, systemImage: icon) }
            else { Text(label) }
        }
        .buttonStyle(TVActionStyle(focused: focus == key, tint: accent ? .tvAccent : .tvText,
                                   compact: compact, selected: selected))
        .fixedSize(horizontal: compact, vertical: false)
        .focusEffectDisabled()
        .focused($focus, equals: key)
        .accessibilityLabel(label)
    }

    private func shelfAnchor(for key: String?) -> String? {
        guard let key else { return nil }
        if key.hasPrefix("feature-") { return "featured-shelf" }
        if key.hasPrefix("continue-") { return "continue-shelf" }
        if key.hasPrefix("catalog-") || key == "previous" || key == "next" { return "catalog-shelf" }
        return nil
    }

    private func poster(_ entry: LiveLibraryEntry, namespace: String, wide: Bool) -> some View {
        let key = "\(namespace)-\(entry.id)"
        let focused = focus == key
        let opensVideo = namespace == "continue" && (entry.featuredPath != nil || entry.watchPath != nil)
        return Button {
            if suppressEpisodePlay == key {
                suppressEpisodePlay = nil
                return
            }
            if opensVideo {
                Task { await open(entry, origin: key, featured: true, resumeImmediately: true) }
            } else {
                show(entry, origin: key)
            }
        } label: {
            VStack(alignment: .leading, spacing: 8) {
                TVLibraryArtwork(entry: entry, client: client, banner: wide)
                    .aspectRatio(wide ? 16.0 / 9 : 2.0 / 3, contentMode: .fit)
                    .clipShape(.rect(cornerRadius: 10))
                    .overlay {
                        RoundedRectangle(cornerRadius: 10)
                            .strokeBorder(Color.tvKeyline, lineWidth: 1)
                    }
                Text(entry.title).font(.callout.weight(.semibold)).lineLimit(2, reservesSpace: true)
                    .foregroundStyle(focused ? Color.tvFocus : Color.tvText)
                Text(entry.note).font(.caption).foregroundStyle(focused ? Color.tvText : Color.tvMuted).lineLimit(1)
                if wide, let progress = entry.progress, (0...1).contains(progress) {
                    ProgressView(value: progress).tint(Color.tvFocus)
                        .accessibilityLabel("Watched")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(TVLibraryTileStyle(focused: focused, longPress: {
            guard !busy else { return }
            openDetails(entry, key: key)
        }))
        .focusEffectDisabled()
        .focused($focus, equals: key)
        .zIndex(focused ? 1 : 0)
        .accessibilityIdentifier(key)
        .id(key)
        .accessibilityLabel(entry.title)
        .accessibilityHint(entry.note)
        .accessibilityAction(named: "Details") {
            guard !busy else { return }
            openDetails(entry, key: key)
        }
        .disabled(busy)
    }

    private func episodeCard(_ entry: LiveLibraryEntry) -> some View {
        let key = "episode-\(entry.id)"
        let focused = focus == key
        let status = entry.watched == true ? "Watched" : entry.progress != nil ? "In progress" : nil
        return Button {
            if suppressEpisodePlay == key {
                suppressEpisodePlay = nil
                return
            }
            Task { await open(entry, origin: key, resumeImmediately: true) }
        } label: {
            VStack(alignment: .leading, spacing: 10) {
                TVArtworkPlaceholder(compact: true)
                    .aspectRatio(16.0 / 9, contentMode: .fit)
                    .overlay {
                        TVLibraryArtwork(entry: entry, client: client, banner: true, showsPlaceholder: false)
                    }
                    .overlay(alignment: .bottomTrailing) {
                        if let status {
                            Text(status)
                                .font(.caption2.weight(.semibold))
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(.black.opacity(0.84), in: .rect(cornerRadius: 6))
                                .padding(10)
                        }
                    }
                    .clipShape(.rect(cornerRadius: 10))
                    .overlay {
                        RoundedRectangle(cornerRadius: 10)
                            .strokeBorder(focused ? Color.tvFocus : Color.tvKeyline,
                                          lineWidth: focused ? 5 : 1)
                    }
                if entry.note != entry.title {
                    Text(entry.note).font(.caption.monospacedDigit()).foregroundStyle(Color.tvMuted)
                }
                Text(entry.episodeDisplayTitle).font(.callout.weight(.semibold)).lineLimit(2, reservesSpace: true)
                    .foregroundStyle(Color.tvText)
                if let runtime = entry.durationLabel {
                    Text(runtime).font(.caption.monospacedDigit()).foregroundStyle(Color.tvMuted)
                }
                if let summary = entry.summary, !summary.isEmpty {
                    Text(summary).font(.caption).foregroundStyle(Color.tvMuted).lineLimit(2, reservesSpace: true)
                }
                if let progress = entry.progress, entry.watched != true, (0...1).contains(progress) {
                    ProgressView(value: progress).tint(Color.tvFocus)
                        .accessibilityLabel("Episode progress")
                }
                if let availability = entry.unavailableLabel {
                    Text(availability).font(.caption)
                        .foregroundStyle(entry.availabilityPending ? Color.tvWarning : Color.tvMuted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(focused ? Color.tvFocusSurface : Color.tvSurface, in: .rect(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(focused ? Color.tvFocusBorder : Color.tvKeyline, lineWidth: focused ? 3 : 1)
                    .allowsHitTesting(false)
            }
        }
        .buttonStyle(TVLibraryTileStyle(focused: focused, longPress: {
            guard !busy, entry.watchPath != nil else { return }
            openDetails(entry, key: key)
        }))
        .focusEffectDisabled()
        .focused($focus, equals: key)
        .zIndex(focused ? 1 : 0)
        .accessibilityIdentifier(key)
        .accessibilityLabel([entry.note, entry.episodeDisplayTitle, entry.durationLabel, status, entry.unavailableLabel, entry.summary]
            .compactMap { $0 }.joined(separator: ", "))
        .accessibilityAction(named: "Episode details") {
            guard !busy, entry.watchPath != nil else { return }
            openDetails(entry, key: key)
        }
        .disabled(busy || entry.watchPath == nil)
    }

    private func pagination(offset: Int, next: Int?) -> some View {
        HStack(spacing: 16) {
            if offset > 0 {
                control("Previous page", key: "previous", icon: "chevron.left") {
                    Task { await load(offset - 30) }
                }
            }
            if let next {
                control("Next page", key: "next", icon: "chevron.right") {
                    Task { await load(next) }
                }
            }
        }
        .disabled(busy)
    }

    private func featuredAction(_ entry: LiveLibraryEntry) -> String {
        let resume = entry.progress.map { $0 > 0 } ?? false
        if let number = entry.featuredEpisode, number > 0 {
            return "\(resume ? "Resume" : "Play") episode \(number)"
        }
        return resume ? "Resume" : "Play"
    }

    private func searchCatalog() {
        guard searchTerm.count >= 2 else { return }
        catalogSearchQuery = searchTerm
        searchOpen = false
    }

    private func applySearch(_ term: String) {
        guard !busy else { return }
        searchOpen = false
        returnFocus = nil
        catalogReturnFocus = nil
        homeOpen = false
        title = nil
        episodes = nil
        query = term
        catalog = nil
        Task {
            await load(0)
            if !expired { focus = "search" }
        }
    }

    private func goHome() {
        guard !showingHome else { return }
        returnFocus = nil
        catalogReturnFocus = nil
        title = nil
        episodes = nil
        error = nil
        focus = "home-entry"
        homeOpen = true
    }

    private func openHomeCard(_ card: HomeCard, key: String) {
        guard !busy else { return }
        returnFocus = key
        if card.entry.watchPath != nil {
            kind = card.domain
            Task { await open(card.entry, origin: key, resumeImmediately: true) }
        } else if card.id.hasPrefix("watchlist-") {
            catalogSearchQuery = card.title
            catalogPresentation = CatalogPresentation(query: card.title)
        } else {
            openDetails(card.entry, key: key)
        }
    }

    private func changeCategory(_ tab: LiveLibraryKind) {
        guard !busy else { return }
        homeOpen = false
        returnFocus = nil
        catalogReturnFocus = nil
        title = nil
        kind = tab
        search = ""
        query = ""
        searchOpen = false
        catalog = nil
        Task { await load(0) }
    }

    private func openDetails(_ entry: LiveLibraryEntry, key: String) {
        suppressEpisodePlay = key
        detailsReturnKey = key
        detailsEntry = entry
        detailsPayload = nil
        guard let path = entry.watchPath ?? entry.featuredPath else { detailsLoading = false; return }
        detailsLoading = true
        Task {
            let details = try? await client.details(path: path)
            guard !Task.isCancelled, detailsEntry?.id == entry.id else { return }
            detailsPayload = details
            detailsLoading = false
        }
    }

    private func playFromEpisodeDetails(_ entry: LiveLibraryEntry) {
        let key = detailsReturnKey
        closeEpisodeDetails()
        returnFocus = key
        Task {
            if entry.watchPath != nil {
                await open(entry, origin: key ?? "episode-details", resumeImmediately: true)
            } else if entry.featuredPath != nil {
                await open(entry, origin: key ?? "episode-details", featured: true,
                           resumeImmediately: true)
            }
        }
    }

    private func closeEpisodeDetails() {
        let key = detailsReturnKey
        detailsEntry = nil
        detailsPayload = nil
        detailsLoading = false
        detailsReturnKey = nil
        suppressEpisodePlay = nil
        Task { @MainActor in
            await Task.yield()
            resetFocus(in: libraryFocusScope)
            if let key { focus = key }
        }
    }

    private func show(_ entry: LiveLibraryEntry, origin: String) {
        returnFocus = nil
        catalogReturnFocus = origin
        title = entry
        episodes = nil
        episodeOffset = 0
        focus = entry.featuredPath != nil || entry.watchPath != nil ? "detail-play" : "details-secondary"
        if kind != .movie && (kind != .cloud || entry.isFolder == true) { Task { await load(0) } }
    }

    private func back() {
        returnFocus = catalogReturnFocus
        title = nil
        episodes = nil
        error = nil
        focus = catalogReturnFocus ?? kind.id
        if Date().timeIntervalSince(lastRefresh) > 45, !expired, !busy {
            Task { await load(0) }
        }
    }

    private func followTopShelf() async {
        guard isPresented, !expired, !busy, let destination = topShelfDestination else { return }
        topShelfDestination = nil
        homeOpen = false
        catalogPresentation = nil
        catalogSearchQuery = nil
        searchOpen = false
        synopsisOpen = false
        accountPage = nil
        accountToolsOpen = false
        title = nil
        episodes = nil
        let refreshCatalog = kind != destination.kind || !query.isEmpty || catalogOffset != 0
        if refreshCatalog {
            catalog = nil
            catalogOffset = 0
        }
        query = ""
        kind = destination.kind
        returnFocus = destination.kind.id
        let entry = destination.entry
        if destination.action == .resume, entry.featuredPath != nil || entry.watchPath != nil {
            // Establish the focus anchor before playback takes over the screen.
            focus = destination.kind.id
            await Task.yield()
            guard isPresented, !expired, kind == destination.kind, returnFocus == destination.kind.id else { return }
            await open(entry, origin: destination.kind.id, featured: entry.featuredPath != nil,
                       resumeImmediately: true)
        } else {
            openDetails(entry, key: destination.kind.id)
            if refreshCatalog { await load(0) }
        }
    }

    private func adoptReturnSeries() {
        guard let entry = returnSeries else { return }
        returnSeries = nil
        guard isPresented, !expired, !showingHome, title?.id != entry.id else { return }
        show(entry, origin: returnFocus ?? focus ?? kind.id)
    }

    private func refresh(preservingContents: Bool = false) async {
        guard !busy, !expired else { return }
        spin = true
        defer { spin = false }
        if !preservingContents {
            catalog = nil
            episodes = nil
        }
        await load(0)
        if !expired {
            lastRefresh = Date()
            libraryOptions = (try? await client.libraries()) ?? libraryOptions
        }
    }

    private func load(_ offset: Int) async {
        guard !busy else { return }
        busy = true
        error = nil
        let previousFocus = focus
        defer {
            busy = false
            if !expired, focus == nil { focus = previousFocus ?? kind.id }
        }
        do {
            let page = try await client.page(kind: kind, title: title, offset: offset, query: query)
            if title == nil {
                catalog = page
                catalogOffset = offset
            } else {
                episodes = page
                episodeOffset = offset
            }
            lastRefresh = Date()
        } catch FixtureConnectionError.unauthorized {
            linkExpired()
        } catch LiveLibraryError.copyUnavailable where client.selectedLibrary != nil {
            catalog = nil
            episodes = nil
            error = "This household library is unavailable. Choose another library to continue."
        } catch is CancellationError {
            return
        } catch {
            self.error = "Couldn’t load this section. Select a category or Refresh to try again."
        }
    }

    private func open(_ entry: LiveLibraryEntry, origin: String, featured: Bool = false,
                      resumeImmediately: Bool = false) async {
        guard !busy else { return }
        busy = true
        let playsDirectly = resumeImmediately || (featured && (entry.progress ?? 0) > 0)
        openingVideo = playsDirectly
        error = nil
        defer {
            busy = false
            openingVideo = false
        }
        do {
            // One player. A copy the Apple TV system player cannot open is
            // reported, never secretly handed to a second player with its own
            // controls: the owner rejected that interface explicitly.
            let pass = try await client.play(entry, featured: featured, engine: .native)
            returnFocus = origin
            let episode = featured ? episodes?.items.first(where: { $0.watchPath == entry.featuredPath }) : entry
            select(pass, title ?? entry, pass.card.episodeTitle == nil ? nil : episode, playsDirectly)
        } catch FixtureConnectionError.unauthorized {
            linkExpired()
        } catch LiveLibraryError.unsupportedCopy {
            error = "This copy uses a format the Apple TV player can’t open yet."
        } catch LiveLibraryError.plexConnectionRequired {
            error = "Connect your own Plex account in Settings → Connections on the web, then try again."
        } catch {
            self.error = "This copy couldn’t be opened. Choose another title or try again."
        }
    }

    private func unlink() async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await client.unlink()
            disconnect()
        } catch is CancellationError {
            return
        } catch {
            self.error = "Couldn’t disconnect this TV. Try again or remove it from Devices in Omakase Plus."
            if accountPage == .settings { settingsFocus = .cancel }
            else { focus = "account-tools" }
        }
    }

    private func linkExpired() {
        TVTopShelfLibrary.invalidate()
        expired = true
        catalog = nil
        episodes = nil
        error = "This TV’s connection expired. Link it again to continue."
        focus = "account-tools"
    }
}

private struct LibrarySearchPrompt: View {
    @Environment(\.isFocused) private var isFocused
    let text: String

    var body: some View {
        Text(text.isEmpty ? "Search movies, TV shows and anime" : text)
            .font(.callout)
            .foregroundStyle(text.isEmpty && !isFocused ? Color.tvMuted : Color.tvText)
            .lineLimit(1)
            .padding(.horizontal, 24)
            .frame(maxWidth: .infinity, minHeight: 70, alignment: .leading)
            .background(Color.tvSurface, in: .rect(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(isFocused ? Color.tvFocus : Color.tvKeyline,
                                  lineWidth: isFocused ? 3 : 2)
            }
    }
}

struct TVSystemKeyboard: UIViewRepresentable {
    @Binding var text: String
    let requested: Bool
    let prompt: String
    var keyboard: UIKeyboardType = .default
    var returnKey: UIReturnKeyType = .done
    let submit: () -> Void

    func makeUIView(context: Context) -> Field {
        let field = Field()
        field.clipsToBounds = true
        field.placeholder = prompt
        field.keyboardType = keyboard
        field.returnKeyType = returnKey
        if keyboard == .URL {
            field.autocapitalizationType = .none
            field.autocorrectionType = .no
        }
        field.delegate = context.coordinator
        field.addTarget(context.coordinator, action: #selector(Coordinator.editingChanged), for: .editingChanged)
        return field
    }

    func updateUIView(_ field: Field, context: Context) {
        let shouldOpen = context.coordinator.input.requested != requested
        context.coordinator.input = self
        if field.text != text, field.markedTextRange == nil { field.text = text }
        if shouldOpen {
            Task { @MainActor [weak field] in _ = field?.becomeFirstResponder() }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(input: self) }

    final class Field: UITextField {
        // The SwiftUI button owns remote focus. The field presents the system keyboard.
        override var canBecomeFocused: Bool { false }
    }

    @MainActor
    final class Coordinator: NSObject, UITextFieldDelegate {
        var input: TVSystemKeyboard

        init(input: TVSystemKeyboard) { self.input = input }

        @objc func editingChanged(_ field: UITextField) { input.text = field.text ?? "" }

        func textFieldShouldReturn(_ textField: UITextField) -> Bool {
            editingChanged(textField)
            textField.resignFirstResponder()
            input.submit()
            return false
        }
    }
}

@MainActor
private struct TVCinemaFeature<Actions: View>: View {
    let entry: LiveLibraryEntry
    let client: LiveLibraryClient
    var label: String = ""
    @ViewBuilder let actions: () -> Actions

    private var hasArtworkReference: Bool {
        entry.artworkID != nil || entry.episodeArtwork != nil ||
        entry.posterURL != nil || entry.backdropURL != nil
    }

    var body: some View {
        ZStack(alignment: .leading) {
            Color.tvSurface
            if hasArtworkReference {
                GeometryReader { geometry in
                    TVArtworkPlaceholder(compact: true)
                        .overlay {
                            TVLibraryArtwork(entry: entry, client: client, banner: true, showsPlaceholder: false)
                        }
                        .frame(width: geometry.size.width * 0.76, height: geometry.size.height)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                LinearGradient(stops: [
                    .init(color: .tvBackground, location: 0.22),
                    .init(color: .tvBackground.opacity(0.74), location: 0.49),
                    .init(color: .clear, location: 0.87),
                ], startPoint: .leading, endPoint: .trailing)
                LinearGradient(colors: [.clear, .black.opacity(0.46)], startPoint: .center, endPoint: .bottom)
            } else {
                TVArtworkPlaceholder(showsGlow: false)
                    .frame(width: 300)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.trailing, 44)
            }
            VStack(alignment: .leading, spacing: hasArtworkReference ? 16 : 12) {
                Text(label.isEmpty ? "FROM YOUR LIBRARY" : label.uppercased())
                    .font(.headline.weight(.semibold)).tracking(4).foregroundStyle(Color.tvFocus)
                Text(entry.title).font(.system(size: hasArtworkReference ? 56 : 44, weight: .bold)).lineLimit(2)
                    .accessibilityAddTraits(.isHeader)
                if !entry.note.isEmpty {
                    Text(entry.note).font(.callout).foregroundStyle(Color.tvMuted)
                }
                if let summary = entry.summary, !summary.isEmpty {
                    Text(summary).font(.callout).foregroundStyle(Color.tvMuted).lineLimit(3)
                }
                actions().padding(.top, 4)
            }
            .frame(maxWidth: 720, alignment: .leading)
            .padding(hasArtworkReference ? 44 : 28)
        }
        .frame(minHeight: hasArtworkReference ? 390 : 280)
        .clipShape(.rect(cornerRadius: 16))
        .focusSection()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("library.feature")
    }
}

@MainActor
struct TVLibraryArtwork: View {
    let entry: LiveLibraryEntry
    let client: LiveLibraryClient
    var banner: Bool = false
    var showsPlaceholder = true
    @State private var image: UIImage?
    @State private var portraitFallback = false

    var body: some View {
        (showsPlaceholder || image != nil ? Color.tvSurface : Color.clear)
            .overlay {
                GeometryReader { geometry in
                    if let image {
                        Image(uiImage: image).resizable().interpolation(.high)
                            .aspectRatio(contentMode: portraitFallback ? .fit : .fill)
                            .frame(width: geometry.size.width, height: geometry.size.height)
                            .clipped()
                    } else if showsPlaceholder && (!banner || entry.episodeArtwork != nil) {
                        VStack(spacing: 10) {
                            Image(systemName: "film.stack").font(.system(size: 45, weight: .light))
                            Text(entry.episodeArtwork == nil ? entry.title : "Image unavailable")
                                .font(.caption).lineLimit(2)
                        }
                        .multilineTextAlignment(.center)
                        .foregroundStyle(Color.tvMuted)
                        .padding(20)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
            }
            .accessibilityHidden(true)
            .task(id: [entry.id, entry.episodeArtwork, entry.artworkID, entry.posterURL?.absoluteString,
                       entry.backdropURL?.absoluteString, banner ? "banner" : "poster"]) {
                image = nil
                portraitFallback = false
                if let data = try? await client.artwork(entry, banner: banner),
                   !Task.isCancelled {
                    image = UIImage(data: data)
                }
                if banner, entry.episodeArtwork == nil, image == nil, !Task.isCancelled,
                   let data = try? await client.artwork(entry, banner: false),
                   !Task.isCancelled {
                    portraitFallback = true
                    image = UIImage(data: data)
                }
            }
    }
}

private struct EpisodeDetailsOverlay: View {
    let entry: LiveLibraryEntry
    let details: LiveLibraryDetails?
    let loading: Bool
    let client: LiveLibraryClient
    let canPlay: Bool
    let play: () -> Void
    let close: () -> Void
    @FocusState private var focus: Action?
    @Namespace private var detailsScope
    @Environment(\.resetFocus) private var resetFocus

    private enum Action: Hashable { case play, close }
    private var title: String { details?.title ?? entry.title }
    private var subtitle: String {
        if let episodeTitle = details?.episodeTitle, !episodeTitle.isEmpty {
            return details?.note.isEmpty == false ? "\(details?.note ?? "")  \(episodeTitle)" : episodeTitle
        }
        return details?.note.isEmpty == false ? (details?.note ?? "") : entry.note
    }
    private var summary: String? {
        for value in [details?.summary, entry.summary] {
            if let value, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return value }
        }
        return nil
    }
    private var isEpisode: Bool { details?.episodeNumber != nil || entry.note.contains("Episode") }
    private var summaryHeight: CGFloat {
        let count = summary?.count ?? 0
        if count > 380 { return 180 }
        if count > 180 { return 120 }
        return 62
    }

    var body: some View {
        ZStack {
            Color.black.opacity(0.74).ignoresSafeArea()
            VStack(alignment: .leading, spacing: 0) {
                ZStack {
                    if let path = details?.artwork {
                        DetailsArtwork(path: path, client: client, entry: entry)
                    } else {
                        TVLibraryArtwork(entry: entry, client: client, banner: true)
                    }
                    if loading {
                        ProgressView().tint(.white)
                    }
                }
                .frame(height: 180)
                .clipped()
                .overlay(alignment: .bottomLeading) {
                    LinearGradient(colors: [.clear, .black.opacity(0.75)],
                                   startPoint: .center, endPoint: .bottom)
                }
                VStack(alignment: .leading, spacing: 14) {
                    Text(isEpisode ? "Episode details" : "Details")
                        .font(.caption.weight(.semibold)).tracking(2)
                        .foregroundStyle(Color.tvMuted)
                    Text(title).font(.title.bold()).lineLimit(2)
                    if !subtitle.isEmpty {
                        Text(subtitle)
                            .font(.title3.weight(.semibold)).foregroundStyle(Color.tvFocus)
                            .lineLimit(2)
                    }
                    if let summary {
                        ScrollView {
                            Text(summary)
                                .font(.callout)
                                .foregroundStyle(Color.tvText)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.trailing, 16)
                        }
                        .frame(maxHeight: summaryHeight)
                    } else if loading {
                        ProgressView("Loading episode details…").tint(.white)
                    }
                    HStack(spacing: 18) {
                        if canPlay {
                            Button("Play now", action: play)
                                .buttonStyle(TVActionStyle(focused: focus == .play, tint: .tvFocus))
                                .focusEffectDisabled()
                                .prefersDefaultFocus(in: detailsScope)
                                .focused($focus, equals: .play)
                        } else {
                            Text("Playback unavailable")
                                .font(.callout).foregroundStyle(Color.tvMuted)
                        }
                        Button("Close", action: close)
                            .buttonStyle(TVActionStyle(focused: focus == .close))
                            .focusEffectDisabled()
                            .focused($focus, equals: .close)
                    }
                    .focusSection()
                    .padding(.top, 6)
                }
                .padding(26)
            }
            .frame(width: 760, alignment: .leading)
            .background(Color.tvSurface)
            .clipShape(.rect(cornerRadius: 22))
            .overlay {
                RoundedRectangle(cornerRadius: 22).strokeBorder(Color.tvKeyline, lineWidth: 1)
            }
            .shadow(color: .black.opacity(0.55), radius: 40, y: 24)
        }
        .focusScope(detailsScope)
        .defaultFocus($focus, .play)
        .task {
            await Task.yield()
            resetFocus(in: detailsScope)
            focus = .play
        }
        .accessibilityAddTraits(.isModal)
    }
}

private struct DetailsArtwork: View {
    let path: String
    let client: LiveLibraryClient
    let entry: LiveLibraryEntry
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Color.tvSurface.overlay {
                    Image(uiImage: image).resizable().interpolation(.high)
                        .aspectRatio(contentMode: .fill)
                }
            } else if failed {
                TVLibraryArtwork(entry: entry, client: client, banner: true)
            } else {
                Color.tvSurface.overlay { ProgressView().tint(.white) }
            }
        }
        .task(id: path) {
            failed = false
            image = (try? await client.detailsArtwork(path: path)).flatMap { UIImage(data: $0) }
            failed = image == nil
        }
    }
}



@MainActor
private struct TVForYouScreen: View {
    let client: LiveLibraryClient
    let query: String?
    let dismiss: () -> Void

    @State private var category: LiveRecommendationCategory = .anime
    @State private var page: LiveRecommendationsPage?
    @State private var selected: LiveRecommendation?
    @State private var selectionFocus: String?
    @State private var resultByTitle: [String: LiveTitleRequest] = [:]
    @State private var loading = false
    @State private var requesting = false
    @State private var error: String?
    @FocusState private var focus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack(spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(query == nil ? "For You" : "Search results")
                        .font(.title2.bold()).accessibilityAddTraits(.isHeader)
                    if let query {
                        Text(query).font(.callout).foregroundStyle(Color.tvMuted).lineLimit(1)
                    }
                }
                Spacer()
                if query == nil && selected == nil {
                    ForEach(LiveRecommendationCategory.allCases) { item in
                        action(item.label, key: item.id, selected: item == category) {
                            selectionFocus = nil
                            error = nil
                            category = item
                        }
                        .accessibilityAddTraits(item == category ? .isSelected : [])
                    }
                }
                action("Close", key: "close", icon: "xmark") { dismiss() }
            }
            if let selected {
                titleDetails(selected)
            } else if loading {
                ProgressView(query == nil ? "Loading recommendations" : "Searching…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let page {
                ScrollView {
                    VStack(alignment: .leading, spacing: 36) {
                        ForEach(page.warnings ?? []) { warning in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(warning.label).font(.callout.bold())
                                Text(warning.detail).font(.caption).foregroundStyle(Color.tvMuted)
                            }
                        }
                        if page.rows.allSatisfy({ $0.cards.isEmpty }) {
                            Text(emptyMessage(page))
                                .foregroundStyle(Color.tvMuted).font(.body)
                        }
                        ForEach(page.rows.filter { !$0.cards.isEmpty }) { row in
                            shelf(row)
                        }
                        if query == nil && !requests.isEmpty {
                            requestsView
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 20)
                }
            } else if let error {
                VStack(alignment: .leading, spacing: 20) {
                    Text(error).foregroundStyle(Color.tvWarning)
                    action("Try again", key: "retry") { Task { await load() } }
                }
            }
        }
        .padding(.horizontal, 88).padding(.vertical, 54)
        .foregroundStyle(Color.tvText)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color.tvBackground.ignoresSafeArea())
        .defaultFocus($focus, defaultFocusKey)
        .task(id: category) { await load() }
        .onExitCommand {
            if selected != nil { closeDetails() }
            else { dismiss() }
        }
        .accessibilityIdentifier(query == nil ? "for-you" : "catalog-search")
    }

    private var defaultFocusKey: String {
        if selected != nil { return "title-back" }
        return selectionFocus ?? (query == nil ? category.id : "close")
    }

    private func emptyMessage(_ page: LiveRecommendationsPage) -> String {
        if query == nil { return "No recommendations are available right now." }
        if page.warnings?.isEmpty == false { return "No matches in the catalogs that responded." }
        return "No matching titles."
    }

    private var requests: [LiveTitleRequest] {
        let saved = page?.requests ?? []
        let knownIDs = Set(saved.map(\.id))
        let updated = saved.map { resultByTitle[$0.id] ?? $0 }
            + resultByTitle.values.filter { !knownIDs.contains($0.id) }.sorted { $0.title < $1.title }
        return updated.filter { !["in_library", "ready", "removed"].contains($0.state) }
    }

    private func action(_ label: String, key: String, icon: String? = nil, selected: Bool = false,
                        perform: @escaping () -> Void) -> some View {
        Button(action: perform) {
            if let icon { Label(label, systemImage: icon) }
            else { Text(label) }
        }
        .buttonStyle(TVActionStyle(focused: focus == key, compact: true, selected: selected))
        .focusEffectDisabled()
        .focused($focus, equals: key)
        .accessibilityIdentifier("for-you." + key)
    }

    private func shelf(_ row: LiveRecommendationsPage.Row) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(row.title).font(.title3.bold()).accessibilityAddTraits(.isHeader)
            ScrollView(.horizontal) {
                LazyHStack(alignment: .top, spacing: 26) {
                    ForEach(row.cards) { card in
                        let key = row.key + ":" + card.key
                        Button {
                            selectionFocus = key
                            error = nil
                            selected = card
                        } label: {
                            VStack(alignment: .leading, spacing: 10) {
                                TVRecommendationPoster(card: card, client: client)
                                    .frame(width: 196, height: 268)
                                Text(card.title).font(.callout.weight(.semibold))
                                    .lineLimit(2, reservesSpace: true)
                                if let status = resultByTitle[card.key] ?? card.request {
                                    Text(status.label).font(.caption).foregroundStyle(Color.tvAccent)
                                }
                            }
                            .frame(width: 196, alignment: .topLeading)
                            .padding(10)
                            .background(focus == key ? Color.tvRaised : Color.clear,
                                        in: RoundedRectangle(cornerRadius: 12))
                            .overlay {
                                if focus == key {
                                    RoundedRectangle(cornerRadius: 12).strokeBorder(Color.tvFocus, lineWidth: 3)
                                }
                            }
                        }
                        .buttonStyle(TVBareButtonStyle())
                        .focusEffectDisabled()
                        .focused($focus, equals: key)
                    }
                }
                .padding(4)
            }
            .focusSection()
        }
    }

    private var requestsView: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Your requests").font(.title3.bold()).accessibilityAddTraits(.isHeader)
            ForEach(requests) { status in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(status.title).font(.callout.bold())
                        Spacer()
                        Text(status.label).font(.callout).foregroundStyle(Color.tvAccent)
                    }
                    if !status.detail.isEmpty {
                        Text(status.detail).font(.caption).foregroundStyle(Color.tvMuted)
                    }
                }
                .padding(20)
                .background(Color.tvSurface, in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }

    private func titleDetails(_ card: LiveRecommendation) -> some View {
        HStack(alignment: .top, spacing: 36) {
            TVRecommendationPoster(card: card, client: client).frame(width: 288, height: 414)
            VStack(alignment: .leading, spacing: 20) {
                Text(card.title).font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
                if let summary = card.summary, !summary.isEmpty {
                    Text(summary).font(.body).foregroundStyle(Color.tvMuted).lineLimit(6)
                }
                if let status = resultByTitle[card.key] ?? card.request {
                    Text(status.label).font(.title3.bold()).foregroundStyle(Color.tvAccent)
                    if !status.detail.isEmpty {
                        Text(status.detail).font(.callout).foregroundStyle(Color.tvMuted)
                    }
                }
                if let error { Text(error).font(.callout).foregroundStyle(Color.tvWarning) }
                HStack(spacing: 16) {
                    action("Back", key: "title-back", icon: "chevron.left", perform: closeDetails)
                    let status = resultByTitle[card.key] ?? card.request
                    if let status, status.canCheck ?? (status.kind == "movie" && status.state != "ready") {
                        action(requesting ? "Checking…" : "Check status", key: "check") {
                            Task { await request(card, check: true) }
                        }
                        .disabled(requesting)
                    } else if status == nil && card.canRequest {
                        action(requesting ? "Requesting…" : "Request", key: "request", icon: "plus") {
                            Task { await request(card, check: false) }
                        }
                        .disabled(requesting)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 20)
        .focusSection()
        .task(id: card.key) {
            await Task.yield()
            guard !Task.isCancelled, selected?.key == card.key else { return }
            focus = "title-back"
        }
    }

    private func closeDetails() {
        selected = nil
        error = nil
        let key = selectionFocus
        Task { @MainActor in
            await Task.yield()
            focus = key
        }
    }

    private func load() async {
        let wanted = category
        loading = true
        page = nil
        error = nil
        defer { if category == wanted { loading = false } }
        do {
            let fetched: LiveRecommendationsPage
            if let query { fetched = try await client.searchCatalog(query) }
            else { fetched = try await client.recommendations(wanted) }
            guard !Task.isCancelled, category == wanted else { return }
            page = fetched
            resultByTitle = [:]
        } catch {
            guard !Task.isCancelled, category == wanted else { return }
            self.error = query == nil ? "Couldn’t load For You." : "Couldn’t search the catalog."
        }
    }

    private func request(_ card: LiveRecommendation, check: Bool) async {
        guard !requesting else { return }
        requesting = true
        error = nil
        defer { requesting = false }
        do {
            let result = try await client.requestTitle(card, check: check)
            resultByTitle[result.id] = result
            if selected?.key == card.key { focus = "title-back" }
        } catch {
            guard selected?.key == card.key else { return }
            self.error = check ? "Couldn’t refresh this request." : "Couldn’t send this request. Try again."
        }
    }
}

@MainActor
private struct TVRecommendationPoster: View {
    let card: LiveRecommendation
    let client: LiveLibraryClient
    @State private var image: UIImage?

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color.tvSurface
                if let image {
                    Image(uiImage: image).resizable().scaledToFill()
                } else {
                    Image(systemName: "film.stack").font(.largeTitle).foregroundStyle(Color.tvMuted)
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
            .clipShape(RoundedRectangle(cornerRadius: geometry.size.width / 22))
        }
        .accessibilityHidden(true)
        .task(id: card.posterURL) {
            image = nil
            if let bytes = try? await client.recommendationArtwork(card), !Task.isCancelled {
                image = UIImage(data: bytes)
            }
        }
    }
}
