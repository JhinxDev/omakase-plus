import QualificationCore
import SwiftUI

@MainActor
final class VLCPlayerEngine {
    static let tracks: [EngineTrack] = [
        EngineTrack(id: "audio:0", kind: .audio, language: "en", name: "Original Stereo"),
        EngineTrack(id: "audio:1", kind: .audio, language: "fr", name: "Alternate Stereo"),
    ] + ["en", "en", "fr", "es", "pt", "de", "it", "ja", "ko", "pl", "tr", "ar"]
        .enumerated().map { index, language in
            let number = String(format: "%02d", index + 1)
            return EngineTrack(
                id: "subtitle:\(index)", kind: .subtitle, language: language,
                name: "Fixture \(number) Original-language subtitles including translator notes, commentary and descriptive closed captions"
            )
        }

    private var selections: [TrackKind: String] = [.audio: "audio:0", .subtitle: "subtitle:0"]

    func selectedTrackID(kind: TrackKind) -> String? { selections[kind] }

    func selectTrack(kind: TrackKind, id: String?) -> Bool {
        if let id, !Self.tracks.contains(where: { $0.kind == kind && $0.id == id }) { return false }
        selections[kind] = id
        return true
    }
}

struct VLCPlayerView: View {
    let engine: VLCPlayerEngine

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.13, green: 0.19, blue: 0.24), .black],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
            Text("Synthetic direct playback")
                .font(.system(size: 52, weight: .semibold))
                .foregroundStyle(.white.opacity(0.82))
        }
    }
}
