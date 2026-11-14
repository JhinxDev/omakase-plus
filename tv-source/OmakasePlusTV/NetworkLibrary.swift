import QualificationCore
import CoreImage.CIFilterBuiltins
import SwiftUI

@MainActor
private final class NetworkLibrary: ObservableObject {
    @Published private(set) var account: String?
    @Published private(set) var cards: [FixtureRemoteCard] = []
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    private var client: FixtureClient?
    private var device: FixtureDevice?

    func connect(origin text: String, code: String) async {
        guard !busy, device == nil else { return }
        busy = true
        error = nil
        defer { busy = false }
        #if targetEnvironment(simulator) && DEBUG
        let allowLoopback = true
        #else
        let allowLoopback = false
        #endif
        do {
            let candidate = FixtureClient(origin: try FixtureOrigin(text, allowLoopbackHTTP: allowLoopback))
            let paired = try await candidate.pair(code: code)
            do {
                cards = try await candidate.catalog(device: paired)
            } catch {
                try? await candidate.logout(device: paired)
                throw error
            }
            client = candidate
            device = paired
            account = paired.accountLabel
        } catch FixtureConnectionError.invalidOrigin {
            error = "Enter the test server origin. The physical Apple TV requires HTTPS."
        } catch FixtureConnectionError.invalidCode {
            error = "Enter the 16-character code shown by the test server."
        } catch FixtureConnectionError.unauthorized {
            error = "That code was rejected. Use an unused code from a running test server."
        } catch {
            self.error = "Could not connect to the test library. Check the server and address."
        }
    }

    func start(_ id: LibraryIdentity, resume: Bool, player: PlaybackSession) async -> Bool {
        guard !busy, let client, let device, let selected = cards.first(where: { $0.card.id == id }) else {
            return false
        }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let play = try await client.start(selected, resume: resume, device: device)
            if !Task.isCancelled && player.beginRemote(url: play.url, position: play.position, finished: { [weak self] position in
                self?.finish(play, position: position)
            }) {
                return true
            }
            try? await client.close(play, position: nil, device: device)
            return false
        } catch {
            self.error = "The test library could not open this copy. Check your connection or reconnect."
            return false
        }
    }

    private func finish(_ play: FixtureRemotePlayback, position: Double?) {
        guard let client, let device else { return }
        busy = true
        Task { @MainActor in
            defer { busy = false }
            do {
                try await client.close(play, position: position, device: device)
            } catch {
                // The request is idempotent. A lost response can be recovered with the same position.
                do {
                    try await client.close(play, position: position, device: device)
                } catch {
                    self.error = "Could not confirm resume was saved. The next play uses the server’s last saved point."
                    return
                }
            }
            do {
                cards = try await client.catalog(device: device)
            } catch {
                self.error = "Resume saved, but the shelf could not refresh. Reconnect to read the saved point."
            }
        }
    }

    func disconnect() async {
        guard !busy, let client, let device else { return }
        busy = true
        error = nil
        defer {
            self.device = nil
            self.client = nil
            account = nil
            cards = []
            busy = false
        }
        do {
            try await client.logout(device: device)
        } catch FixtureConnectionError.unauthorized {
            // The server already rejected this device.
        } catch {
            self.error = "Disconnected here. The server could not confirm sign-out."
        }
    }

    func resume(for card: LibraryCard) -> Double? {
        guard let value = cards.first(where: { $0.card.id == card.id })?.position, value > 0, value < 30
        else { return nil }
        return value
    }
}

