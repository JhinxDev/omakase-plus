import AVFoundation
import QualificationCore
import SwiftUI

/// Drives production PlayerScreen without a media decoder in simulator-only builds.
@MainActor
final class PlaybackSession: ObservableObject {
    @Published var isPresenting = true
    @Published var status = "Playing"
    @Published var position: Double = 32
    @Published var isDirectPlaybackRequested = true
    @Published var directTracks: [EngineTrack] = VLCPlayerEngine.tracks

    let duration: Double = 150
    let canPlayNext = true
    let directEngine: VLCPlayerEngine? = VLCPlayerEngine()
    let player: AVPlayer? = nil

    func togglePlayPause() {
        isDirectPlaybackRequested.toggle()
        status = isDirectPlaybackRequested ? "Playing" : "Paused"
    }

    func skip(by delta: Double) {
        position = min(duration, max(0, position + delta))
    }

    func stop() {
        isPresenting = false
    }

    @discardableResult
    func selectTrack(kind: TrackKind, id: String?) -> Bool {
        guard let directEngine, directEngine.selectTrack(kind: kind, id: id) else { return false }
        objectWillChange.send()
        return true
    }

    static func clock(_ seconds: Double) -> String {
        "\(Int(seconds) / 60):" + String(format: "%02d", Int(seconds) % 60)
    }

    static func shouldSuggestNext(position: Double, duration: Double) -> Bool { false }
}

