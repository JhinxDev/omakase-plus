import AVFoundation
import AVKit
import QualificationCore
import SwiftUI

struct PhonePlayerView: View {
    @Bindable var store: PhoneSessionStore
    let pass: LiveTestPass

    @State private var position: Double = 0
    @State private var duration: Double = 0
    @State private var seekRequest: Double?
    @State private var openingNext = false
    @State private var showMediaInfo = false

    private var activeIntro: LiveIntro? {
        guard let intro = pass.intro, duration > 0, intro.end < duration, intro.contains(position) else {
            return nil
        }
        return intro
    }

    private var showNextEpisode: Bool {
        pass.nextWatchPath != nil && duration > 0 && position >= max(0, duration - 45)
    }

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            PhoneAVPlayerController(
                pass: pass,
                audioLanguage: store.audioLanguage,
                subtitlesAlways: store.subtitlesAlways,
                subtitleLanguage: store.subtitleLanguage,
                resumeWriter: store.resumeWriter,
                position: $position,
                duration: $duration,
                seekRequest: $seekRequest,
                onDismiss: {
                    Task { await store.closePlayer() }
                }
            )
            .ignoresSafeArea()

            VStack(alignment: .trailing, spacing: 12) {
                if let intro = activeIntro {
                    Button {
                        seekRequest = intro.end
                    } label: {
                        Label("Skip Intro", systemImage: "forward.end.fill")
                            .font(.subheadline.weight(.semibold))
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.appAccent)
                    .foregroundStyle(Color.appBackground)
                }
                if showNextEpisode {
                    Button {
                        openingNext = true
                        Task {
                            defer { openingNext = false }
                            try? await store.playNext()
                        }
                    } label: {
                        Label(openingNext ? "Opening Next…" : "Next Episode", systemImage: "forward.end.fill")
                            .font(.subheadline.weight(.semibold))
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.appAccent)
                    .foregroundStyle(Color.appBackground)
                    .disabled(openingNext)
                }
            }
            .padding(.trailing, 20)
            .padding(.bottom, 76)
        }
        .overlay(alignment: .topTrailing) {
            if let info = pass.mediaInformation, info.hasDetails {
                Button {
                    showMediaInfo = true
                } label: {
                    Image(systemName: "info.circle")
                        .font(.title3)
                        .padding(10)
                        .background(Color.black.opacity(0.55), in: Circle())
                }
                .foregroundStyle(.white)
                .padding(.top, 14)
                .padding(.trailing, 18)
                .accessibilityLabel("Media Info")
            }
        }
        .sheet(isPresented: $showMediaInfo) {
            if let info = pass.mediaInformation {
                NavigationStack {
                    List {
                        if let container = info.container {
                            LabeledContent("Container", value: container)
                        }
                        if let codec = info.videoCodec {
                            LabeledContent("Video", value: codec)
                        }
                        if !info.audioCodecs.isEmpty {
                            LabeledContent("Audio", value: info.audioCodecs.joined(separator: ", "))
                        }
                        if let width = info.width, let height = info.height {
                            LabeledContent("Resolution", value: "\(width) x \(height)")
                        }
                        if let bytes = info.fileBytes {
                            LabeledContent("File size", value: ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file))
                        }
                    }
                    .navigationTitle("Media Info")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { showMediaInfo = false }
                        }
                    }
                }
                .presentationDetents([.medium])
            }
        }
    }
}

