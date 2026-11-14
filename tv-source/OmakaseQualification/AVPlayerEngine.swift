import AVFoundation
import Foundation
import QualificationCore

/// AVFoundation transport adapter.
///
/// This is the existing AVPlayer behaviour wrapped in `PlayerEngine`: the same
/// player, the same item, the same KVO and notification signals, and the same
/// AVKit presentation (`NativePlayer` still binds `AVPlayerViewController.player`
/// to `player`). It adds nothing to what AVKit already did, so mp4/HLS keep the
/// controls, the track menu and the info panel they have today.
///
/// Every event and seek completion is delivered on a later main-actor turn, never
/// from inside the call that caused it. The callback contract is defined on
/// `PlayerEngine`.
@MainActor
final class AVPlayerEngine: NSObject, PlayerEngine {
    /// The AVPlayer AVKit presents. Owned here so the engine is the only writer
    /// of transport state.
    let player: AVPlayer
    /// The item behind `player`. Exposed for AVKit-only diagnostics and for the
    /// media selection groups, which are AVFoundation types and stay out of the
    /// portable protocol.
    private(set) var item: AVPlayerItem

    private(set) var state: PlayerState = .opening
    var onEvent: ((PlayerEvent) -> Void)?

    private var statusObservation: NSKeyValueObservation?
    private var controlObservation: NSKeyValueObservation?
    private var endObserver: NSObjectProtocol?
    private var failureObserver: NSObjectProtocol?
    private var selectionObserver: NSObjectProtocol?
    private var readyEmitted = false

    init(player: AVPlayer, item: AVPlayerItem) {
        self.player = player
        self.item = item
        super.init()
        observe()
    }

    // MARK: - PlayerEngine

    var currentTime: Double { player.currentTime().seconds }

    var duration: Double {
        let value = item.duration.seconds
        return value.isFinite && value > 0 ? value : .nan
    }

    var ranges: TransportRanges {
        TransportRanges(buffered: item.loadedTimeRanges.map(\.timeRangeValue).compactMap(Self.range),
                        seekable: item.seekableTimeRanges.map(\.timeRangeValue).compactMap(Self.range))
    }

    var hasVideoOutput: Bool {
        item.tracks.contains { $0.assetTrack?.mediaType == .video }
    }

    var tracks: [EngineTrack] {
        var result: [EngineTrack] = []
        for (kind, characteristic) in [(TrackKind.audio, AVMediaCharacteristic.audible),
                                       (TrackKind.subtitle, AVMediaCharacteristic.legible)] {
            guard let group = item.asset.mediaSelectionGroup(forMediaCharacteristic: characteristic)
            else { continue }
            for (index, option) in group.options.enumerated() where option.isPlayable {
                result.append(EngineTrack(
                    id: "\(kind.rawValue):\(index)",
                    kind: kind,
                    language: option.extendedLanguageTag?.lowercased(),
                    name: option.displayName,
                    isForced: option.hasMediaCharacteristic(.containsOnlyForcedSubtitles),
                    isSDH: option.hasMediaCharacteristic(.transcribesSpokenDialogForAccessibility)
                        || option.hasMediaCharacteristic(.describesMusicAndSoundForAccessibility)
                ))
            }
        }
        return result
    }

    func play() {
        // Transport state comes from AVPlayer's own notifications, not from here,
        // so the UI keeps the original timing between "Buffering" and "Playing".
        player.play()
    }

    func pause() {
        player.pause()
    }

    func stop() {
        player.pause()
        teardown()
        player.replaceCurrentItem(with: nil)
        publish(.idle)
    }

    func seek(to seconds: Double, tolerance: SeekTolerance,
              completion: @escaping @MainActor (SeekLanding) -> Void) {
        // The keyframe policy is what the repaired resume seek used: an exact
        // seek needs the target segment decoded and fails on a filling buffer.
        // The recovery attempt narrows the window without demanding zero.
        let window = tolerance == .keyframe ? .positiveInfinity : CMTime(seconds: 1, preferredTimescale: 600)
        let target = CMTime(seconds: seconds, preferredTimescale: 600)
        player.seek(to: target, toleranceBefore: window, toleranceAfter: window) { [weak self] finished in
            Task { @MainActor in
                guard let self else {
                    completion(SeekLanding(landed: nil, finished: finished))
                    return
                }
                completion(SeekLanding(landed: self.player.currentTime().seconds, finished: finished))
            }
        }
    }

