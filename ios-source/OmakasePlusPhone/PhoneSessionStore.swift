import Foundation
import Observation
import QualificationCore

@MainActor
@Observable
final class PhoneSessionStore {
    private(set) var roster = LiveProfileRoster()
    private(set) var client: LiveLibraryClient?
    private(set) var libraries: [LiveLibraryOption] = []
    private(set) var selectedLibrary: LiveLibraryOption?

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

    private static let audioKey = "omakase.ios.audio-language"
    private static let subtitlesAlwaysKey = "omakase.ios.subtitles-always"
    private static let subtitleLanguageKey = "omakase.ios.subtitle-language"

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

    func restoreFromKeychain() {
        guard let stored = try? PhoneKeychain.read() else { return }
        switch stored {
        case .roster(var savedRoster):
            savedRoster.prune()
            roster = savedRoster
            try? PhoneKeychain.save(roster)
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

    private func adoptLegacyCredential(_ client: LiveLibraryClient, credential: LiveLibraryClient.Credential) async {
        guard let profile = try? await client.profile() else { return }
        let entry = LiveSavedProfile(profile: profile, credential: credential)
        _ = try? roster.select(entry)
        try? PhoneKeychain.save(roster)
        await refreshLibraries()
    }

    func startPairing(server: String) async {
        cancelPairing()
        errorMessage = nil
        connecting = true
        defer { connecting = false }
        do {
            let challenge = try await LiveLibraryClient.startPairing(server: server)
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
            let saved = LiveSavedProfile(profile: profile, credential: liveClient.credential)
            _ = try roster.select(saved)
            try? PhoneKeychain.save(roster)
            pairingChallenge = nil
            applyAuthenticatedClient(liveClient, savedProfile: saved)
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
            let saved = LiveSavedProfile(profile: profile, credential: liveClient.credential)
            _ = try roster.select(saved)
            try? PhoneKeychain.save(roster)
            applyAuthenticatedClient(liveClient, savedProfile: saved)
        } catch {
            errorMessage = Self.describe(error, fallback: "Invalid or expired invitation link.")
        }
    }

    func switchProfile(_ entry: LiveSavedProfile) {
        guard let restored = try? LiveLibraryClient.restore(entry.credential) else {
            errorMessage = "Saved session expired. Link this server again."
            return
        }
        _ = try? roster.select(entry)
        try? PhoneKeychain.save(roster)
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
        try? PhoneKeychain.save(roster)
        baseClient = nil
        client = nil
        libraries = []
        selectedLibrary = nil
        PhoneArtworkCache.shared.clear()
        if let fallback = roster.entries.first,
           let restored = try? LiveLibraryClient.restore(fallback.credential) {
            _ = try? roster.select(fallback)
            try? PhoneKeychain.save(roster)
            applyAuthenticatedClient(restored, savedProfile: fallback)
        }
    }

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

    func handleUnauthorized() {
        errorMessage = "Session expired or device was unlinked. Reconnect your server."
        if let id = roster.selected?.id {
            roster.remove(id)
            try? PhoneKeychain.save(roster)
        }
        baseClient = nil
        client = nil
        activePass = nil
        resumeWriter = nil
    }

    func refreshLibraries() async {
        guard let baseClient else { return }
        if let list = try? await baseClient.libraries() {
            libraries = list
            if let current = selectedLibrary, !list.contains(current) {
                selectLibrary(nil)
            }
        }
    }

    private func applyAuthenticatedClient(_ liveClient: LiveLibraryClient, savedProfile: LiveSavedProfile) {
        baseClient = liveClient
        selectedLibrary = nil
        client = liveClient
        errorMessage = nil
        Task { await refreshLibraries() }
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
