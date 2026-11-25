import QualificationCore
import SwiftUI

struct PhoneOnboardingView: View {
    @Bindable var store: PhoneSessionStore

    @State private var serverURL = ""
    @State private var inviteLink = ""
    @State private var useInvite = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    headerCard
                    if let error = store.errorMessage {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.subheadline)
                            .foregroundStyle(Color.appWarning)
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 12))
                            .overlay(
                                RoundedRectangle(cornerRadius: 12)
                                    .strokeBorder(Color.appWarning.opacity(0.4), lineWidth: 1)
                            )
                    }
                    if let challenge = store.pairingChallenge {
                        pairingCodeCard(challenge)
                    } else if useInvite {
                        inviteCard
                    } else {
                        serverCard
                    }
                }
                .padding(20)
            }
            .background(Color.appBackground.ignoresSafeArea())
            .foregroundStyle(Color.appText)
            .navigationTitle("Omakase Plus")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear {
                if serverURL.isEmpty {
                    serverURL = store.defaultServerOrigin
                }
            }
        }
    }

    private var headerCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Connect to your server")
                .font(.title.bold())
            Text("Sign in with a one-time pairing code from your web hub or paste a library invitation link.")
                .font(.subheadline)
                .foregroundStyle(Color.appMuted)
        }
    }

    private var serverCard: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Server URL")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Color.appMuted)
            TextField("https://your-server.example", text: $serverURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .padding(14)
                .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))
                .accessibilityIdentifier("phone.onboarding.server")

            Button {
                let target = serverURL
                Task { await store.startPairing(server: target) }
            } label: {
                HStack {
                    if store.connecting {
                        ProgressView().tint(.black)
                    }
                    Text(store.connecting ? "Starting pairing…" : "Connect with pairing code")
                        .font(.headline)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.appAccent)
            .foregroundStyle(Color.appBackground)
            .disabled(store.connecting || serverURL.trimmingCharacters(in: .whitespacesAndNewlines).count < 8)
            .accessibilityIdentifier("phone.onboarding.pair")

            Button("Paste an invite link instead") {
                store.errorMessage = nil
                useInvite = true
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(Color.appAccent)
            .accessibilityIdentifier("phone.onboarding.invite")
        }
        .padding(18)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    private func pairingCodeCard(_ challenge: LiveLibraryClient.DeviceChallenge) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Approve this iPhone")
                .font(.headline)
            Text("Open the approval page on your Omakase Plus server and confirm this code:")
                .font(.subheadline)
                .foregroundStyle(Color.appMuted)
            Text(challenge.code)
                .font(.system(size: 34, weight: .bold, design: .monospaced))
                .foregroundStyle(Color.appAccent)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(Color.appRaised, in: RoundedRectangle(cornerRadius: 12))
                .textSelection(.enabled)

            Link(destination: challenge.verificationURL) {
                Label("Open approval page in Safari", systemImage: "safari")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.appAccent)
            .foregroundStyle(Color.appBackground)

            HStack(spacing: 12) {
                ProgressView()
                    .tint(Color.appAccent)
                Text("Waiting for approval…")
                    .font(.footnote)
                    .foregroundStyle(Color.appMuted)
                Spacer()
                Button("Cancel", role: .cancel) {
                    store.cancelPairing()
                }
                .font(.footnote.weight(.semibold))
            }
        }
        .padding(18)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }

    private var inviteCard: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Invitation link")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(Color.appMuted)
            TextField("https://your-server#library-...", text: $inviteLink)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .padding(14)
                .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.appKeyline, lineWidth: 1))
                .accessibilityIdentifier("phone.onboarding.inviteField")

            Button {
                let trimmed = inviteLink
                Task { await store.linkInvite(trimmed) }
            } label: {
                Text(store.connecting ? "Connecting…" : "Link invite")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.appAccent)
            .foregroundStyle(Color.appBackground)
            .disabled(store.connecting || inviteLink.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

            Button("Use server URL and pairing code instead") {
                store.errorMessage = nil
                useInvite = false
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(Color.appAccent)
        }
        .padding(18)
        .background(Color.appSurface, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.appKeyline, lineWidth: 1))
    }
}