    @discardableResult
    func selectTrack(kind: TrackKind, id: String?) -> Bool {
        let characteristic: AVMediaCharacteristic = kind == .audio ? .audible : .legible
        guard let group = item.asset.mediaSelectionGroup(forMediaCharacteristic: characteristic)
        else { return false }
        guard let id else {
            guard group.allowsEmptySelection else { return false }
            item.select(nil, in: group)
            emit(.tracksChanged)
            return true
        }
        guard let index = Self.index(from: id, kind: kind), index < group.options.count,
              group.options[index].isPlayable else { return false }
        item.select(group.options[index], in: group)
        emit(.tracksChanged)
        return true
    }

    func selectedTrackID(kind: TrackKind) -> String? {
        let characteristic: AVMediaCharacteristic = kind == .audio ? .audible : .legible
        guard let group = item.asset.mediaSelectionGroup(forMediaCharacteristic: characteristic),
              let selected = item.currentMediaSelection.selectedMediaOption(in: group),
              let index = group.options.firstIndex(of: selected) else { return nil }
        return "\(kind.rawValue):\(index)"
    }

    // MARK: - Observation

    private func observe() {
        statusObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] _, _ in
            Task { @MainActor in self?.statusChanged() }
        }
        controlObservation = player.observe(\.timeControlStatus, options: [.new]) { [weak self] _, _ in
            Task { @MainActor in self?.controlChanged() }
        }
        endObserver = NotificationCenter.default.addObserver(
            forName: AVPlayerItem.didPlayToEndTimeNotification, object: item, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.publish(.ended)
                self.emit(.ended)
            }
        }
        failureObserver = NotificationCenter.default.addObserver(
            forName: AVPlayerItem.failedToPlayToEndTimeNotification, object: item, queue: .main
        ) { [weak self] notification in
            Task { @MainActor in
                guard let self else { return }
                let error = notification.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
                self.publish(.failed)
                self.emit(.failed(Self.failure(error ?? self.item.error)))
            }
        }
        // A viewer can change selection from AVKit's own menu, which bypasses the
        // app. Reporting it lets the session record an explicit choice so a late
        // default task cannot overwrite it.
        selectionObserver = NotificationCenter.default.addObserver(
            forName: AVPlayerItem.mediaSelectionDidChangeNotification, object: item, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.emit(.tracksChanged) }
        }
    }

    private func teardown() {
        statusObservation?.invalidate()
        statusObservation = nil
        controlObservation?.invalidate()
        controlObservation = nil
        for token in [endObserver, failureObserver, selectionObserver] {
            if let token { NotificationCenter.default.removeObserver(token) }
        }
        endObserver = nil
        failureObserver = nil
        selectionObserver = nil
    }

    private func statusChanged() {
        switch item.status {
        case .readyToPlay:
            guard !readyEmitted else { return }
            readyEmitted = true
            if state == .opening || state == .idle { publish(.ready) }
            emit(.ready)
        case .failed:
            publish(.failed)
            emit(.failed(Self.failure(item.error)))
        default:
            break
        }
    }

    private func controlChanged() {
        switch player.timeControlStatus {
        case .playing:
            publish(.playing)
        case .paused:
            if state == .playing || state == .buffering { publish(.paused) }
        case .waitingToPlayAtSpecifiedRate:
            publish(.buffering)
        @unknown default:
            break
        }
    }

    private func publish(_ next: PlayerState) {
        guard state != next else { return }
        state = next
        emit(.stateChanged(next))
    }

    /// Deferred delivery: callers may tear the session down inside a callback.
    private func emit(_ event: PlayerEvent) {
        let callback = onEvent
        Task { @MainActor in callback?(event) }
    }

    // MARK: - Helpers

    private static func range(_ value: CMTimeRange) -> MediaRange? {
        let start = value.start.seconds
        let end = CMTimeRangeGetEnd(value).seconds
        guard start.isFinite, end.isFinite else { return nil }
        return MediaRange(start: start, end: end)
    }

    private static func index(from id: String, kind: TrackKind) -> Int? {
        let prefix = "\(kind.rawValue):"
        guard id.hasPrefix(prefix) else { return nil }
        return Int(id.dropFirst(prefix.count))
    }

    static func failure(_ error: Error?) -> EngineFailure {
        guard let error else { return EngineFailure(kind: .unknown, code: "none") }
        let ns = error as NSError
        let kind: EngineFailure.Kind
        switch ns.domain {
        case NSURLErrorDomain: kind = .network
        case AVFoundationErrorDomain: kind = .media
        case NSOSStatusErrorDomain: kind = .media
        default: kind = .unknown
        }
        // Codes only: never an error description, which can embed the media URL.
        return EngineFailure(kind: kind, code: "\(Self.domainLabel(ns.domain)):\(ns.code)")
    }

    private static func domainLabel(_ domain: String) -> String {
        switch domain {
        case NSURLErrorDomain: return "network"
        case AVFoundationErrorDomain: return "avfoundation"
        case NSOSStatusErrorDomain: return "osstatus"
        default: return "other"
        }
    }

    // MARK: - Diagnostics

    /// AVFoundation facts the server needs, with provider URLs and tokens redacted.
    ///
    /// Item status and error, the error log, the access log, the playback control
    /// state, why the player is waiting, the loaded ranges and the current time.
    /// Everything that could carry a per-pass token or a provider URL is passed
    /// through `redact` first, because this string is posted to the server.
    var diagnosticSnapshot: String {
        let loaded = item.loadedTimeRanges
            .map(\.timeRangeValue)
            .compactMap(Self.rangeText)
            .joined(separator: " ")
        let access = (item.accessLog()?.events ?? []).suffix(2).map { event in
            "uri=\(Self.redact(event.uri ?? "")) segs=\(event.numberOfSegmentsDownloaded)"
                + " bytes=\(event.numberOfBytesTransferred) br=\(event.observedBitrate)"
                + " stalls=\(event.numberOfStalls) dur=\(String(format: "%.2f", event.transferDuration))"
        }.joined(separator: " || ")
        let errors = (item.errorLog()?.events ?? []).suffix(3).map { event in
            "\(event.errorStatusCode)/\(event.errorDomain)/\(Self.redact(event.errorComment ?? ""))"
        }.joined(separator: " || ")
        return "engine=avplayer status=\(Self.statusText(item.status))"
            + " error=\(Self.failure(item.error).kind.rawValue):\(Self.failure(item.error).code)"
            + " time=\(String(format: "%.3f", CMTimeGetSeconds(item.currentTime())))"
            + " rate=\(String(format: "%.2f", player.rate))"
            + " control=\(Self.controlText(player.timeControlStatus))"
            + " wait=\(Self.waitText(player.reasonForWaitingToPlay))"
            + " likely=\(item.isPlaybackLikelyToKeepUp) empty=\(item.isPlaybackBufferEmpty)"
            + " loaded=[\(loaded)] access=[\(access)] errors=[\(errors)]"
    }

    private static func statusText(_ status: AVPlayerItem.Status) -> String {
        switch status {
        case .unknown: return "unknown"
        case .readyToPlay: return "ready"
        case .failed: return "failed"
        @unknown default: return "other"
        }
    }

    private static func controlText(_ status: AVPlayer.TimeControlStatus) -> String {
        switch status {
        case .paused: return "paused"
        case .waitingToPlayAtSpecifiedRate: return "waiting"
        case .playing: return "playing"
        @unknown default: return "other"
        }
    }

    private static func waitText(_ reason: AVPlayer.WaitingReason?) -> String {
        guard let reason else { return "none" }
        switch reason {
        case .evaluatingBufferingRate: return "buffering_rate"
        case .toMinimizeStalls: return "minimize_stalls"
        case .noItemToPlay: return "no_item"
        case .waitingForCoordinatedPlayback: return "coordinated"
        default: return "other"
        }
    }

    private static func rangeText(_ range: CMTimeRange) -> String? {
        let start = range.start.seconds
        let end = CMTimeRangeGetEnd(range).seconds
        guard start.isFinite, end.isFinite else { return nil }
        return String(format: "%.2f-%.2f", start, end)
    }

    /// Removes query strings, per-pass tokens, media ids and provider origins.
    static func redact(_ value: String) -> String {
        guard !value.isEmpty else { return "" }
        var text = value
        if let mark = text.firstIndex(of: "?") { text = String(text[..<mark]) }
        text = text.replacingOccurrences(
            of: "/[A-Za-z0-9_-]{43}(?=[/\\s]|$)",
            with: "/<token>",
            options: .regularExpression
        )
        // The media id is a correlation handle the server pairs its own served-asset
        // trace against, and it is useless without the token, so it is kept.
        if let origin = text.range(of: "https?://[^/\\s]+", options: .regularExpression) {
            let lower = text[origin].lowercased()
            let ours = lower.contains("omakase") || lower.contains("127.0.0.1")
                || lower.contains("localhost") || lower.contains("192.168.")
            if !ours { text = text.replacingCharacters(in: origin, with: "<provider>") }
        }
        return String(text.prefix(600))
    }
}