@MainActor
struct NetworkLibraryScreen: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var model = NetworkLibrary()
    @StateObject private var player = PlaybackSession()
    @State private var selected: LibraryCard?
    @State private var lastCard: LibraryIdentity?
    #if targetEnvironment(simulator) && DEBUG
    @State private var origin = "http://localhost:8765"
    #else
    @State private var origin = ""
    #endif
    @State private var code = ""
    let back: () -> Void

    var body: some View {
        Group {
            if let selected {
                TitleScreen(card: selected, saved: model.resume(for: selected), session: player,
                            isSaving: model.busy, externalError: model.error,
                            startPlayback: { await model.start(selected.id, resume: $0, player: player) },
                            back: { self.selected = nil })
            } else if let account = model.account {
                LibraryShelf(cards: model.cards.map(\.card), returnTo: lastCard, heading: account,
                             description: "Test library · Resume is saved on the synthetic server",
                             actionTitle: "Disconnect test account", action: disconnect, resume: model.resume) { card in
                    guard !model.busy else { return }
                    player.clearFailure()
                    lastCard = card.id
                    selected = card
                }
                .onExitCommand(perform: disconnect)
            } else {
                VStack(alignment: .leading, spacing: 24) {
                    Text("Connect to test library").font(.largeTitle.bold())
                    Text("Start the bundled test server on your Mac and enter one of its codes.")
                        .foregroundStyle(.secondary)
                    TextField("Test server address", text: $origin)
                    SecureField("One-time code", text: $code)
                    if let error = model.error { Text(error).foregroundStyle(.yellow) }
                    HStack {
                        Button("Connect") {
                            let entered = code
                            code = ""
                            Task { await model.connect(origin: origin, code: entered) }
                        }
                        .disabled(model.busy || origin.isEmpty || code.isEmpty)
                        Button("Back to offline library", action: back).disabled(model.busy)
                    }
                }
                .frame(maxWidth: 950)
                .onExitCommand { if !model.busy { back() } }
            }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            player.scenePhaseChanged(AppScenePhase(phase))
        }
    }

    private func disconnect() {
        guard !model.busy else { return }
        lastCard = nil
        Task { await model.disconnect() }
    }
}

