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
    @State private var selector: TrackKind?
    @State private var returnFocus: Control = .play
    @FocusState private var controlFocus: Control?
    @FocusState private var trackFocus: TrackRow?

    private enum Control: String, Hashable {
        case back, play, forward, audio, subtitles, next, hide, show
    }

    private enum TrackRow: Hashable {
        case done, off, track(String)
    }

    var body: some View {
        ZStack {
            VLCPlayerView(engine: engine)
                .ignoresSafeArea()
                .accessibilityHidden(true)

            if let selector {
                trackSelector(selector)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.black.opacity(0.48).ignoresSafeArea())
            } else if showingControls {
                transport
                    .padding(.horizontal, 48)
                    .padding(.bottom, 44)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                    .defaultFocus($controlFocus, returnFocus)
                    .task {
                        await Task.yield()
                        guard !Task.isCancelled else { return }
                        controlFocus = returnFocus
                    }
            } else {
                control(.show, "Show Controls", icon: "chevron.up", width: 196, caption: true) {
                    returnFocus = .play
                    showingControls = true
                }
                .padding(.bottom, 44)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                .defaultFocus($controlFocus, .show)
            }
        }
        .onPlayPauseCommand { session.togglePlayPause() }
        .onExitCommand(perform: goBack)
    }

    private var transport: some View {
        VStack(spacing: 16) {
            if session.duration > 0 {
                HStack(spacing: 16) {
                    Text(PlaybackSession.clock(session.position))
                    ProgressView(value: min(max(0, session.position), session.duration),
                                 total: session.duration)
                        .tint(.white)
                        .accessibilityLabel("Playback progress")
                    Text(PlaybackSession.clock(session.duration))
                }
                .font(.system(size: 22, weight: .medium).monospacedDigit())
                .foregroundStyle(.white.opacity(0.86))
            }

            HStack(spacing: 14) {
                control(.back, "Back 10 seconds", icon: "gobackward.10", width: 70) {
                    session.skip(by: -10)
                }
                control(.play, session.isDirectPlaybackRequested ? "Pause" : "Play",
                        icon: session.isDirectPlaybackRequested ? "pause.fill" : "play.fill", width: 76) {
                    session.togglePlayPause()
                }
                control(.forward, "Forward 10 seconds", icon: "goforward.10", width: 70) {
                    session.skip(by: 10)
                }
                Spacer().frame(width: 16)
                control(.audio, "Audio", icon: "waveform", width: 132, caption: true) {
                    openSelector(.audio, from: .audio)
                }
                .disabled(!session.directTracks.contains { $0.kind == .audio })
                control(.subtitles, "Subtitles", icon: "captions.bubble", width: 158, caption: true) {
                    openSelector(.subtitle, from: .subtitles)
                }
                if let next {
                    control(.next, "Next Episode", icon: "forward.end.fill", width: 70, action: next)
                        .disabled(openingNext)
                }
                control(.hide, "Hide Controls", icon: "chevron.down", width: 70) {
                    showingControls = false
                }
            }
        }
        .padding(22)
        .background(Color.black.opacity(0.86), in: .rect(cornerRadius: 24))
        .overlay {
            RoundedRectangle(cornerRadius: 24)
                .strokeBorder(.white.opacity(0.18), lineWidth: 1)
        }
    }

    private func control(
        _ item: Control, _ label: String, icon: String, width: CGFloat,
        caption: Bool = false, action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon).font(.system(size: caption ? 24 : 30, weight: .semibold))
                if caption {
                    Text(label).font(.system(size: 23, weight: .semibold))
                        .lineLimit(1)
                }
            }
            .frame(width: width, height: 66)
        }
        .buttonStyle(PlayerControlStyle(focused: controlFocus == item))
        .focused($controlFocus, equals: item)
        .accessibilityLabel(label)
        .accessibilityIdentifier("direct.control." + item.rawValue)
    }

    private func trackSelector(_ kind: TrackKind) -> some View {
        let tracks = session.directTracks.filter { $0.kind == kind }
        let selected = engine.selectedTrackID(kind: kind)
        let initialFocus: TrackRow = kind == .subtitle ? .off
            : tracks.first.map { .track($0.id) } ?? .done

        return VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text(kind == .subtitle ? "Subtitles" : "Audio Tracks")
                    .font(.system(size: 36, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Done", action: closeSelector)
                    .font(.system(size: 24, weight: .semibold))
                    .frame(minWidth: 110)
                    .buttonStyle(PlayerControlStyle(focused: trackFocus == .done))
                    .focused($trackFocus, equals: .done)
                    .accessibilityIdentifier("direct.track.done")
            }
            ScrollView {
                VStack(spacing: 12) {
                    if kind == .subtitle {
                        trackRow(.off, title: "Off", active: selected == nil, kind: kind, id: nil)
                    }
                    ForEach(Array(tracks.enumerated()), id: \.element.id) { index, track in
                        let name = track.name?.trimmingCharacters(in: .whitespacesAndNewlines)
                        let title = name.flatMap { $0.isEmpty ? nil : $0 }
                            ?? track.language ?? "Track \(index + 1)"
                        trackRow(.track(track.id), title: title, active: selected == track.id,
                                 kind: kind, id: track.id)
                    }
                }
                .padding(3)
            }
            .frame(height: 322)
        }
        .foregroundStyle(.white)
        .padding(30)
        .frame(width: 746)
        .background(Color(red: 0.07, green: 0.08, blue: 0.11), in: .rect(cornerRadius: 26))
        .overlay {
            RoundedRectangle(cornerRadius: 26)
                .strokeBorder(.white.opacity(0.18), lineWidth: 1)
        }
        .accessibilityIdentifier("direct.track.selector")
        .defaultFocus($trackFocus, initialFocus)
        .task(id: kind) {
            await Task.yield()
            guard !Task.isCancelled else { return }
            trackFocus = initialFocus
        }
    }

    private func trackRow(
        _ row: TrackRow, title: String, active: Bool, kind: TrackKind, id: String?
    ) -> some View {
        Button {
            if session.selectTrack(kind: kind, id: id) { closeSelector() }
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 16) {
                Image(systemName: "checkmark")
                    .font(.system(size: 21, weight: .bold))
                    .opacity(active ? 1 : 0)
                    .frame(width: 28)
                    .accessibilityHidden(true)
                Text(title)
                    .font(.system(size: 25, weight: .medium))
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 14)
            .frame(minHeight: 61)
        }
        .buttonStyle(PlayerControlStyle(focused: trackFocus == row))
        .focused($trackFocus, equals: row)
        .accessibilityAddTraits(active ? .isSelected : [])
        .accessibilityIdentifier(id.map { "direct.track.\(kind.rawValue).\($0)" } ?? "direct.track.off")
    }

    private func openSelector(_ kind: TrackKind, from button: Control) {
        returnFocus = button
        trackFocus = nil
        selector = kind
    }

    private func closeSelector() {
        trackFocus = nil
        selector = nil
    }

    private func goBack() {
        if selector != nil {
            closeSelector()
        } else if showingControls {
            showingControls = false
        } else {
            dismiss()
        }
    }
}

private struct PlayerControlStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    let focused: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(focused ? Color.black : Color.white)
            .padding(3)
            .background(focused ? Color(red: 0.85, green: 0.79, blue: 1) : Color.white.opacity(0.1),
                        in: .rect(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(.white.opacity(focused ? 0 : 0.18), lineWidth: 1)
            }
            .opacity(isEnabled ? (configuration.isPressed ? 0.8 : 1) : 0.42)
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
