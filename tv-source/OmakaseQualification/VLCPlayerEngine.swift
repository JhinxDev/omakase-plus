import AVFoundation
import Foundation
import QualificationCore
import SwiftUI
import UIKit

struct DirectMediaTracks {
    var width: Int? = nil
    var height: Int? = nil
    var video: String? = nil
    var audio: [String] = []
}

#if canImport(TVVLCKit)
import TVVLCKit
#elseif OMAKASE_REQUIRE_TVVLCKIT
#error("TVVLCKit must be present in the tvOS app targets. Run scripts/build-tvos.sh.")
#endif

// TVVLCKit integration notes
// ==========================
//
// Pinned dependency: TVVLCKit 3.7.3, artifact
//   https://download.videolan.org/pub/cocoapods/prod/TVVLCKit-3.7.3-319ed2c0-79128878.tar.xz
//   SHA256 b5f90c226ed54d9dc1c03901c60dc7749b74a53caace2c3047e4c0b7a063e46c
//   (the CocoaPods trunk spec for 3.7.3 declares the same hash)
//
// The build links and embeds the pinned dynamic framework in both tvOS targets.
// See docs/apple-tv.md for the dependency and distribution details.
//
// Every symbol used below was verified against the headers shipped inside that
// exact archive (VLCMediaPlayer.h, VLCMedia.h, VLCTime.h) and the published API
// documentation at https://videolan.videolan.me/VLCKit/interface_v_l_c_media_player.html
//
#if canImport(TVVLCKit)

/// TVVLCKit transport adapter for provider originals (MKV/H.264/HEVC with
/// embedded ASS/PGS subtitles and alternate audio streams).
///
/// VLCKit has no per-request header API beyond the two libvlc HTTP options below,
/// so a server-supplied `Authorization` header cannot be attached here. That is
/// reported as `unsupported` rather than dropped: a credential must never be
/// ignored silently. A signed range URL (token in the URL) needs no headers and
/// is the supported path.
@MainActor
final class VLCPlayerEngine: NSObject, PlayerEngine, VLCMediaPlayerDelegate {
    /// The UIView VLCKit renders into. Owned by the engine so the SwiftUI layer
    /// only has to wrap it.
    let videoView: UIView

    private(set) var state: PlayerState = .idle
    var onEvent: ((PlayerEvent) -> Void)?

    private let player: VLCMediaPlayer
    private let media: VLCMedia
    private var readyEmitted = false
    private var lastKnownTrackCount = 0
    private var pendingSeek: PendingSeek?

    private struct PendingSeek {
        let id: UUID
        let target: Double
        let completion: @MainActor (SeekLanding) -> Void
    }

    init(url: URL, headers: [String: String]) throws {
        let options = try Self.options(for: headers)
        let media = VLCMedia(url: url)
        for option in options { media.addOption(option) }
        let player = VLCMediaPlayer(options: [])
        let view = UIView(frame: .zero)
        view.backgroundColor = .black
        player.drawable = view
        self.media = media
        self.player = player
        self.videoView = view
        super.init()
        player.media = media
        player.delegate = self
    }

    // MARK: - PlayerEngine

    var mediaTracks: DirectMediaTracks {
        guard let rows = media.tracksInformation as? [[String: Any]] else { return .init() }
        func fourCC(_ row: [String: Any]) -> String? {
            guard let code = row[VLCMediaTracksInformationCodec] as? NSNumber else { return nil }
            let value = code.uint32Value
            let bytes = (0..<4).map { UInt8(truncatingIfNeeded: value >> ($0 * 8)) }
            guard bytes.allSatisfy({ (32...126).contains($0) }) else { return nil }
            return String(bytes: bytes, encoding: .ascii)?.trimmingCharacters(in: .whitespaces)
        }
        let video = rows.first { $0[VLCMediaTracksInformationType] as? String == VLCMediaTracksInformationTypeVideo }
        let audio = rows.filter { $0[VLCMediaTracksInformationType] as? String == VLCMediaTracksInformationTypeAudio }
        let width = (video?[VLCMediaTracksInformationVideoWidth] as? NSNumber)?.intValue
        let height = (video?[VLCMediaTracksInformationVideoHeight] as? NSNumber)?.intValue
        return DirectMediaTracks(width: width, height: height, video: video.flatMap(fourCC),
                                 audio: Array(audio.prefix(4).compactMap(fourCC)))
    }

    var currentTime: Double {
        let milliseconds = Double(player.time.intValue)
        guard milliseconds.isFinite, milliseconds >= 0 else { return .nan }
        return milliseconds / 1000
    }

    var duration: Double {
        let milliseconds = Double(media.length.intValue)
        guard milliseconds.isFinite, milliseconds > 0 else { return .nan }
        return milliseconds / 1000
    }

    var ranges: TransportRanges {
        // VLCKit exposes seekability but not time ranges. Describing the whole
        // item as seekable when libVLC says the input is seekable is honest
        // enough for the resume decision; buffered ranges stay unknown.
        guard player.isSeekable, duration.isFinite, duration > 0 else { return TransportRanges() }
        return TransportRanges(seekable: [MediaRange(start: 0, end: duration)])
    }