@MainActor
struct LiveTestScreen: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var player = PlaybackSession(resumeKey: "omakase.tv.live.resume",
                                                             openingTimeout: .seconds(90))
    @State private var invitation = ""
    @State private var pass: LiveTestPass?
    @State private var passSeries: LiveLibraryEntry?
    @State private var passEpisode: LiveLibraryEntry?
    @State private var pendingReturnSeries: LiveLibraryEntry?
    @State private var resumeImmediately = false
    @State private var resumeWriter: LiveResumeWriter?
    @State private var reloadMediaID: String?
    @State private var autoRetriedOpening = false
    @State private var closingPass = false
    @State private var library: LiveLibraryClient?
    @State private var libraryVersion = UUID()
    @State private var libraryKind: LiveLibraryKind = .anime
    @State private var roster = LiveProfileRoster()
    @State private var restoring = true
    @State private var restoreFailed = false
    @State private var showingProfiles = false
    @State private var openingProfile = false
    @State private var profileError: String?
    @State private var enteringServer = false
    @State private var serverKeyboardRequest = false
    @State private var challenge: LiveLibraryClient.DeviceChallenge?
    @State private var pairingError: String?
    @AppStorage("omakase.tv.server-origin") private var server =
        Bundle.main.object(forInfoDictionaryKey: "OmakaseServerOrigin") as? String ?? ""
    @State private var connecting = false
    @State private var error: String?
    @State private var showingEditor = false
    @FocusState private var focus: Field?

    private enum Field { case setup, manual, invitation, open, cancel, server, begin, pairBack }

    private var libraryVisible: Bool {
        pass == nil
    }

    private var activePlaybackMediaID: String? {
        guard scenePhase == .active, player.isPresenting, !closingPass,
              let pass, pass.supportsAccessCheck else { return nil }
        return pass.mediaID
    }

    var body: some View {
        ZStack {
            if let library, !showingProfiles && !enteringServer && challenge == nil {
                LiveLibraryScreen(client: library, isPresented: libraryVisible, select: { selection, series, episode, resumeImmediately in
                    passSeries = series
                    passEpisode = episode
                    self.resumeImmediately = resumeImmediately
                    autoRetriedOpening = false
                    resumeWriter = makeResumeWriter(for: selection, client: library)
                    pass = selection
                }, disconnect: disconnect, openProfiles: openProfiles, chooseLibrary: { option in
                    self.library = library.selecting(option)
                    libraryVersion = UUID()
                }, returnSeries: $pendingReturnSeries, kind: $libraryKind)
                    .id(libraryVersion)
                    .opacity(libraryVisible ? 1 : 0)
                    .disabled(!libraryVisible)
                    .accessibilityHidden(!libraryVisible)
                    .onChange(of: player.errorMessage) { _, message in
                        guard let message else { return }
                        Task {
                            await library.reportDiagnostic("\(message) | \(player.lastDiagnostic.isEmpty ? player.diagnostic : player.lastDiagnostic)")
                        }
                    }
            }
            if let pass {
                TitleScreen(card: pass.card, saved: savedPosition(pass), session: player,
                            sampleDescription: pass.supportsResumeSync
                                ? ""
                                : "Resume is saved on this Apple TV.",
                            backTitle: library == nil ? "Use a new link" : "Back to library",
                            isSaving: closingPass,
                            needsReload: reloadMediaID == pass.mediaID,
                            externalError: error,
                            showsTechnicalErrors: false,
                            playNext: openNext,
                            artwork: preplayArtwork,
                            overview: pass.card.episodeTitle == nil ? passSeries?.summary : passEpisode?.summary,
                            episodeName: pass.episodeName ?? passEpisode?.episodeDisplayTitle,
                            runtimeSeconds: Int(pass.duration.rounded(.up)),
                            audioLanguages: pass.audioLanguages,
                            subtitleLanguages: pass.subtitleLanguages,
                            resumeImmediately: resumeImmediately,
                            startPlayback: { await startPlayback(pass, resume: $0) },
                            back: { closePass(pass) })
            } else if let challenge {
                pairingScreen(challenge)
                    .task(id: challenge.code) { await waitForApproval(challenge) }
            } else if enteringServer {
                serverScreen
            } else if showingProfiles {
                TVProfiles(roster: roster, active: library == nil ? nil : roster.selected?.id,
                           busy: openingProfile, error: profileError,
                           choose: { entry in Task { await openProfile(entry) } },
                           add: { pairingError = nil; enteringServer = true },
                           done: { showingProfiles = false })
            } else if restoring {
                ProgressView("Restoring your profile…")
            } else if restoreFailed {
                VStack(alignment: .leading, spacing: 24) {
                    Text("Couldn’t restore your profile").font(.largeTitle.bold())
                    Text("Check your connection and try again. Your saved profile is still on this TV.")
                        .foregroundStyle(Color.tvMuted)
                    Button("Try again") {
                        restoring = true
                        Task { await restoreProfiles() }
                    }
                    .buttonStyle(TVActionStyle(focused: focus == .setup))
                    .focusEffectDisabled()
                    .focused($focus, equals: .setup)
                }
                .defaultFocus($focus, .setup)
            } else if library == nil {
                HStack(alignment: .center, spacing: 94) {
                    VStack(alignment: .leading, spacing: 22) {
                        HStack(spacing: 16) {
                            TVBrandMark(size: 34)
                            Text("OMAKASE PLUS")
                                .font(.headline.weight(.semibold)).tracking(6)
                                .foregroundStyle(Color.tvFocus)
                        }
                        Text("Your library on Apple TV")
                            .font(.system(size: 68, weight: .bold))
                            .accessibilityAddTraits(.isHeader)
                            .accessibilityIdentifier("live.heading")
                        Text("Sign in to bring your Omakase Plus library to this TV.")
                            .font(.title3)
                            .foregroundStyle(Color.tvMuted)
                        if let profileError { Text(profileError).foregroundStyle(Color.tvWarning) }
                        Button("Set up Apple TV") {
                            pairingError = nil
                            enteringServer = true
                        }
                            .buttonStyle(TVActionStyle(focused: focus == .setup, tint: .tvFocus))
                            .focusEffectDisabled()
                            .focused($focus, equals: .setup)
                            .accessibilityIdentifier("live.enter")
                            .padding(.top, 10)
                        Button("Use a one-time link") { showingEditor = true }
                            .buttonStyle(TVActionStyle(focused: focus == .manual))
                            .focusEffectDisabled()
                            .focused($focus, equals: .manual)
                            .accessibilityIdentifier("live.manual")
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    VStack(alignment: .leading, spacing: 28) {
                        Label("On your phone or computer", systemImage: "iphone.and.arrow.right")
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(Color.tvText)
                        VStack(alignment: .leading, spacing: 18) {
                            instruction("01", "Set up with your Omakase server address.")
                            instruction("02", "Scan the QR code shown on this TV.")
                            instruction("03", "Sign in on your phone and approve the code.")
                        }
                    }
                    .padding(38)
                    .frame(width: 570, alignment: .leading)
                    .background(Color.tvSurface, in: RoundedRectangle(cornerRadius: 16))
                    .overlay {
                        RoundedRectangle(cornerRadius: 16)
                            .strokeBorder(Color.tvKeyline, lineWidth: 1)
                    }
                }
                .padding(.horizontal, 108)
                .defaultFocus($focus, .setup)
                .fullScreenCover(isPresented: $showingEditor, onDismiss: {
                    invitation = ""
                    error = nil
                    focus = .setup
                }) {
                    linkEditor
                }
            }
        }
        .foregroundStyle(Color.tvText)
        .task { if restoring { await restoreProfiles() } }
        .task(id: activePlaybackMediaID) {
            if let pass, let library, activePlaybackMediaID == pass.mediaID {
                await monitorPlaybackAccess(pass, client: library)
            }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            player.scenePhaseChanged(AppScenePhase(phase))
        }
    }

    private var serverScreen: some View {
        VStack(alignment: .leading, spacing: 26) {
            Text(showingProfiles ? "Add a profile" : "Set up Apple TV")
                .font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            Text("Enter the HTTPS address you use to open Omakase Plus in your browser.")
                .foregroundStyle(.secondary)
            Button { serverKeyboardRequest.toggle() } label: {
                Label(server.isEmpty ? "Enter server address" : server, systemImage: "link")
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
                .buttonStyle(TVActionStyle(focused: focus == .server))
                .focusEffectDisabled()
                .focused($focus, equals: .server)
                .accessibilityLabel("Omakase server address")
                .accessibilityValue(server)
                .accessibilityHint("Opens the Apple TV keyboard")
                .accessibilityIdentifier("live.server")
                .disabled(connecting)
                .background {
                    TVSystemKeyboard(text: $server, requested: serverKeyboardRequest,
                                     prompt: "Omakase server address", keyboard: .URL) {
                        if !server.isEmpty { focus = .begin }
                    }
                    .frame(width: 1, height: 1)
                    .clipped()
                    .accessibilityHidden(true)
                }
            if let pairingError { Text(pairingError).foregroundStyle(.yellow) }
            HStack(spacing: 18) {
                Button(connecting ? "Connecting…" : "Show sign-in code", action: startPairing)
                    .buttonStyle(TVActionStyle(focused: focus == .begin, tint: .tvFocus))
                    .focusEffectDisabled()
                    .focused($focus, equals: .begin)
                    .accessibilityIdentifier("live.showCode")
                    .disabled(connecting || server.isEmpty)
                Button("Back") {
                    enteringServer = false
                    focus = .setup
                }
                .buttonStyle(TVActionStyle(focused: focus == .pairBack))
                .focusEffectDisabled()
                .focused($focus, equals: .pairBack)
                .disabled(connecting)
            }
        }
        .frame(maxWidth: 900)
        .padding(70)
        .defaultFocus($focus, server.isEmpty ? .server : .begin)
        .task {
            focus = server.isEmpty ? .server : .begin
        }
        .onExitCommand {
            if !connecting {
                enteringServer = false
                focus = .setup
            }
        }
    }

    private func pairingScreen(_ challenge: LiveLibraryClient.DeviceChallenge) -> some View {
        HStack(alignment: .center, spacing: 76) {
            VStack(alignment: .leading, spacing: 24) {
                Text("OMAKASE PLUS").font(.headline.weight(.semibold)).tracking(6)
                    .foregroundStyle(Color.tvFocus)
                Text(showingProfiles ? "Link this profile" : "Link your Apple TV")
                    .font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
                Text(showingProfiles
                     ? "On your phone, sign in as the person you’re adding and approve this code."
                     : "Scan with your phone, sign in, and confirm the code below.")
                    .foregroundStyle(.secondary)
                Text(challenge.code)
                    .font(.system(size: 62, weight: .semibold, design: .monospaced))
                    .accessibilityIdentifier("live.deviceCode")
                Text(challenge.verificationURL.host ?? "")
                    .font(.callout)
                Text("You can also enter the code in Settings → Devices.")
                    .font(.callout).foregroundStyle(.secondary)
                if let pairingError {
                    Text(pairingError).foregroundStyle(.yellow)
                } else {
                    ProgressView("Waiting for approval")
                }
                Button(pairingError == nil ? "Cancel" : "Get a new code") {
                    self.challenge = nil
                    pairingError = nil
                    focus = .begin
                }
                .buttonStyle(TVActionStyle(focused: focus == .pairBack))
                .focusEffectDisabled()
                .focused($focus, equals: .pairBack)
            }
            TVPairingQR(url: challenge.verificationURL)
        }
        .padding(.horizontal, 110)
        .defaultFocus($focus, .pairBack)
        .onExitCommand {
            self.challenge = nil
            pairingError = nil
            focus = .begin
        }
    }

    private func startPairing() {
        guard !connecting else { return }
        connecting = true
        pairingError = nil
        Task {
            defer { connecting = false }
            do {
                challenge = try await LiveLibraryClient.startPairing(server: server)
            } catch FixtureConnectionError.invalidOrigin {
                pairingError = "Enter the server’s HTTPS address, without a page path."
            } catch {
                pairingError = "Couldn’t get a code. Check the address and your connection."
            }
        }
    }

    private func waitForApproval(_ challenge: LiveLibraryClient.DeviceChallenge) async {
        do {
            let client = try await challenge.waitForApproval()
            do {
                try Task.checkCancellation()
                try await addProfile(client)
            } catch {
                // If approval raced with cancellation, discard just this new device grant.
                Task { try? await client.unlink() }
                throw error
            }
            self.challenge = nil
            enteringServer = false
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            switch error {
            case FixtureConnectionError.unauthorized:
                pairingError = "The code expired. Get a new code to continue."
            case is TVLibraryKeychain.Unavailable:
                pairingError = "This TV couldn’t save the profile. Get a new code to try again."
            default:
                pairingError = "Couldn’t finish linking. Get a new code to try again."
            }
        }
    }

    private func restoreProfiles() async {
        defer { restoring = false }
        restoreFailed = false
        do {
            switch try TVLibraryKeychain.read() {
            case .roster(var saved):
                saved.prune()
                roster = saved
                if let selected = saved.selected { await openProfile(selected) }
                else { showingProfiles = !saved.entries.isEmpty }
            case .legacy(let saved):
                do {
                    try await addProfile(LiveLibraryClient.restore(saved))
                } catch FixtureConnectionError.unauthorized {
                    try TVLibraryKeychain.save(LiveProfileRoster())
                }
            }
        } catch is CancellationError {
            return
        } catch {
            restoreFailed = true
        }
    }

    private func openProfiles() {
        let active = roster.selected?.id
        roster.prune()
        if active != nil, roster.selected == nil { library = nil }
        profileError = nil
        showingProfiles = true
    }

    private func openProfile(_ entry: LiveSavedProfile) async {
        guard !openingProfile, pass == nil else { return }
        openingProfile = true
        profileError = nil
        defer { openingProfile = false }
        do {
            let client = try LiveLibraryClient.restore(entry.credential)
            let profile = try await client.profile()
            try Task.checkCancellation()
            guard profile.accountID == entry.profile.accountID else {
                throw FixtureConnectionError.unauthorized
            }
            try activate(LiveSavedProfile(profile: profile, credential: client.credential), client)
        } catch FixtureConnectionError.unauthorized {
            if roster.selected?.id == entry.id { library = nil }
            roster.remove(entry.id)
            try? TVLibraryKeychain.save(roster)
            profileError = "This profile’s link expired. Add it again to keep watching."
            showingProfiles = !roster.entries.isEmpty || library != nil
        } catch is CancellationError {
            return
        } catch {
            profileError = "Couldn’t open this profile. Check your connection and try again."
            showingProfiles = true
        }
    }

    private func addProfile(_ client: LiveLibraryClient) async throws {
        let profile = try await client.profile()
        try Task.checkCancellation()
        try activate(LiveSavedProfile(profile: profile, credential: client.credential), client)
    }

    private func activate(_ entry: LiveSavedProfile, _ client: LiveLibraryClient) throws {
        var updated = roster
        let replaced = try updated.select(entry)
        try TVLibraryKeychain.save(updated)
        roster = updated
        libraryVersion = UUID()
        library = client
        showingProfiles = false
        profileError = nil
        if let replaced, let previous = try? LiveLibraryClient.restore(replaced) {
            Task { try? await previous.unlink() }
        }
    }

    private func disconnect() {
        if let active = roster.selected {
            roster.remove(active.id)
            try? TVLibraryKeychain.save(roster)
        }
        library = nil
        showingProfiles = !roster.entries.isEmpty
        focus = .setup
    }

    private var preplayArtwork: TVLibraryArtwork? {
        guard let library, let passSeries else { return nil }
        let still = passEpisode.flatMap { $0.episodeArtwork == nil ? nil : $0 }
        return TVLibraryArtwork(entry: still ?? passSeries, client: library, banner: true, showsPlaceholder: false)
    }

    private func savedPosition(_ pass: LiveTestPass) -> Double? {
        if let position = resumeWriter?.localPosition {
            return position > 0 && position < pass.duration ? position : nil
        }
        if pass.hasServerResume { return pass.position > 0 ? pass.position : nil }
        if let local = player.resumeRecord(for: pass.card.identity.resumeIdentity) {
            return !local.reachedEnd && local.position > 0 && local.position < pass.duration ? local.position : nil
        }
        return pass.position > 0 ? pass.position : nil
    }

    private func makeResumeWriter(for pass: LiveTestPass, client: LiveLibraryClient) -> LiveResumeWriter? {
        let mediaID = pass.mediaID
        return client.resumeWriter(for: pass) { loss in
            guard self.pass?.mediaID == mediaID, !self.closingPass else { return }
            self.playbackAccessLost(loss)
        }
    }

    private func monitorPlaybackAccess(_ pass: LiveTestPass, client: LiveLibraryClient) async {
        while !Task.isCancelled, activePlaybackMediaID == pass.mediaID {
            // A transient network error can resolve without interrupting buffered playback.
            if let loss = try? await client.playbackAccessLoss(for: pass) {
                guard !Task.isCancelled, activePlaybackMediaID == pass.mediaID else { return }
                playbackAccessLost(loss)
                return
            }
            do {
                try await Task.sleep(for: .seconds(10))
            } catch {
                return
            }
        }
    }

    private func playbackAccessLost(_ loss: LivePlaybackAccessLoss) {
        player.stopForLostAccess()
        switch loss {
        case .profile:
            pass = nil
            passSeries = nil
            passEpisode = nil
            resumeImmediately = false
            resumeWriter = nil
            reloadMediaID = nil
            error = nil
            disconnect()
            profileError = "This profile is no longer linked. Link it again to keep watching."
        case .media:
            error = "This video is no longer available for playback. Try again to check access."
        }
    }

    private func startPlayback(_ current: LiveTestPass, resume: Bool) async -> Bool {
        var playable = current
        let reloading = reloadMediaID == current.mediaID
        let retryingMedia = library != nil && player.requiresFreshMediaPass
        error = nil
        if reloading || retryingMedia || current.expiresAt.timeIntervalSinceNow <= (library == nil ? 0 : 5) {
            guard let library else {
                error = "This link expired. Go back and open a new one-time link."
                return false
            }
            do {
                await resumeWriter?.flush(retryUnsaved: retryingMedia && !reloading)
                try Task.checkCancellation()
                guard pass?.mediaID == current.mediaID else { return false }
                let replacement = try await library.refreshPlayback(current, acceptUpdatedCopy: reloading)
                guard !Task.isCancelled, scenePhase != .background, pass?.mediaID == current.mediaID else {
                    Task { await library.release(replacement) }
                    return false
                }
                playable = replacement
                pass = replacement
                resumeWriter = makeResumeWriter(for: replacement, client: library)
                reloadMediaID = nil
                error = nil
                if reloading {
                    player.clearFailure()
                    return false
                }
            } catch LiveLibraryError.copyChanged {
                if !Task.isCancelled {
                    reloadMediaID = current.mediaID
                    error = "A new copy is available. Reload it to continue."
                }
                return false
            } catch FixtureConnectionError.unauthorized {
                if !Task.isCancelled, pass?.mediaID == current.mediaID {
                    playbackAccessLost(.profile)
                }
                return false
            } catch {
                if !Task.isCancelled { self.error = "Couldn’t reopen this video. Check your connection and try again." }
                return false
            }
        }
        guard !Task.isCancelled, scenePhase != .background else { return false }
        let writer = resumeWriter
        guard player.beginLive(playable, resumePosition: resume ? savedPosition(playable) : nil,
                               onCheckpoint: writer.map { $0.offer }) else { return false }
        player.canPlayNext = library != nil && playable.nextWatchPath != nil
        return true
    }

    private func closePass(_ closing: LiveTestPass) {
        guard !closingPass else { return }
        let returnSeries = closing.card.episodeTitle == nil ? nil : passSeries
        closingPass = true
        player.stop()
        let writer = resumeWriter
        let client = library
        // Leave the pre-play page first. The resume flush is a network call and
        // used to hold the player page open for its whole duration.
        pendingReturnSeries = returnSeries
        pass = nil
        passSeries = nil
        passEpisode = nil
        resumeImmediately = false
        resumeWriter = nil
        reloadMediaID = nil
        player.clearFailure()
        error = nil
        Task {
            defer { closingPass = false }
            await writer?.flush()
            await client?.release(closing)
        }
    }

    private func openNext() async throws {
        guard let library, let current = pass else { throw CancellationError() }
        let next: LiveTestPass
        do {
            next = try await library.nextEpisode(after: current)
        } catch FixtureConnectionError.unauthorized {
            if !Task.isCancelled, pass?.mediaID == current.mediaID {
                playbackAccessLost(.profile)
            }
            throw CancellationError()
        }
        let nextWriter = makeResumeWriter(for: next, client: library)
        let previousWriter = resumeWriter
        do {
            try await player.replaceLive(with: next, onCheckpoint: nextWriter.map { $0.offer })
        } catch {
            Task { await library.release(next) }
            throw error
        }
        pass = next
        passEpisode = nil
        resumeWriter = nextWriter
        player.canPlayNext = next.nextWatchPath != nil
        Task {
            await previousWriter?.flush()
            await library.release(current)
        }
    }

    private func instruction(_ number: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 18) {
            Text(number)
                .font(.caption.monospacedDigit().weight(.semibold))
                .foregroundStyle(Color.tvFocus)
                .accessibilityHidden(true)
            Text(text).font(.callout).foregroundStyle(Color.tvMuted)
        }
    }

    private var linkEditor: some View {
        VStack(alignment: .leading, spacing: 26) {
            Text("Enter test link")
                .font(.largeTitle.bold())
                .accessibilityAddTraits(.isHeader)
            Text("Select the field to open the Apple TV keyboard, then paste the link you copied.")
                .font(.body)
                .foregroundStyle(Color.tvMuted)
            SecureField("One-time test link", text: $invitation)
                .textFieldStyle(.plain)
                .focusEffectDisabled()
                .tvInputSurface(focused: focus == .invitation)
                .focused($focus, equals: .invitation)
                .accessibilityIdentifier("live.invitation")
                .onSubmit { if !invitation.isEmpty { focus = .open } }
            if let error { Text(error).foregroundStyle(.yellow) }
            HStack(spacing: 24) {
                Button(connecting ? "Connecting…" : "Connect", action: connect)
                    .buttonStyle(TVActionStyle(focused: focus == .open, tint: .tvFocus))
                    .focusEffectDisabled()
                    .focused($focus, equals: .open)
                    .disabled(connecting || invitation.isEmpty)
                Button("Cancel") { showingEditor = false }
                    .buttonStyle(TVActionStyle(focused: focus == .cancel))
                    .focusEffectDisabled()
                    .focused($focus, equals: .cancel)
                    .disabled(connecting)
            }
        }
        .frame(maxWidth: 950)
        .padding(.horizontal, 70)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.tvBackground.ignoresSafeArea())
        .defaultFocus($focus, .invitation)
        .onExitCommand { if !connecting { showingEditor = false } }
    }

    private func connect() {
        guard !connecting, !invitation.isEmpty else { return }
        let submitted = invitation
        invitation = ""
        error = nil
        connecting = true
        Task { @MainActor in
            defer { connecting = false }
            do {
                if submitted.contains("#library-") {
                    let client = try await LiveLibraryClient.link(invitation: submitted)
                    do {
                        try await addProfile(client)
                    } catch {
                        Task { try? await client.unlink() }
                        throw error
                    }
                } else {
                    resumeImmediately = false
                    pass = try await LiveTestClient(invitation: submitted).redeem()
                }
                showingEditor = false
            } catch FixtureConnectionError.invalidCode {
                error = "Paste the entire one-time link shown in Omakase."
            } catch FixtureConnectionError.invalidOrigin {
                error = "Use a test link from the HTTPS address of your Omakase server."
            } catch {
                self.error = "The test link could not be opened. Check the connection and get a fresh link."
            }
        }
    }
}

private struct TVPairingQR: View {
    let url: URL

    var body: some View {
        if let image = makeImage() {
            Image(uiImage: image).resizable().interpolation(.none)
                .frame(width: 310, height: 310)
                .padding(24)
                .background(.white, in: RoundedRectangle(cornerRadius: 14))
                .accessibilityLabel("QR code for Omakase Plus Apple TV setup")
        }
    }

    private func makeImage() -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(url.absoluteString.utf8)
        guard let code = filter.outputImage,
              let cgImage = CIContext().createCGImage(code, from: code.extent) else { return nil }
        return UIImage(cgImage: cgImage)
    }
}
