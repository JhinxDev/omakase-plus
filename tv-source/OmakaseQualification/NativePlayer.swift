import AVKit
import Combine
import CoreMedia
import QualificationCore
import SwiftUI
import UIKit

@MainActor
struct PlayerScreen: View {
    @ObservedObject var session: PlaybackSession
    var showsDiagnostics = true
    var playNext: (() async throws -> Void)? = nil
    var mediaInformation: LiveMediaInformation? = nil
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
                                     openingNext: nextTask != nil, mediaInformation: mediaInformation,
                                     dismiss: dismiss)
            } else {
                NativePlayer(session: session, nextAction: nextAction,
                             openingNext: nextTask != nil, mediaInformation: mediaInformation,
                             dismiss: dismiss).ignoresSafeArea()
            }
            if showsDiagnostics {
                PlayerDiagnosticsOverlay(
                    direct: session.directEngine != nil,
                    status: session.status,
                    clock: session.clock
                )
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
private struct PlayerDiagnosticsOverlay: View {
    let direct: Bool
    let status: String
    @ObservedObject var clock: PlaybackClock

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(direct ? "QUALIFICATION | VLCKit direct" : "QUALIFICATION | AVKit only")
                .font(.caption.bold())
            Text(status).font(.caption)
            Text("\(PlaybackSession.clock(clock.position)) / \(PlaybackSession.clock(clock.duration))")
                .font(.caption.monospacedDigit())
        }
        .padding(16)
        .background(Color.black.opacity(0.7))
        .padding(40)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

@MainActor
private struct DirectTransportProgressView: View {
    @ObservedObject var clock: PlaybackClock

    var body: some View {
        if clock.duration > 0 {
            HStack(spacing: 16) {
                Text(PlaybackSession.clock(clock.position))
                ProgressView(value: min(max(0, clock.position), clock.duration),
                             total: clock.duration)
                    .tint(.white)
                    .accessibilityLabel("Playback progress")
                Text(PlaybackSession.clock(clock.duration))
            }
            .font(.system(size: 22, weight: .medium).monospacedDigit())
            .foregroundStyle(.white.opacity(0.86))
        }
    }
}

@MainActor
private struct DirectPlayerControls: View {
    @ObservedObject var session: PlaybackSession
    let engine: VLCPlayerEngine
    let next: (() -> Void)?
    let openingNext: Bool
    let mediaInformation: LiveMediaInformation?
    let dismiss: () -> Void

    @State private var showingControls = true
    @State private var selector: TrackKind?
    @State private var showsMediaInformation = false
    @State private var returnFocus: Control = .play
    @FocusState private var controlFocus: Control?
    @FocusState private var trackFocus: TrackRow?

    private enum Control: String, Hashable {
        case back, play, forward, audio, subtitles, info, infoDone, next, hide, show
    }

    private enum TrackRow: Hashable {
        case done, off, track(String)
    }

    var body: some View {
        ZStack {
            VLCPlayerView(engine: engine)
                .ignoresSafeArea()
                .accessibilityHidden(true)

            if showsMediaInformation {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Media Info").font(.title2.bold())
                    MediaInformationPanel(info: mediaInformation, tracks: engine.mediaTracks)
                    control(.infoDone, "Done", icon: "checkmark", width: 140, caption: true) {
                        closeMediaInformation()
                    }
                }
                .padding(38)
                .foregroundStyle(.white)
                .background(.black.opacity(0.9), in: .rect(cornerRadius: 24))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.black.opacity(0.35).ignoresSafeArea())
                .defaultFocus($controlFocus, .infoDone)
            } else if let selector {
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
            DirectTransportProgressView(clock: session.clock)

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
                control(.info, "Media Info", icon: "info.circle", width: 70) {
                    returnFocus = .info
                    showsMediaInformation = true
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
        if showsMediaInformation {
            closeMediaInformation()
        } else if selector != nil {
            closeSelector()
        } else if showingControls {
            showingControls = false
        } else {
            dismiss()
        }
    }

    private func closeMediaInformation() {
        showsMediaInformation = false
        returnFocus = .info
    }
}

private struct MediaInformationPanel: View {
    let info: LiveMediaInformation?
    var tracks = DirectMediaTracks()

    private var rows: [(String, String)] {
        var values: [(String, String)] = []
        if let width = info?.width ?? tracks.width, let height = info?.height ?? tracks.height,
           (1...16384).contains(width), (1...16384).contains(height) {
            values.append(("Resolution", "\(width) × \(height)"))
        }
        if let video = info?.videoCodec ?? tracks.video { values.append(("Video", codecName(video))) }
        let audio = info?.audioCodecs ?? []
        var seen: Set<String> = []
        let unique = (audio.isEmpty ? tracks.audio : audio).map(codecName)
            .filter { !$0.isEmpty && seen.insert($0).inserted }
        if !unique.isEmpty { values.append(("Audio", unique.joined(separator: ", "))) }
        if let container = info?.container {
            values.append(("Container", container.lowercased() == "matroska" ? "MKV" : container.uppercased()))
        }
        if let bytes = info?.fileBytes {
            values.append(("File size", ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)))
        }
        return values
    }

    var body: some View {
        let items = rows
        if items.isEmpty {
            Text("File information is unavailable.").font(.callout)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(items.indices, id: \.self) { index in
                    HStack(alignment: .firstTextBaseline, spacing: 24) {
                        Text(items[index].0).foregroundStyle(.secondary)
                            .frame(width: 176, alignment: .leading)
                        Text(items[index].1).fontWeight(.medium)
                    }.accessibilityElement(children: .combine)
                }
            }.font(.callout)
        }
    }

    private func codecName(_ value: String) -> String {
        switch value.lowercased().split(separator: ".").first.map(String.init) {
        case "hevc", "h265", "hvc1", "hev1": "HEVC (H.265)"
        case "h264", "avc1", "avc3": "H.264"
        case "av1", "av01": "AV1"
        case "vp9", "vp09": "VP9"
        case "aac", "mp4a": "AAC"
        case "ac3", "ac-3", "a52": "Dolby Digital"
        case "eac3", "ec-3", "eac-3": "Dolby Digital Plus"
        case "dca", "dts", "dtsc", "dtsh": "DTS"
        case "truehd", "mlp": "Dolby TrueHD"
        default: value.uppercased()
        }
    }
}

