import SwiftUI

@main
struct OmakasePlusTVApp: App {
    @StateObject private var session = PlaybackSession()
    @State private var presenting = true

    var body: some Scene {
        WindowGroup {
            ZStack {
                Color.black.ignoresSafeArea()
                if presenting {
                    PlayerScreen(session: session, showsDiagnostics: false, playNext: {}) {
                        presenting = false
                    }
                }
            }
            .preferredColorScheme(.dark)
        }
    }
}
