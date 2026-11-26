import Foundation
import Observation
import QualificationCore

@MainActor
@Observable
final class PhoneSessionStore {
    enum RestoreState: Equatable {
        case idle
        /// The Keychain could not be read yet, usually because the phone was locked at launch.
        case needsRetry
    }

    private(set) var roster = LiveProfileRoster()
    private(set) var client: LiveLibraryClient?
    private(set) var libraries: [LiveLibraryOption] = []
    private(set) var selectedLibrary: LiveLibraryOption?
    private(set) var restoreState: RestoreState = .idle

    var pairingChallenge: LiveLibraryClient.DeviceChallenge?
    var activePass: LiveTestPass?
    var resumeWriter: LiveResumeWriter?
    var openingPlayback = false
    var connecting = false
    var errorMessage: String?

    var audioLanguage: String {
        didSet { UserDefaults.standard.set(audioLanguage, forKey: Self.audioKey) }
    }
    var subtitlesAlways: Bool {
        didSet { UserDefaults.standard.set(subtitlesAlways, forKey: Self.subtitlesAlwaysKey) }
    }
    var subtitleLanguage: String {
        didSet { UserDefaults.standard.set(subtitleLanguage, forKey: Self.subtitleLanguageKey) }
    }

    private var baseClient: LiveLibraryClient?
    private var pairingTask: Task<Void, Never>?
    private var confirmingUnauthorized = false
    private var renewing = false
    private var lastRenewal = Date.distantPast

    private static let audioKey = "omakase.ios.audio-language"
    private static let subtitlesAlwaysKey = "omakase.ios.subtitles-always"
    private static let subtitleLanguageKey = "omakase.ios.subtitle-language"
    private static let renewalInterval: TimeInterval = 6 * 3600

    var defaultServerOrigin: String {
        if let raw = Bundle.main.object(forInfoDictionaryKey: "OmakaseServerOrigin") as? String {
            let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.hasPrefix("https://"), !trimmed.contains("$(") {
                return trimmed
            }
        }
        return "https://"
    }

    var activeProfile: LiveSavedProfile? { roster.selected }

    init() {
        let defaults = UserDefaults.standard
        audioLanguage = defaults.string(forKey: Self.audioKey) ?? "auto"
        subtitlesAlways = defaults.object(forKey: Self.subtitlesAlwaysKey) as? Bool ?? true
        subtitleLanguage = defaults.string(forKey: Self.subtitleLanguageKey) ?? "en"
        restoreFromKeychain()
    }

    // MARK: Restoring and keeping the link

    func restoreFromKeychain() {
        let stored: PhoneKeychain.Storage
        do {
            stored = try PhoneKeychain.read()
        } catch PhoneKeychain.Failure.locked {
            // Launched while the phone was locked. Do not show onboarding; retry when active.
            restoreState = .needsRetry
            return
        } catch {
            restoreState = .idle
            return
        }
        restoreState = .idle
        switch stored {
        case .roster(var savedRoster):
            savedRoster.prune()
            if savedRoster.selected == nil, let first = savedRoster.entries.first {
                _ = try? savedRoster.select(first)
            }
            roster = savedRoster
            persistRoster()
            if let current = roster.selected,
               let restored = try? LiveLibraryClient.restore(current.credential) {
                applyAuthenticatedClient(restored, savedProfile: current)
            }
        case .legacy(let credential):
            if let restored = try? LiveLibraryClient.restore(credential) {
                baseClient = restored
                client = restored
                Task { await adoptLegacyCredential(restored, credential: credential) }
            }
        }
    }

    /// Called whenever the app becomes active. Retries a restore that was blocked by a
    /// locked phone or a transient Keychain failure.
    func resumeIfNeeded() {
        guard client == nil, pairingChallenge == nil, !connecting else { return }
        restoreFromKeychain()
    }

    /// Stop waiting for the Keychain and show the connect screen.
    func abandonRestore() {
        restoreState = .idle
    }

    /// Ask the server to refresh this device's expiry. A paired device in use stays paired.
    /// Any failure is ignored on purpose: an invitation link or a flaky network must never
    /// cost the user their saved connection.
    func renewIfNeeded(force: Bool = false) async {
        guard !renewing, let current = roster.selected, let base = baseClient else { return }
        guard force || Date().timeIntervalSince(lastRenewal) >= Self.renewalInterval else { return }
        renewing = true
        defer { renewing = false }
        let expectedToken = base.credential.token
        guard let renewed = try? await base.renewed() else { return }
        guard baseClient?.credential.token == expectedToken, roster.selected?.id == current.id else { return }
        lastRenewal = Date()
        let entry = LiveSavedProfile(profile: current.profile, credential: renewed.credential)
        guard (try? roster.select(entry)) != nil else { return }
        persistRoster()
        baseClient = renewed
        client = renewed.selecting(selectedLibrary)
    }

