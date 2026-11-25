import AVKit
import Observation
import QualificationCore
import SwiftUI

/// Offline qualification shell. No server, pairing, credentials or network media.
@MainActor
@Observable
private final class PrototypeStore {
    let fixture: LibraryFixture?
    var ledger = ResumeLedger()

    init() {
        fixture = Bundle.main.url(forResource: "baseline", withExtension: "mp4")
            .flatMap { LibraryFixture(baselineURL: $0) }
    }

    func position(for card: LibraryCard) -> Double {
        ledger.record(for: card.id.resumeIdentity, now: Date().timeIntervalSince1970)?.position ?? 0
    }
}

struct PrototypeLibrary: View {
    @State private var store = PrototypeStore()
    @State private var search = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("Offline simulator prototype")
                        .font(.headline)
                        .accessibilityIdentifier("prototypeNotice")
                    Text("Synthetic catalog and silent test clip. Progress lasts for this app session.")
                        .foregroundStyle(.secondary)
                }
                if let fixture = store.fixture {
                    ForEach(fixture.cards.filter { search.isEmpty || $0.title.localizedCaseInsensitiveContains(search) }) { card in
                        NavigationLink {
                            PrototypeDetail(store: store, card: card)
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(card.title).font(.headline)
                                Text(card.episodeTitle ?? card.kind.rawValue.capitalized)
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 6)
                        }
                        .accessibilityIdentifier("title-\(card.kind.rawValue)")
                    }
                } else {
                    Text("The bundled synthetic clip is missing.").accessibilityIdentifier("missingFixture")
                }
            }
            .searchable(text: $search, prompt: "Search synthetic titles")
            .navigationTitle("Omakase Prototype")
        }
    }
}

private struct PrototypeDetail: View {
    let store: PrototypeStore
    let card: LibraryCard
    @Environment(\.scenePhase) private var scenePhase
    @State private var player: AVPlayer?
    @State private var running = false
    @State private var seconds = 0.0
    @State private var message = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text(card.title).font(.largeTitle.bold())
                if let episode = card.episodeTitle { Text(episode).font(.headline) }
                Text(card.synopsis)
                Text("Synthetic silent clip").foregroundStyle(.secondary)
                if let player {
                    VideoPlayer(player: player).frame(height: 220)
                    Text(seconds >= 1 ? "Playback advancing" : "Opening clip")
                        .accessibilityIdentifier("playbackState")
                    Text("Position: \(Int(seconds)) seconds")
                        .monospacedDigit().accessibilityIdentifier("playbackPosition")
                    Button("Seek forward five seconds") {
                        let target = min(seconds + 5, 25)
                        player.seek(to: CMTime(seconds: target, preferredTimescale: 600))
                    }.accessibilityIdentifier("seekForward")
                    Button("Stop and save session position", action: stop)
                        .accessibilityIdentifier("stopPlayback")
                } else {
                    Text("Session position: \(Int(store.position(for: card))) seconds")
                        .accessibilityIdentifier("resumePosition")
                    Button(store.position(for: card) > 0 ? "Resume synthetic clip" : "Play synthetic clip", action: play)
                        .buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("playClip")
                }
                if !message.isEmpty { Text(message).accessibilityIdentifier("playbackError") }
            }
            .padding(24)
            .frame(maxWidth: 600, alignment: .leading)
        }
        .navigationTitle("Synthetic detail")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: running) {
            while running, !Task.isCancelled {
                if let player {
                    let value = player.currentTime().seconds
                    if value.isFinite { seconds = max(0, value) }
                    if player.currentItem?.status == .failed { message = "The synthetic clip could not be decoded." }
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
        .onDisappear(perform: stop)
        .onChange(of: scenePhase) { _, phase in if phase != .active { stop() } }
    }

    private func play() {
        guard let media = store.fixture?.media(for: card.id) else { return }
        let next = AVPlayer(url: media.url)
        seconds = store.position(for: card)
        if seconds > 0 { next.seek(to: CMTime(seconds: seconds, preferredTimescale: 600)) }
        player = next
        message = ""
        running = true
        next.play()
    }

    private func stop() {
        guard let player else { return }
        player.pause()
        let position = player.currentTime().seconds
        let duration = player.currentItem?.duration.seconds ?? .nan
        if position.isFinite, duration.isFinite, duration > 0 {
            store.ledger.checkpoint(identity: card.id.resumeIdentity,
                                    position: min(max(0, position), duration), duration: duration,
                                    reachedEnd: position >= duration, now: Date().timeIntervalSince1970)
        }
        running = false
        self.player = nil
    }
}