    var hasVideoOutput: Bool {
        player.hasVideoOut || player.numberOfVideoTracks > 0
    }

    var tracks: [EngineTrack] {
        var result: [EngineTrack] = []
        let details = trackDetails()
        result.append(contentsOf: Self.engineTracks(kind: .audio,
                                                    names: player.audioTrackNames as? [String] ?? [],
                                                    indexes: player.audioTrackIndexes as? [NSNumber] ?? [],
                                                    details: details))
        result.append(contentsOf: Self.engineTracks(kind: .subtitle,
                                                    names: player.videoSubTitlesNames as? [String] ?? [],
                                                    indexes: player.videoSubTitlesIndexes as? [NSNumber] ?? [],
                                                    details: details))
        return result
    }

    func play() {
        // libVLC reports transport changes through the delegate; state is not
        // invented here.
        player.play()
    }

    func pause() {
        player.pause()
    }

    func stop() {
        finishPendingSeek()
        player.stop()
        player.delegate = nil
        player.drawable = nil
        publish(.idle)
    }

    func seek(to seconds: Double, tolerance: SeekTolerance,
              completion: @escaping @MainActor (SeekLanding) -> Void) {
        guard seconds.isFinite, seconds >= 0 else {
            Task { @MainActor in completion(SeekLanding(landed: nil, finished: false)) }
            return
        }
        // libVLC has no tolerance parameter: it decodes from the nearest sync
        // point, which is the keyframe policy. Both policies therefore issue the
        // same seek; the validator still decides whether the landing is usable.
        _ = tolerance
        finishPendingSeek()
        let pending = PendingSeek(id: UUID(), target: seconds, completion: completion)
        self.pendingSeek = pending
        if duration.isFinite, duration > 0 {
            player.position = Float(min(max(seconds / duration, 0), 1))
        } else {
            player.time = VLCTime(int: Int32(min(seconds, 86_400) * 1000))
        }
        // A landing may never be reported (unseekable input, closed item), so
        // complete the request after a bounded wait instead of hanging it.
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard let self, self.pendingSeek?.id == pending.id else { return }
            self.finishPendingSeek()
        }
    }

    @discardableResult
    func selectTrack(kind: TrackKind, id: String?) -> Bool {
        let index: Int
        if let id {
            guard let parsed = Self.index(from: id, kind: kind), (0...Int(Int32.max)).contains(parsed)
            else { return false }
            index = parsed
        } else {
            index = -1
        }
        if selectedTrackID(kind: kind) == id { return true }
        let available = kind == .audio ? player.audioTrackIndexes as? [NSNumber]
                                       : player.videoSubTitlesIndexes as? [NSNumber]
        if index >= 0, !(available ?? []).contains(where: { $0.intValue == index }) { return false }
        switch kind {
        case .audio: player.currentAudioTrackIndex = Int32(index)
        case .subtitle: player.currentVideoSubTitleIndex = Int32(index)
        }
        return true
    }

    func selectedTrackID(kind: TrackKind) -> String? {
        let index = kind == .audio ? player.currentAudioTrackIndex : player.currentVideoSubTitleIndex
        guard index >= 0 else { return nil }
        return "\(kind.rawValue):\(index)"
    }

    // MARK: - VLCMediaPlayerDelegate

    nonisolated func mediaPlayerStateChanged(_ aNotification: Notification) {
        Task { @MainActor [weak self] in self?.stateUpdated() }
    }

    nonisolated func mediaPlayerTimeChanged(_ aNotification: Notification) {
        Task { @MainActor [weak self] in self?.landingUpdated() }
    }

    // MARK: - Private

    private func stateUpdated() {
        let current = player.state
        if current == .playing || current == .buffering {
            if !readyEmitted {
                readyEmitted = true
                publish(.ready)
                emit(.ready)
            }
            publish(current == .playing ? .playing : .buffering)
            if tracks.count != lastKnownTrackCount {
                lastKnownTrackCount = tracks.count
                emit(.tracksChanged)
            }
        } else if current == .ended {
            publish(.ended)
            emit(.ended)
        } else if current == .error {
            publish(.failed)
            emit(.failed(EngineFailure(kind: .media, code: "vlc-error")))
        } else if current == .paused {
            publish(.paused)
        } else if current == .opening {
            publish(.opening)
        } else if current == .stopped {
            publish(.idle)
        }
        // `.esAdded` is an elementary-stream notification, not a transport state.
    }

    private func landingUpdated() {
        guard let pending = pendingSeek else { return }
        let now = currentTime
        guard now.isFinite else { return }
        if abs(now - pending.target) <= 5 { finishPendingSeek() }
    }

    private func finishPendingSeek() {
        guard let pending = pendingSeek else { return }
        pendingSeek = nil
        let landed = currentTime
        // libVLC has no seek-completion flag. A position is the only evidence,
        // and the validator decides whether it is usable.
        let finished = landed.isFinite
        Task { @MainActor in
            pending.completion(SeekLanding(landed: landed.isFinite ? landed : nil, finished: finished))
        }
    }

    private func publish(_ next: PlayerState) {
        guard state != next else { return }
        state = next
        emit(.stateChanged(next))
    }

    private func emit(_ event: PlayerEvent) {
        let callback = onEvent
        Task { @MainActor in callback?(event) }
    }

    /// Languages and role hints per container track id, when the container
    /// publishes them. Falls back to name-based hints in `engineTracks`.
    private func trackDetails() -> [Int: (language: String?, forced: Bool, sdh: Bool)] {
        var result: [Int: (language: String?, forced: Bool, sdh: Bool)] = [:]
        guard let rows = media.tracksInformation as? [[String: Any]] else { return result }
        for row in rows {
            guard let id = (row[VLCMediaTracksInformationId] as? NSNumber)?.intValue else { continue }
            let language = (row[VLCMediaTracksInformationLanguage] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let description = row[VLCMediaTracksInformationDescription] as? String
            let hints = TrackSelector.roleHint(name: description)
            result[id] = (language?.isEmpty == false ? language : nil, hints.isForced, hints.isSDH)
        }
        return result
    }

    private static func engineTracks(kind: TrackKind, names: [String], indexes: [NSNumber],
                                     details: [Int: (language: String?, forced: Bool, sdh: Bool)]) -> [EngineTrack] {
        var result: [EngineTrack] = []
        for (offset, number) in indexes.enumerated() {
            let index = number.intValue
            // -1 is the "Disabled" pseudo track; libVLC also lists it first.
            guard index >= 0 else { continue }
            let name = offset < names.count ? names[offset] : nil
            let hints = TrackSelector.roleHint(name: name)
            let detail = details[index]
            result.append(EngineTrack(id: "\(kind.rawValue):\(index)", kind: kind,
                                      language: detail?.language ?? nil, name: name,
                                      isForced: detail?.forced ?? hints.isForced,
                                      isSDH: detail?.sdh ?? hints.isSDH))
        }
        return result
    }

    private static func index(from id: String, kind: TrackKind) -> Int? {
        let prefix = "\(kind.rawValue):"
        guard id.hasPrefix(prefix) else { return nil }
        return Int(id.dropFirst(prefix.count))
    }

    /// Maps server headers onto the only HTTP options libVLC 3 exposes.
    ///
    /// Anything else, `Authorization` included, is refused with `unsupported` so
    /// the app cannot end up playing an unauthenticated request by accident. No
    /// header value is ever logged.
    private static func options(for headers: [String: String]) throws -> [String] {
        var options: [String] = []
        for (name, value) in headers.sorted(by: { $0.key < $1.key }) {
            switch name.lowercased() {
            case "referer", "referrer":
                options.append(":http-referrer=\(value)")
            case "user-agent":
                options.append(":http-user-agent=\(value)")
            default:
                throw EngineFailure(kind: .unsupported, code: "header-not-supported")
            }
        }
        return options
    }
}

