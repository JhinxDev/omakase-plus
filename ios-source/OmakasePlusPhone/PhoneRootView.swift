import QualificationCore
import SwiftUI

struct PhoneRootView: View {
    @State private var store = PhoneSessionStore()
    @State private var selectedTab = 0
    @Environment(\.scenePhase) private var scenePhase

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
            } else if store.restoreState == .needsRetry {
                reconnecting
            } else {
                PhoneOnboardingView(store: store)
            }
        }
        // Retry a locked-phone restore and refresh the device link each time the app
        // becomes active, including the first launch.
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            store.resumeIfNeeded()
            await store.renewIfNeeded()
        }
        .alert(
            "Omakase Plus",
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

    private var reconnecting: some View {
        VStack(spacing: 18) {
            ProgressView()
                .tint(Color.appAccent)
            Text("Reconnecting…")
                .font(.headline)
            Text("Unlock your iPhone to open your saved connection.")
                .font(.subheadline)
                .foregroundStyle(Color.appMuted)
                .multilineTextAlignment(.center)
            Button("Try again") { store.resumeIfNeeded() }
                .buttonStyle(.borderedProminent)
                .tint(Color.appAccent)
                .foregroundStyle(Color.appBackground)
            Button("Connect a different server") { store.abandonRestore() }
                .font(.subheadline)
                .foregroundStyle(Color.appAccent)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.appBackground.ignoresSafeArea())
        .foregroundStyle(Color.appText)
    }
}

extension LiveTestPass: @retroactive Identifiable {
    public var id: String { mediaID }
}