private struct PlayerControlStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    let focused: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.white)
            .padding(3)
            .background(focused ? Color(red: 0.16, green: 0.13, blue: 0.25) : Color.white.opacity(0.1),
                        in: .rect(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(focused ? Color(red: 0.69, green: 0.55, blue: 0.96) : .white.opacity(0.18),
                                  lineWidth: focused ? 3 : 1)
            }
            .opacity(isEnabled ? (configuration.isPressed ? 0.8 : 1) : 0.42)
    }
}

@MainActor
struct NativePlayer: UIViewControllerRepresentable {
    @ObservedObject var session: PlaybackSession
    let nextAction: (() -> Void)?
    let openingNext: Bool
    let mediaInformation: LiveMediaInformation?
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
        context.coordinator.observe(clock: session.clock, session: session, on: controller)
        let nearEnd = PlaybackSession.shouldSuggestNext(position: session.position, duration: session.duration)
        context.coordinator.updateNext(on: controller, action: nextAction, opening: openingNext)
        context.coordinator.updateContext(on: controller, session: session, nearEnd: nearEnd)
        context.coordinator.updateSubtitleItem(on: controller)
        context.coordinator.updateMediaInformation(mediaInformation, on: controller)
    }

    static func dismantleUIViewController(_ controller: AVPlayerViewController, coordinator: Coordinator) {
        coordinator.cancelSubtitleDiscovery()
        coordinator.cancelClockObservation()
        controller.player?.pause()
        controller.player = nil
        controller.delegate = nil
    }

    @MainActor
    final class Coordinator: NSObject, AVPlayerViewControllerDelegate {
        let dismiss: () -> Void
        private var nextAction: (() -> Void)?
        private var transportState: NextControl = .hidden
        private var contextState: ContextControl = .hidden
        private weak var contextPlayer: AVPlayer?
        private weak var subtitleItem: AVPlayerItem?
        private var subtitleDiscovery: Task<Void, Never>?
        private var subtitlePositions: [SubtitlePosition: AVTextStyleRule] = [:]
        private var originalTextStyleRules: [AVTextStyleRule]?
        private var subtitlePosition = SubtitlePosition.saved
        private var mediaInformation: LiveMediaInformation?
        private var clockCancellable: AnyCancellable?
        private weak var observedClock: PlaybackClock?
        private weak var observedController: AVPlayerViewController?

        func observe(clock: PlaybackClock, session: PlaybackSession, on controller: AVPlayerViewController) {
            guard observedClock !== clock || observedController !== controller else { return }
            observedClock = clock
            observedController = controller
            clockCancellable = clock.$position
                .receive(on: RunLoop.main)
                .sink { [weak self, weak controller, weak session] pos in
                    guard let self, let controller, let session else { return }
                    let nearEnd = PlaybackSession.shouldSuggestNext(position: pos, duration: clock.duration)
                    self.updateContext(on: controller, session: session, nearEnd: nearEnd)
                }
        }

        func cancelClockObservation() {
            clockCancellable?.cancel()
            clockCancellable = nil
            observedClock = nil
            observedController = nil
        }

        func updateMediaInformation(_ info: LiveMediaInformation?, on controller: AVPlayerViewController) {
            guard info != mediaInformation else { return }
            mediaInformation = info
            guard let info else {
                controller.customInfoViewControllers = []
                return
            }
            let panel = UIHostingController(rootView: MediaInformationPanel(info: info))
            panel.title = "Media Info"
            panel.preferredContentSize = CGSize(width: 840, height: 245)
            controller.customInfoViewControllers = [panel]
        }

        private enum NextControl { case hidden, opening, available }
        private enum ContextControl: Equatable { case hidden, next, intro(LiveIntro) }

        private enum SubtitlePosition: String, CaseIterable {
            case system, top, topLower, bottomHigher, bottom

            static var saved: Self {
                UserDefaults.standard.string(forKey: preferenceKey).flatMap(Self.init(rawValue:)) ?? .system
            }

            private static let preferenceKey = "omakase.tv.subtitle-position"

            var title: String {
                switch self {
                case .system: "System Default"
                case .top: "Top"
                case .topLower: "Top (Lower)"
                case .bottomHigher: "Bottom (Higher)"
                case .bottom: "Bottom"
                }
            }

            var styleRule: AVTextStyleRule? {
                let percentage: Double
                switch self {
                case .system: return nil
                case .top: percentage = 10
                case .topLower: percentage = 16
                case .bottomHigher: percentage = 84
                case .bottom: percentage = 90
                }
                return AVTextStyleRule(textMarkupAttributes: [
                    kCMTextMarkupAttribute_OrthogonalLinePositionPercentageRelativeToWritingDirection as String:
                        NSNumber(value: percentage),
                ])
            }

            func save() { UserDefaults.standard.set(rawValue, forKey: Self.preferenceKey) }
        }

        init(dismiss: @escaping () -> Void) { self.dismiss = dismiss }

        func updateNext(on controller: AVPlayerViewController, action: (() -> Void)?, opening: Bool) {
            nextAction = action
            let transport: NextControl = action == nil ? .hidden : opening ? .opening : .available
            if transport != transportState {
                transportState = transport
                updateTransport(on: controller)
            }
        }

        func updateContext(on controller: AVPlayerViewController, session: PlaybackSession, nearEnd: Bool) {
            let player = controller.player
            let context: ContextControl
            if let marker = session.intro, player != nil, marker.end < session.duration,
               marker.contains(session.position) {
                context = .intro(marker)
            } else {
                context = transportState == .available && nearEnd ? .next : .hidden
            }
            guard context != contextState || player !== contextPlayer else { return }
            contextState = context
            contextPlayer = player
            switch context {
            case .hidden: controller.contextualActions = []
            case .next: controller.contextualActions = [nextButton()]
            case .intro(let marker):
                let skip = UIAction(title: "Skip Intro", image: UIImage(systemName: "forward.end.fill")) {
                    [weak session, weak player] _ in
                    guard let player else { return }
                    session?.skipIntro(marker, on: player)
                }
                controller.contextualActions = [skip]
            }
        }

        func updateSubtitleItem(on controller: AVPlayerViewController) {
            let item = controller.player?.currentItem
            guard item !== subtitleItem else { return }
            cancelSubtitleDiscovery()
            subtitleItem = item
            if !subtitlePositions.isEmpty {
                subtitlePositions = [:]
                originalTextStyleRules = nil
                updateTransport(on: controller)
            }
            guard let item else { return }

            subtitleDiscovery = Task { [weak self, weak controller] in
                guard let group = try? await item.asset.loadMediaSelectionGroup(for: .legible),
                      !Task.isCancelled, let self, let controller, controller.player?.currentItem === item
                else { return }
                let isHLS = (item.asset as? AVURLAsset)?.url.pathExtension.lowercased() == "m3u8"
                let containsWebVTT = group.options.contains { option in
                    option.mediaSubTypes.contains { $0.uint32Value == kCMSubtitleFormatType_WebVTT }
                        || (isHLS && option.mediaType == .subtitle && option.mediaSubTypes.isEmpty)
                }
                guard containsWebVTT else { return }
                self.originalTextStyleRules = item.textStyleRules
                for position in SubtitlePosition.allCases {
                    self.subtitlePositions[position] = position.styleRule
                }
                self.applySubtitlePosition(to: item)
                self.updateTransport(on: controller)
            }
        }

        func cancelSubtitleDiscovery() {
            subtitleDiscovery?.cancel()
            subtitleDiscovery = nil
        }

        private func applySubtitlePosition(to item: AVPlayerItem) {
            if let rule = subtitlePositions[subtitlePosition] {
                item.textStyleRules = (originalTextStyleRules ?? []) + [rule]
            } else {
                item.textStyleRules = originalTextStyleRules
            }
        }

        private func updateTransport(on controller: AVPlayerViewController) {
            var items: [UIMenuElement] = []
            if transportState != .hidden {
                items.append(nextButton(disabled: transportState == .opening))
            }
            if !subtitlePositions.isEmpty {
                let choices = SubtitlePosition.allCases.compactMap { position -> UIAction? in
                    guard position == .system || subtitlePositions[position] != nil else { return nil }
                    return UIAction(title: position.title, state: position == subtitlePosition ? .on : .off) {
                        [weak self, weak controller] _ in
                        guard let self, let controller, let item = self.subtitleItem,
                              controller.player?.currentItem === item else { return }
                        self.subtitlePosition = position
                        position.save()
                        self.applySubtitlePosition(to: item)
                        self.updateTransport(on: controller)
                    }
                }
                items.append(UIMenu(title: "Subtitle Position", image: UIImage(systemName: "arrow.up.and.down"),
                                    options: .singleSelection, children: choices))
            }
            controller.transportBarCustomMenuItems = items
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
