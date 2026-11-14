import AVKit
import QualificationCore
import SwiftUI
import UIKit

@MainActor
struct PlayerScreen: View {
    @ObservedObject var session: PlaybackSession
    var showsDiagnostics = true
    var playNext: (() async throws -> Void)? = nil
    let dismiss: () -> Void
    @State private var nextTask: Task<Void, Never>?
    @State private var nextError = false

    private var nextAction: (() -> Void)? {
        guard session.canPlayNext, playNext != nil else { return nil }
        return openNext
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            if let direct = session.directEngine {
                DirectPlayerControls(session: session, engine: direct, next: nextAction,
                                     openingNext: nextTask != nil, dismiss: dismiss)
            } else {
                NativePlayer(session: session, nextAction: nextAction,
                             openingNext: nextTask != nil, dismiss: dismiss).ignoresSafeArea()
            }
            if showsDiagnostics {
                VStack(alignment: .leading, spacing: 6) {
                    Text(session.directEngine == nil ? "QUALIFICATION | AVKit only" : "QUALIFICATION | VLCKit direct")
                        .font(.caption.bold())
                    Text(session.status).font(.caption)
                    Text("\(PlaybackSession.clock(session.position)) / \(PlaybackSession.clock(session.duration))")
                        .font(.caption.monospacedDigit())
                }
                .padding(16)
                .background(Color.black.opacity(0.7))
                .padding(40)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
            }
        }
        .overlay(alignment: .topTrailing) {
            if nextTask != nil || nextError {
                Text(nextError ? "Couldn’t open the next episode" : "Opening next episode…")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 14)
                    .background(Color.black.opacity(0.86), in: RoundedRectangle(cornerRadius: 12))
                    .padding(44)
                    .allowsHitTesting(false)
            }
        }
        .task(id: nextError) {
            if nextError {
                try? await Task.sleep(for: .seconds(5))
                if !Task.isCancelled { nextError = false }
            }
        }
        .onDisappear {
            nextTask?.cancel()
            session.stop()
        }
        .onChange(of: session.isPresenting, initial: true) { _, presenting in
            if !presenting {
                nextTask?.cancel()
                dismiss()
            }
        }
    }

    private func openNext() {
        guard let playNext, session.canPlayNext, nextTask == nil else { return }
        nextError = false
        nextTask = Task { @MainActor in
            defer { nextTask = nil }
            do {
                try await playNext()
            } catch {
                if !Task.isCancelled && !(error is CancellationError) { nextError = true }
            }
        }
    }
}

@MainActor
private struct DirectPlayerControls: View {
    @ObservedObject var session: PlaybackSession
    let engine: VLCPlayerEngine
    let next: (() -> Void)?
    let openingNext: Bool
    let dismiss: () -> Void
    @State private var showingControls = true
    @FocusState private var transportFocused: Bool

    var body: some View {
        ZStack(alignment: .bottom) {
            VLCPlayerView(engine: engine)
                .ignoresSafeArea()
                .accessibilityHidden(true)
            if showingControls {
                VStack(spacing: 12) {
                    if session.duration > 0 {
                        ProgressView(value: min(session.position, session.duration), total: session.duration)
                    }
                    HStack {
                        Button {
                            session.skip(by: -10)
                        } label: {
                            Label("Back 10 seconds", systemImage: "gobackward.10")
                        }
                        Button(session.isDirectPlaybackRequested ? "Pause" : "Play") {
                            session.togglePlayPause()
                        }
                        .focused($transportFocused)
                        Button {
                            session.skip(by: 10)
                        } label: {
                            Label("Forward 10 seconds", systemImage: "goforward.10")
                        }
                        trackMenu(.audio, title: "Audio")
                        trackMenu(.subtitle, title: "Subtitles")
                        if let next {
                            Button("Next Episode", action: next)
                                .disabled(openingNext)
                        }
                        Button("Hide Controls") { showingControls = false }
                    }
                }
                .padding(24)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
                .padding(48)
                .onAppear { transportFocused = true }
            } else {
                Button("Show Controls") { showingControls = true }
                    .opacity(0.25)
                    .focused($transportFocused)
                    .padding(48)
                    .onAppear { transportFocused = true }
            }
        }
        .onPlayPauseCommand { session.togglePlayPause() }
        .onExitCommand(perform: dismiss)
    }

    private func trackMenu(_ kind: TrackKind, title: String) -> some View {
        let tracks = session.directTracks.filter { $0.kind == kind }
        let selected = engine.selectedTrackID(kind: kind)
        return Menu(title) {
            if kind == .subtitle {
                Button {
                    session.selectTrack(kind: kind, id: nil)
                } label: {
                    if selected == nil { Label("Off", systemImage: "checkmark") }
                    else { Text("Off") }
                }
            }
            ForEach(Array(tracks.enumerated()), id: \.element.id) { index, track in
                let name = track.name.flatMap { $0.isEmpty ? nil : $0 }
                let label = name ?? track.language ?? "Track \(index + 1)"
                Button {
                    session.selectTrack(kind: kind, id: track.id)
                } label: {
                    if selected == track.id { Label(label, systemImage: "checkmark") }
                    else { Text(label) }
                }
            }
        }
        .disabled(tracks.isEmpty && kind == .audio)
    }
}

@MainActor
struct NativePlayer: UIViewControllerRepresentable {
    @ObservedObject var session: PlaybackSession
    let nextAction: (() -> Void)?
    let openingNext: Bool
    let dismiss: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(dismiss: dismiss) }

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        controller.delegate = context.coordinator
        controller.showsPlaybackControls = true
        controller.allowsPictureInPicturePlayback = false
        controller.appliesPreferredDisplayCriteriaAutomatically = true
        updateUIViewController(controller, context: context)
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {
        if controller.player !== session.player { controller.player = session.player }
        let nearEnd = PlaybackSession.shouldSuggestNext(position: session.position, duration: session.duration)
        context.coordinator.updateNext(on: controller, action: nextAction, opening: openingNext, nearEnd: nearEnd)
    }

    static func dismantleUIViewController(_ controller: AVPlayerViewController, coordinator: Coordinator) {
        controller.player?.pause()
        controller.player = nil
        controller.delegate = nil
    }

    @MainActor
    final class Coordinator: NSObject, AVPlayerViewControllerDelegate {
        let dismiss: () -> Void
        private var nextAction: (() -> Void)?
        private var transportState: NextControl = .hidden
        private var contextualVisible = false

        private enum NextControl { case hidden, opening, available }

        init(dismiss: @escaping () -> Void) { self.dismiss = dismiss }

        func updateNext(on controller: AVPlayerViewController, action: (() -> Void)?, opening: Bool, nearEnd: Bool) {
            nextAction = action
            let transport: NextControl = action == nil ? .hidden : opening ? .opening : .available
            if transport != transportState {
                controller.transportBarCustomMenuItems = transport == .hidden ? [] : [nextButton(disabled: opening)]
                transportState = transport
            }
            let context = transport == .available && nearEnd
            if context != contextualVisible {
                controller.contextualActions = context ? [nextButton()] : []
                contextualVisible = context
            }
        }

        private func nextButton(disabled: Bool = false) -> UIAction {
            UIAction(title: "Next Episode", image: UIImage(systemName: "forward.end.fill"),
                     attributes: disabled ? .disabled : []) { [weak self] _ in
                self?.nextAction?()
            }
        }

        func playerViewControllerShouldDismiss(_ playerViewController: AVPlayerViewController) -> Bool {
            dismiss()
            return false // SwiftUI owns the presentation; leave transport/track controls to AVKit.
        }
    }
}
