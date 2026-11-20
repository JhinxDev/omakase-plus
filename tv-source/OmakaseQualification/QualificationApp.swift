import SwiftUI
import QualificationCore

@main
struct QualificationApp: App {
    var body: some Scene {
        WindowGroup { SampleList().preferredColorScheme(.dark) }
    }
}

@MainActor
struct SampleList: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var session = PlaybackSession()
    private let samples: [QualificationSample]
    private let configStatus: String

    init() {
        // Local read only. No AVAsset, player or URL request is created here.
        let baseline = Bundle.main.url(forResource: "baseline", withExtension: "mp4", subdirectory: "SampleMedia/Resources")
            .flatMap { QualificationSample.bundledBaseline(resourceURL: $0, fingerprint: "7d649985878059299c7ba7010600c4c9b9aca07e7320169b1f10547111e6d3c0") }
        var rows = baseline.map { [$0] } ?? []
        guard let path = Bundle.main.url(forResource: "qualification.local", withExtension: "json", subdirectory: "SampleMedia/Resources") else {
            samples = rows
            configStatus = baseline == nil ? "Bundled baseline missing. Check the Resources build phase." : "Start with the silent synthetic controls test. Optional real-media samples need Resources/qualification.local.json and a rebuild."
            return
        }
        do {
            let config = try QualificationConfig.decode(Data(contentsOf: path))
            guard !config.samples.contains(where: { $0.id == "synthetic-baseline" }) else { throw ConfigurationError.duplicateID }
            rows.append(contentsOf: config.samples)
            configStatus = "Select a sample to review its expectations. Playback starts only with Play or Resume."
        } catch let code as ConfigurationError {
            configStatus = "Optional config rejected: \(code). The bundled controls test remains available."
        } catch {
            configStatus = "Optional config unreadable. The bundled controls test remains available."
        }
        samples = rows
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 24) {
                Text("Omakase Player Qualification").font(.largeTitle.bold())
                Text("AVKit only. Playback is not accepted until the physical-TV matrix is reviewed.")
                    .font(.title3).foregroundStyle(.yellow)
                Text(configStatus).font(.body)
                if !samples.isEmpty {
                    List(samples, id: \.id) { sample in
                        NavigationLink {
                            SampleDetail(sample: sample, session: session)
                        } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(sample.title).font(.title3)
                                Text("\(sample.id) | Expected HDR: \(sample.expectedHDR.rawValue)")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 12)
                        }
                    }
                }
            }
            .padding(64)
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            session.scenePhaseChanged(AppScenePhase(phase))
        }
    }
}

@MainActor
struct SampleDetail: View {
    let sample: QualificationSample
    @ObservedObject var session: PlaybackSession
    @State private var showingPlayer = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                Text(sample.title).font(.largeTitle.bold())
                Text("Sample ID: \(sample.id)")
                Text("Expected HDR: \(sample.expectedHDR.rawValue)")
                Text("Expected subtitles: \(sample.expectedSubtitles.isEmpty ? "None specified" : sample.expectedSubtitles.joined(separator: ", "))")
                Text("Use the standard player menu for subtitle/audio selection and the standard remote controls for scrubbing.")
                Text("Expectations are operator labels, not detected capabilities. Check actual display output and subtitle rendering on the TV.")
                    .foregroundStyle(.secondary)
                HStack(spacing: 32) {
                    Button("Play from start") { start(resume: false) }
                    if let record = session.resumeRecord(for: sample), !record.reachedEnd, record.position > 0 {
                        Button("Resume at \(PlaybackSession.clock(record.position))") { start(resume: true) }
                    }
                }
                if let record = session.resumeRecord(for: sample) {
                    Text("Saved locally: \(PlaybackSession.clock(record.position)) / \(PlaybackSession.clock(record.duration)). \(record.reachedEnd ? "End reached; human review remains pending." : "Partial progress only.")")
                }
                Text("Status: \(session.status)")
                Text("Last position: \(PlaybackSession.clock(session.position)) / \(PlaybackSession.clock(session.duration))")
                    .monospacedDigit()
                Text("No watched state, tracker, Omakase backend, analytics or report export. Local resume data expires after 30 days.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(64)
        }
        .fullScreenCover(isPresented: $showingPlayer, onDismiss: { session.stop() }) {
            PlayerScreen(session: session) {
                session.stop()
                showingPlayer = false
            }
        }
    }

    private func start(resume: Bool) {
        if session.begin(sample: sample, resume: resume) { showingPlayer = true }
    }
}
