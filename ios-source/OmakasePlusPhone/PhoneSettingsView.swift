import QualificationCore
import SwiftUI

struct PhoneSettingsView: View {
    @Bindable var store: PhoneSessionStore

    @State private var confirmDisconnect = false
    @State private var showAddProfile = false

    private let languages: [(code: String, label: String)] = [
        ("auto", "Automatic"),
        ("en", "English"),
        ("ja", "Japanese"),
        ("fr", "French"),
        ("es", "Spanish"),
    ]

    var body: some View {
        NavigationStack {
            List {
                if let profile = store.activeProfile {
                    Section("Connected Server") {
                        LabeledContent("Profile", value: profile.profile.name)
                        LabeledContent("Server", value: profile.serverAddress)
                    }
                    .listRowBackground(Color.appSurface)
                }

                if store.libraries.count > 1 {
                    Section("Active Library") {
                        Button {
                            store.selectLibrary(nil)
                        } label: {
                            HStack {
                                Text("Personal Library")
                                Spacer()
                                if store.selectedLibrary == nil {
                                    Image(systemName: "checkmark").foregroundStyle(Color.appAccent)
                                }
                            }
                        }
                        ForEach(store.libraries.filter { $0.kind == .household }) { option in
                            Button {
                                store.selectLibrary(option)
                            } label: {
                                HStack {
                                    Text(option.name)
                                    Spacer()
                                    if store.selectedLibrary?.id == option.id {
                                        Image(systemName: "checkmark").foregroundStyle(Color.appAccent)
                                    }
                                }
                            }
                        }
                    }
                    .listRowBackground(Color.appSurface)
                }

                if store.roster.entries.count > 1 {
                    Section("Switch Profile") {
                        ForEach(store.roster.entries) { saved in
                            Button {
                                store.switchProfile(saved)
                            } label: {
                                HStack {
                                    VStack(alignment: .leading) {
                                        Text(saved.profile.name)
                                        Text(saved.serverAddress)
                                            .font(.caption)
                                            .foregroundStyle(Color.appMuted)
                                    }
                                    Spacer()
                                    if saved.id == store.activeProfile?.id {
                                        Image(systemName: "checkmark").foregroundStyle(Color.appAccent)
                                    }
                                }
                            }
                        }
                    }
                    .listRowBackground(Color.appSurface)
                }

                Section("Playback Defaults") {
                    Picker("Audio language", selection: $store.audioLanguage) {
                        ForEach(languages, id: \.code) { item in
                            Text(item.label).tag(item.code)
                        }
                    }
                    Toggle("Subtitles always on", isOn: $store.subtitlesAlways)
                    Picker("Subtitle language", selection: $store.subtitleLanguage) {
                        ForEach(languages, id: \.code) { item in
                            Text(item.label).tag(item.code)
                        }
                    }
                }
                .listRowBackground(Color.appSurface)

                Section("Account") {
                    Button("Add another server or profile") {
                        showAddProfile = true
                    }
                    .foregroundStyle(Color.appAccent)

                    Button("Disconnect this profile", role: .destructive) {
                        confirmDisconnect = true
                    }
                }
                .listRowBackground(Color.appSurface)

                Section("About") {
                    LabeledContent("Version", value: appVersionLabel)
                }
                .listRowBackground(Color.appSurface)
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .scrollIndicators(.hidden)
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .confirmationDialog(
                "Disconnect this profile from this iPhone?",
                isPresented: $confirmDisconnect,
                titleVisibility: .visible
            ) {
                Button("Disconnect", role: .destructive) {
                    Task { await store.disconnectCurrent() }
                }
                Button("Cancel", role: .cancel) {}
            }
            .sheet(isPresented: $showAddProfile) {
                PhoneOnboardingView(store: store)
                    .onChange(of: store.activeProfile?.id) { _, _ in
                        showAddProfile = false
                    }
            }
        }
    }

    private var appVersionLabel: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "0.7.1"
        let build = info?["CFBundleVersion"] as? String ?? "1"
        return "\(short) (\(build))"
    }
}