    private func adoptLegacyCredential(_ legacyClient: LiveLibraryClient, credential: LiveLibraryClient.Credential) async {
        let expectedToken = legacyClient.credential.token
        guard let profile = try? await legacyClient.profile() else { return }
        guard baseClient?.credential.token == expectedToken else { return }
        let entry = LiveSavedProfile(profile: profile, credential: credential)
        _ = try? roster.select(entry)
        persistRoster()
        await refreshLibraries()
    }

    // MARK: Pairing

    func startPairing(server: String) async {
        cancelPairing()
        errorMessage = nil
        connecting = true
        defer { connecting = false }
        do {
            let challenge = try await LiveLibraryClient.startPairing(server: server, kind: "iphone")
            pairingChallenge = challenge
            pairingTask = Task { [weak self] in
                await self?.pollChallenge(challenge)
            }
        } catch {
            errorMessage = Self.describe(error, fallback: "Check the HTTPS server address and try again.")
        }
    }

    func cancelPairing() {
        pairingTask?.cancel()
        pairingTask = nil
        pairingChallenge = nil
    }

    private func pollChallenge(_ challenge: LiveLibraryClient.DeviceChallenge) async {
        do {
            let liveClient = try await challenge.waitForApproval()
            let profile = try await liveClient.profile()
            try finishLinking(liveClient, saved: LiveSavedProfile(profile: profile, credential: liveClient.credential))
        } catch is CancellationError {
            return
        } catch {
            pairingChallenge = nil
            errorMessage = Self.describe(error, fallback: "Pairing expired or was canceled. Please try again.")
        }
    }

    func linkInvite(_ code: String) async {
        cancelPairing()
        errorMessage = nil
        connecting = true
        defer { connecting = false }
        do {
            let liveClient = try await LiveLibraryClient.link(invitation: code)
            let profile = try await liveClient.profile()
            try finishLinking(liveClient, saved: LiveSavedProfile(profile: profile, credential: liveClient.credential))
        } catch {
            errorMessage = Self.describe(error, fallback: "Invalid or expired invitation link.")
        }
    }

    private func finishLinking(_ liveClient: LiveLibraryClient, saved: LiveSavedProfile) throws {
        _ = try roster.select(saved)
        pairingChallenge = nil
        applyAuthenticatedClient(liveClient, savedProfile: saved)
        lastRenewal = Date()
        persistRoster()
    }

    // MARK: Profiles

    func switchProfile(_ entry: LiveSavedProfile) {
        guard let restored = try? LiveLibraryClient.restore(entry.credential) else {
            errorMessage = "Saved session expired. Link this server again."
            return
        }
        _ = try? roster.select(entry)
        persistRoster()
        PhoneArtworkCache.shared.clear()
        applyAuthenticatedClient(restored, savedProfile: entry)
    }

    func selectLibrary(_ option: LiveLibraryOption?) {
        selectedLibrary = option?.kind == .household ? option : nil
        if let baseClient {
            client = baseClient.selecting(selectedLibrary)
        }
    }

    func disconnectCurrent() async {
        await closePlayer()
        let activeID = roster.selected?.id
        if let baseClient {
            try? await baseClient.unlink()
        }
        if let activeID {
            roster.remove(activeID)
        }
        persistRoster()
        clearActiveClient()
        useNextSavedProfile()
    }

    // MARK: Playback

    func play(_ entry: LiveLibraryEntry, featured: Bool = false) async {
        guard let client, !openingPlayback else { return }
        errorMessage = nil
        openingPlayback = true
        defer { openingPlayback = false }
        do {
            let pass = try await client.play(entry, featured: featured, engine: .native)
            installPass(pass, using: client)
        } catch FixtureConnectionError.unauthorized {
            handleUnauthorized()
        } catch {
            errorMessage = Self.describe(error, fallback: "Could not open playback for this title.")
        }
    }

    func playNext() async throws {
        guard let client, let current = activePass else { throw CancellationError() }
        let next: LiveTestPass
        do {
            next = try await client.nextEpisode(after: current)
        } catch FixtureConnectionError.unauthorized {
            handleUnauthorized()
            throw CancellationError()
        }
        let previousWriter = resumeWriter
        installPass(next, using: client)
        await previousWriter?.flush(retryUnsaved: true)
        await client.release(current)
    }

    func closePlayer() async {
        let closing = activePass
        let writer = resumeWriter
        activePass = nil
        resumeWriter = nil
        await writer?.flush(retryUnsaved: true)
        if let closing {
            await client?.release(closing)
        }
    }

    // MARK: Authorization loss

