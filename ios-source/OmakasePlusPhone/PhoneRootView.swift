import QualificationCore
import SwiftUI

struct PhoneRootView: View {
    @State private var store = PhoneSessionStore()
    @State private var selectedTab = 0

    var body: some View {
        Group {
            if let client = store.client {
                TabView(selection: $selectedTab) {
                    PhoneHomeView(store: store, client: client) {
                        selectedTab = 2
                    }
                    .tabItem {
                        Label("Home", systemImage: "house.fill")
                    }
                    .tag(0)

                    PhoneLibraryView(store: store, client: client)
                        .tabItem {
                            Label("Library", systemImage: "square.grid.2x2.fill")
                        }
                        .tag(1)

                    PhoneDiscoverView(store: store, client: client)
                        .tabItem {
                            Label("Discover", systemImage: "magnifyingglass")
                        }
                        .tag(2)

                    PhoneRequestsView(store: store, client: client)
                        .tabItem {
                            Label("Requests", systemImage: "tray.and.arrow.down.fill")
                        }
                        .tag(3)

                    PhoneSettingsView(store: store)
                        .tabItem {
                            Label("Settings", systemImage: "gearshape.fill")
                        }
                        .tag(4)
                }
                .fullScreenCover(
                    item: Binding(
                        get: { store.activePass },
                        set: { next in
                            if next == nil {
                                Task { await store.closePlayer() }
                            }
                        }
                    )
                ) { pass in
                    PhonePlayerView(store: store, pass: pass)
                }
            } else {
                PhoneOnboardingView(store: store)
            }
        }
        .alert(
            "Playback Notice",
            isPresented: Binding(
                get: { store.errorMessage != nil && store.client != nil },
                set: { shown in if !shown { store.errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) { store.errorMessage = nil }
        } message: {
            Text(store.errorMessage ?? "")
        }
    }
}

extension LiveTestPass: @retroactive Identifiable {
    public var id: String { mediaID }
}
