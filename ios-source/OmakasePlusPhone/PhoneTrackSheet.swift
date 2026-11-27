import QualificationCore
import SafariServices
import SwiftUI

struct PhoneTrackSheet: View {
    @Bindable var store: PhoneSessionStore
    let client: LiveLibraryClient
    let kind: String
    let mediaID: Int?
    let imdbID: String?
    let title: String
    let initialStatus: String?
    let initialProgress: Int?
    let episodeTotal: Int?
    let initialRating: Double?
    let allowDismiss: Bool
    let onSaved: (LiveTrackResult) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var status: LiveTrackStatus
    @State private var progress: Int
    @State private var hasRating: Bool
    @State private var rating: Double
    @State private var saving = false
    @State private var errorMessage: String?

    init(
        store: PhoneSessionStore,
        client: LiveLibraryClient,
        kind: String,
        mediaID: Int?,
        imdbID: String? = nil,
        title: String,
        initialStatus: String? = nil,
        initialProgress: Int? = nil,
        episodeTotal: Int? = nil,
        initialRating: Double? = nil,
        allowDismiss: Bool = true,
        onSaved: @escaping (LiveTrackResult) -> Void
    ) {
        self.store = store
        self.client = client
        self.kind = kind
        self.mediaID = mediaID
        self.imdbID = imdbID
        self.title = title
        self.initialStatus = initialStatus
        self.initialProgress = initialProgress
        self.episodeTotal = episodeTotal
        self.initialRating = initialRating
        self.allowDismiss = allowDismiss
        self.onSaved = onSaved

        let validOptions = LiveTrackStatus.options(for: kind)
        let mappedInitial = initialStatus.flatMap(LiveTrackStatus.init(rawValue:))
        let chosen = (mappedInitial != nil && validOptions.contains(mappedInitial!))
            ? mappedInitial!
            : (validOptions.contains(.completed) ? .completed : validOptions[0])
        _status = State(initialValue: chosen)
        let defaultProg = initialProgress ?? ((chosen == .completed ? episodeTotal : nil) ?? 0)
        _progress = State(initialValue: max(0, defaultProg))
        _hasRating = State(initialValue: initialRating != nil)
        _rating = State(initialValue: initialRating ?? 8.0)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Watch status", selection: $status) {
                        ForEach(LiveTrackStatus.options(for: kind)) { option in
                            Text(option.label).tag(option)
                        }
                    }
                    .onChange(of: status) { _, next in
                        if next == .completed, let episodeTotal, progress < episodeTotal {
                            progress = episodeTotal
                        }
                    }

                    if kind == "anime" || kind == "tv" {
                        HStack {
                            Stepper(value: $progress, in: 0...(episodeTotal ?? 9999)) {
                                if let episodeTotal {
                                    Text("Episodes watched: \(progress) / \(episodeTotal)")
                                } else {
                                    Text("Episodes watched: \(progress)")
                                }
                            }
                            if let episodeTotal, progress != episodeTotal {
                                Button("All") { progress = episodeTotal }
                                    .font(.caption.weight(.semibold))
                                    .buttonStyle(.bordered)
                                    .tint(Color.appAccent)
                            }
                        }
                    }
                } header: {
                    Text(kind == "anime" ? "Omakase & AniList" : "Watch history")
                } footer: {
                    Text(
                        kind == "anime"
                            ? "Updates your Omakase history and syncs directly to AniList when connected."
                            : "Saves directly to your Omakase history without downloading anything."
                    )
                }

                Section("Rating") {
                    Toggle("Include rating", isOn: $hasRating)
                        .tint(Color.appAccent)
                    if hasRating {
                        HStack {
                            Text(String(format: "%.1f / 10", rating))
                                .font(.headline.monospacedDigit())
                                .foregroundStyle(Color.appAccent)
                            Spacer()
                            Stepper("", value: $rating, in: 1.0...10.0, step: 0.5)
                                .labelsHidden()
                        }
                    }
                }

                if allowDismiss {
                    Section {
                        Button(role: .destructive) {
                            Task { await submit(statusOverride: .notForMe) }
                        } label: {
                            Label("Not for me (hide from For You)", systemImage: "eye.slash")
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .disabled(saving)
                    }
                }

                if let errorMessage {
                    Section {
                        Text(errorMessage)
                            .foregroundStyle(Color.appWarning)
                            .font(.subheadline)
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Track & rate")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Saving…" : "Save") {
                        Task { await submit(statusOverride: nil) }
                    }
                    .disabled(saving)
                    .fontWeight(.semibold)
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func submit(statusOverride: LiveTrackStatus?) async {
        saving = true
        errorMessage = nil
        defer { saving = false }
        let targetStatus = statusOverride ?? status
        do {
            let result = try await client.trackTitle(
                kind: kind,
                mediaID: mediaID,
                imdbID: imdbID,
                title: title,
                status: targetStatus.rawValue,
                progress: (kind == "anime" && targetStatus != .notForMe) ? progress : nil,
                episodeTotal: (kind == "anime" && targetStatus != .notForMe) ? episodeTotal : nil,
                score: (hasRating && targetStatus != .notForMe) ? rating : nil
            )
            onSaved(result)
            dismiss()
        } catch FixtureConnectionError.unauthorized {
            store.handleUnauthorized()
            dismiss()
        } catch {
            errorMessage = "Could not save tracking update."
        }
    }
}

struct PhoneSafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let controller = SFSafariViewController(url: url)
        controller.preferredBarTintColor = UIColor(Color.appBackground)
        controller.preferredControlTintColor = UIColor(Color.appAccent)
        return controller
    }

    func updateUIViewController(_ uiViewController: SFSafariViewController, context: Context) {}
}