private struct PhoneAVPlayerController: UIViewControllerRepresentable {
    let pass: LiveTestPass
    let audioLanguage: String
    let subtitlesAlways: Bool
    let subtitleLanguage: String
    let resumeWriter: LiveResumeWriter?
    @Binding var position: Double
    @Binding var duration: Double
    @Binding var seekRequest: Double?
    let onDismiss: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(
            position: $position,
            duration: $duration,
            onDismiss: onDismiss
        )
    }

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        try? AVAudioSession.sharedInstance().setActive(true)
        let controller = AVPlayerViewController()
        controller.allowsPictureInPicturePlayback = true
        controller.canStartPictureInPictureAutomaticallyFromInline = true
        let player = AVPlayer()
        player.allowsExternalPlayback = true
        controller.player = player
        context.coordinator.install(
            pass: pass,
            audioLanguage: audioLanguage,
            subtitlesAlways: subtitlesAlways,
            subtitleLanguage: subtitleLanguage,
            resumeWriter: resumeWriter,
            on: player
        )
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {
        guard let player = controller.player else { return }
        if context.coordinator.installedMediaID != pass.mediaID {
            context.coordinator.install(
                pass: pass,
                audioLanguage: audioLanguage,
                subtitlesAlways: subtitlesAlways,
                subtitleLanguage: subtitleLanguage,
                resumeWriter: resumeWriter,
                on: player
            )
        }
        if let target = seekRequest {
            Task { @MainActor in
                seekRequest = nil
                _ = await player.seek(to: CMTime(seconds: target, preferredTimescale: 600))
            }
        }
    }

    static func dismantleUIViewController(_ controller: AVPlayerViewController, coordinator: Coordinator) {
        coordinator.teardown(player: controller.player)
        controller.player?.pause()
        controller.player = nil
    }

    @MainActor
    final class Coordinator {
        private var position: Binding<Double>
        private var duration: Binding<Double>
        private let onDismiss: () -> Void
        private(set) var installedMediaID: String?
        private var timeObserver: Any?
        private var statusObserver: NSKeyValueObservation?
        private var endObserver: NSObjectProtocol?
        private var lastCheckpointAt: Date = .distantPast
        private var didDismiss = false

        init(position: Binding<Double>, duration: Binding<Double>, onDismiss: @escaping () -> Void) {
            self.position = position
            self.duration = duration
            self.onDismiss = onDismiss
        }

        func install(
            pass: LiveTestPass,
            audioLanguage: String,
            subtitlesAlways: Bool,
            subtitleLanguage: String,
            resumeWriter: LiveResumeWriter?,
            on player: AVPlayer
        ) {
            teardown(player: player)
            installedMediaID = pass.mediaID
            didDismiss = false
            for cookie in pass.mediaCookies {
                HTTPCookieStorage.shared.setCookie(cookie)
            }
            let resumeAt = pass.position > 0 && pass.position < pass.duration - 5 ? pass.position : nil
            let mediaURL = pass.mediaURL(forResumeAt: resumeAt)
            let options: [String: Any]? = pass.mediaCookies.isEmpty ? nil : [AVURLAssetHTTPCookiesKey: pass.mediaCookies]
            let item = AVPlayerItem(asset: AVURLAsset(url: mediaURL, options: options))

            if let resumeAt, mediaURL.pathExtension.lowercased() == "m3u8" {
                item.seek(to: CMTime(seconds: resumeAt, preferredTimescale: 600),
                          toleranceBefore: .zero, toleranceAfter: .zero, completionHandler: nil)
            }

            player.replaceCurrentItem(with: item)
            statusObserver = item.observe(\.status, options: [.new]) { [weak self, weak player, weak item] observed, _ in
                guard observed.status == .readyToPlay, let self, let player, let item else { return }
                Task { @MainActor in
                    await self.applyTrackDefaults(
                        to: item,
                        audioLanguage: audioLanguage,
                        subtitlesAlways: subtitlesAlways,
                        subtitleLanguage: subtitleLanguage
                    )
                    if let resumeAt, mediaURL.pathExtension.lowercased() != "m3u8" {
                        _ = await player.seek(to: CMTime(seconds: resumeAt, preferredTimescale: 600))
                    }
                    player.play()
                }
            }

            timeObserver = player.addPeriodicTimeObserver(
                forInterval: CMTime(seconds: 1, preferredTimescale: 600),
                queue: .main
            ) { [weak self, weak item] time in
                guard let self else { return }
                let sec = time.seconds
                let total = item?.duration.seconds ?? pass.duration
                Task { @MainActor in
                    if sec.isFinite { self.position.wrappedValue = max(0, sec) }
                    if total.isFinite && total > 0 { self.duration.wrappedValue = total }
                    if sec.isFinite && Date().timeIntervalSince(self.lastCheckpointAt) >= 5 {
                        self.lastCheckpointAt = Date()
                        resumeWriter?.offer(max(0, sec))
                    }
                }
            }

            endObserver = NotificationCenter.default.addObserver(
                forName: .AVPlayerItemDidPlayToEndTime,
                object: item,
                queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    guard let self, !self.didDismiss else { return }
                    let finishPos = max(pass.duration, self.duration.wrappedValue)
                    resumeWriter?.offer(finishPos)
                    self.didDismiss = true
                    self.onDismiss()
                }
            }
        }

        func teardown(player: AVPlayer?) {
            if let timeObserver, let player {
                player.removeTimeObserver(timeObserver)
            }
            timeObserver = nil
            statusObserver?.invalidate()
            statusObserver = nil
            if let endObserver {
                NotificationCenter.default.removeObserver(endObserver)
            }
            endObserver = nil
        }

        private func applyTrackDefaults(
            to item: AVPlayerItem,
            audioLanguage: String,
            subtitlesAlways: Bool,
            subtitleLanguage: String
        ) async {
            if audioLanguage != "auto",
               let audioGroup = try? await item.asset.loadMediaSelectionGroup(for: .audible),
               let match = audioGroup.options.first(where: {
                   $0.extendedLanguageTag?.lowercased().hasPrefix(audioLanguage) == true
               }) {
                item.select(match, in: audioGroup)
            }
            if let subGroup = try? await item.asset.loadMediaSelectionGroup(for: .legible) {
                if subtitlesAlways {
                    let lang = subtitleLanguage == "auto" ? "en" : subtitleLanguage
                    if let match = subGroup.options.first(where: {
                        $0.extendedLanguageTag?.lowercased().hasPrefix(lang) == true
                    }) ?? subGroup.options.first {
                        item.select(match, in: subGroup)
                    }
                } else if subGroup.allowsEmptySelection {
                    item.select(nil, in: subGroup)
                }
            }
        }
    }
}