/// SwiftUI wrapper around VLCKit's drawable view. Transport commands come from
/// the session, not from AVKit's controls, which do not exist on this path.
struct VLCPlayerView: UIViewRepresentable {
    let engine: VLCPlayerEngine

    func makeUIView(context: Context) -> UIView { engine.videoView }

    func updateUIView(_ uiView: UIView, context: Context) {}
}

#else

/// Fallback for external builds that do not require the tvOS direct player.
@MainActor
final class VLCPlayerEngine: NSObject, PlayerEngine {
    var mediaTracks: DirectMediaTracks { .init() }
    static let unavailableReason = "TVVLCKit is not linked in this build."

    let videoView = UIView(frame: .zero)
    private(set) var state: PlayerState = .idle
    var onEvent: ((PlayerEvent) -> Void)?

    init(url: URL, headers: [String: String]) throws {
        throw EngineFailure(kind: .unsupported, code: "tvvlckit-missing")
    }

    var currentTime: Double { .nan }
    var duration: Double { .nan }
    var ranges: TransportRanges { TransportRanges() }
    var hasVideoOutput: Bool { false }
    var tracks: [EngineTrack] { [] }

    func play() {}
    func pause() {}
    func stop() {}

    func seek(to seconds: Double, tolerance: SeekTolerance,
              completion: @escaping @MainActor (SeekLanding) -> Void) {
        Task { @MainActor in completion(SeekLanding(landed: nil, finished: false)) }
    }

    @discardableResult
    func selectTrack(kind: TrackKind, id: String?) -> Bool { false }

    func selectedTrackID(kind: TrackKind) -> String? { nil }
}

struct VLCPlayerView: UIViewRepresentable {
    let engine: VLCPlayerEngine

    func makeUIView(context: Context) -> UIView { engine.videoView }

    func updateUIView(_ uiView: UIView, context: Context) {}
}

#endif