    /// Any screen that receives a 401 reports it here. One 401 is not proof the link is
    /// gone: it can be a single resource, a restarting server or a stale cached client.
    /// The saved connection is removed only when the server also rejects the profile
    /// check itself.
    func handleUnauthorized() {
        guard !confirmingUnauthorized, let probe = baseClient, let targetID = roster.selected?.id else { return }
        confirmingUnauthorized = true
        let expectedToken = probe.credential.token
        Task { await confirmUnauthorized(using: probe, expectedToken: expectedToken, targetID: targetID) }
    }

    private func confirmUnauthorized(
        using probe: LiveLibraryClient,
        expectedToken: String,
        targetID: LiveSavedProfile.ID
    ) async {
        defer { confirmingUnauthorized = false }
        do {
            _ = try await probe.profile()
            return
        } catch FixtureConnectionError.unauthorized {
            guard baseClient?.credential.token == expectedToken, roster.selected?.id == targetID else { return }
            do {
                let renewed = try await probe.renewed()
                guard baseClient?.credential.token == expectedToken, roster.selected?.id == targetID else { return }
                // The server still knows this device; only our copy of the expiry was stale.
                adopt(renewed: renewed)
            } catch FixtureConnectionError.unauthorized {
                guard baseClient?.credential.token == expectedToken else { return }
                dropProfile(id: targetID, message: "Session expired or device was unlinked. Reconnect your server.")
            } catch {
                guard baseClient?.credential.token == expectedToken else { return }
                errorMessage = "Couldn't reach your server. Pull to refresh."
            }
        } catch {
            guard baseClient?.credential.token == expectedToken else { return }
            errorMessage = "Couldn't reach your server. Pull to refresh."
        }
    }

    private func adopt(renewed: LiveLibraryClient) {
        guard let current = roster.selected else { return }
        let entry = LiveSavedProfile(profile: current.profile, credential: renewed.credential)
        guard (try? roster.select(entry)) != nil else { return }
        persistRoster()
        baseClient = renewed
        client = renewed.selecting(selectedLibrary)
        lastRenewal = Date()
    }

    private func dropProfile(id: LiveSavedProfile.ID, message: String) {
        let wasSelected = roster.selected?.id == id
        roster.remove(id)
        persistRoster()
        guard wasSelected else { return }
        clearActiveClient()
        activePass = nil
        resumeWriter = nil
        useNextSavedProfile()
        errorMessage = message
    }

    private func clearActiveClient() {
        baseClient = nil
        client = nil
        libraries = []
        selectedLibrary = nil
        PhoneArtworkCache.shared.clear()
    }

    private func useNextSavedProfile() {
        guard let fallback = roster.entries.first,
              let restored = try? LiveLibraryClient.restore(fallback.credential) else { return }
        _ = try? roster.select(fallback)
        persistRoster()
        applyAuthenticatedClient(restored, savedProfile: fallback)
    }

    func refreshLibraries() async {
        guard let baseClient else { return }
        let expectedToken = baseClient.credential.token
        if let list = try? await baseClient.libraries(), self.baseClient?.credential.token == expectedToken {
            libraries = list
            if let current = selectedLibrary, !list.contains(current) {
                selectLibrary(nil)
            }
        }
    }

    // MARK: Helpers

    private func applyAuthenticatedClient(_ liveClient: LiveLibraryClient, savedProfile: LiveSavedProfile) {
        baseClient = liveClient
        selectedLibrary = nil
        client = liveClient
        errorMessage = nil
        lastRenewal = .distantPast
        Task { await refreshLibraries() }
    }

    private func persistRoster() {
        do {
            try PhoneKeychain.save(roster)
        } catch {
            errorMessage = "Couldn't save this connection on your iPhone. You may need to connect again later."
        }
    }

    private func installPass(_ pass: LiveTestPass, using client: LiveLibraryClient) {
        resumeWriter = client.resumeWriter(
            for: pass,
            onAccessLost: { [weak self] loss in
                guard let self else { return }
                switch loss {
                case .profile:
                    self.handleUnauthorized()
                case .media, .planExpired:
                    self.errorMessage = "Stream access changed. Reopen playback to refresh."
                }
            }
        )
        activePass = pass
    }

    static func describe(_ error: Error, fallback: String) -> String {
        switch error {
        case FixtureConnectionError.unauthorized:
            return "Authorization failed or expired."
        case FixtureConnectionError.invalidOrigin:
            return "Enter a valid HTTPS server address (or local IP/domain)."
        case FixtureConnectionError.invalidCode:
            return "Invalid pairing or invitation link."
        case LiveLibraryError.copyUnavailable:
            return "No playable stream is available for this title."
        default:
            return fallback
        }
    }
}
