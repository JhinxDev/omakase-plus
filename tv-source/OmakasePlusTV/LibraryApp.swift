import QualificationCore
import SwiftUI

extension Color {
    // Omakase Plus viewer colors from cinema.css and the shared streaming tokens.
    static let tvBackground = Color(omakaseHex: 0x090B12)
    static let tvSurface = Color(omakaseHex: 0x121620)
    static let tvRaised = Color(omakaseHex: 0x1B2130)
    static let tvKeyline = Color(omakaseHex: 0x282F40)
    static let tvText = Color(omakaseHex: 0xF4F3F8)
    static let tvMuted = Color(omakaseHex: 0xBDC4D1)
    static let tvAccent = Color(omakaseHex: 0xC3B1FF)
    static let tvFocus = Color(omakaseHex: 0xD2C2FF)
    static let tvOnFocus = Color(omakaseHex: 0x171124)
    static let tvWarning = Color(omakaseHex: 0xE9B565)

    private init(omakaseHex value: UInt32) {
        self.init(.sRGB,
                  red: Double((value >> 16) & 0xff) / 255,
                  green: Double((value >> 8) & 0xff) / 255,
                  blue: Double(value & 0xff) / 255)
    }
}

struct TVActionStyle: ButtonStyle {
    var focused: Bool? = nil
    var tint: Color = .tvText
    var compact = false
    var selected = false

    func makeBody(configuration: Configuration) -> some View {
        Chrome(label: configuration.label, focusOverride: focused, tint: tint,
               compact: compact, selected: selected, isPressed: configuration.isPressed)
    }

    private struct Chrome<Label: View>: View {
        @Environment(\.isFocused) private var isFocused
        @Environment(\.accessibilityReduceMotion) private var reduceMotion
        let label: Label
        let focusOverride: Bool?
        let tint: Color
        let compact: Bool
        let selected: Bool
        let isPressed: Bool

        var body: some View {
            let focused = focusOverride ?? isFocused
            label
                .font(.callout.weight(.semibold))
                .foregroundStyle(focused ? Color.tvOnFocus : selected ? Color.tvFocus : Color.tvText)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .padding(.horizontal, compact ? 18 : 30)
                .frame(minHeight: 68)
                .background {
                    RoundedRectangle(cornerRadius: 10)
                        .fill(focused ? Color.tvFocus : selected ? Color.tvAccent.opacity(0.18) : Color.tvRaised)
                        .overlay {
                            RoundedRectangle(cornerRadius: 10)
                                .fill((focused ? Color.tvOnFocus : tint)
                                    .opacity(isPressed ? 0.15 : focused ? 0 : 0.08))
                        }
                }
                .overlay {
                    RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(focused ? Color.tvAccent : selected ? Color.tvAccent.opacity(0.6) : Color.tvKeyline,
                                      lineWidth: focused ? 3 : 2)
                }
                .scaleEffect(focused && !reduceMotion ? 1.045 : 1)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: focused)
        }
    }
}

struct TVLibraryTileStyle: ButtonStyle {
    var focused = false
    var longPress: (() -> Void)? = nil

    func makeBody(configuration: Configuration) -> some View {
        Chrome(focused: focused, pressed: configuration.isPressed,
               longPress: longPress, label: configuration.label)
    }

    private struct Chrome<Label: View>: View {
        @Environment(\.accessibilityReduceMotion) private var reduceMotion
        let focused: Bool
        let pressed: Bool
        let longPress: (() -> Void)?
        let label: Label
        @State private var press: Task<Void, Never>?

        var body: some View {
            label
                .opacity(pressed ? 0.82 : 1)
                .shadow(color: focused ? .black.opacity(0.5) : .clear, radius: 9, y: 6)
                .scaleEffect(focused && !reduceMotion ? 1.05 : 1)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: focused)
                .onChange(of: pressed) { _, down in
                    press?.cancel()
                    press = nil
                    guard down, let longPress else { return }
                    press = Task { @MainActor in
                        try? await Task.sleep(for: .milliseconds(550))
                        guard !Task.isCancelled else { return }
                        longPress()
                    }
                }
                .onDisappear { press?.cancel() }
        }
    }
}

extension View {
    func tvInputSurface(focused: Bool) -> some View {
        self
            .foregroundStyle(focused ? Color.tvOnFocus : Color.tvText)
            .focusEffectDisabled()
            .padding(.horizontal, 24)
            .frame(minHeight: 70)
            .background(Color.tvSurface, in: .rect(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(focused ? Color.tvFocus : Color.tvKeyline, lineWidth: 2)
                    .allowsHitTesting(false)
            }
    }
}

struct TVPlaybackLoading: View {
    @State private var showsIndicator = false
    @State private var showsSlowAnimation = false
    var message = "Starting playback…"

    var body: some View {
        ZStack {
            Color.tvBackground.opacity(0.94).ignoresSafeArea()
            if showsIndicator {
                VStack(spacing: 22) {
                    if showsSlowAnimation {
                        TVLoadingOrbit()
                            .frame(width: 132, height: 132)
                            .accessibilityHidden(true)
                        Text("Still preparing…")
                            .font(.callout.weight(.medium))
                            .foregroundStyle(Color.tvText)
                            .accessibilityIdentifier("playback.loading")
                    } else {
                        ProgressView()
                            .progressViewStyle(.circular)
                            .controlSize(.large)
                            .tint(Color.tvAccent)
                            .accessibilityHidden(true)
                        Text(message)
                            .font(.callout.weight(.medium))
                            .foregroundStyle(Color.tvText)
                            .accessibilityIdentifier("playback.loading")
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .allowsHitTesting(false)
        .task {
            // Avoid a flash of text when the player is ready almost immediately.
            try? await Task.sleep(for: .milliseconds(200))
            guard !Task.isCancelled else { return }
            showsIndicator = true
            try? await Task.sleep(for: .seconds(10))
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.35)) { showsSlowAnimation = true }
        }
    }
}

struct TVLoadingOrbit: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var spinning = false

    var body: some View {
        ZStack {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .trim(from: 0.06, to: 0.34)
                    .stroke(
                        AngularGradient(
                            colors: [Color.tvAccent.opacity(0.05), Color.tvAccent],
                            center: .center
                        ),
                        style: StrokeStyle(lineWidth: 6, lineCap: .round)
                    )
                    .rotationEffect(Angle(degrees: Double(index) * 120))
                    .rotationEffect(Angle(degrees: spinning ? 360 : 0))
                    .animation(
                        reduceMotion ? nil : .linear(duration: 3.6).repeatForever(autoreverses: false),
                        value: spinning
                    )
            }
            Circle()
                .fill(Color.tvAccent.opacity(0.18))
                .frame(width: 22, height: 22)
                .scaleEffect(spinning ? 1.35 : 0.75)
                .animation(
                    reduceMotion ? nil : .easeInOut(duration: 1.6).repeatForever(autoreverses: true),
                    value: spinning
                )
        }
        .onAppear { spinning = true }
        .accessibilityHidden(true)
    }
}

struct TVSynopsisScreen: View {
    let title: String
    let synopsis: String
    let close: () -> Void
    @FocusState private var doneFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text(title).font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
            ScrollView {
                Text(synopsis)
                    .font(.title3)
                    .lineSpacing(8)
                    .foregroundStyle(Color.tvMuted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("library.synopsis")
            }
            Button("Done", action: close)
                .buttonStyle(TVActionStyle(compact: true))
                .focusEffectDisabled()
                .focused($doneFocused)
        }
        .frame(maxWidth: 1180, maxHeight: .infinity)
        .padding(.vertical, 56)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.tvBackground.ignoresSafeArea())
        .foregroundStyle(Color.tvText)
        .defaultFocus($doneFocused, true)
        .onExitCommand(perform: close)
    }
}

@main
struct OmakasePlusTVApp: App {
    var body: some Scene {
        WindowGroup {
            TVLaunchScreen().preferredColorScheme(.dark)
        }
    }
}

@MainActor
private struct TVLaunchScreen: View {
    var body: some View {
        ZStack {
            Color.tvBackground
                .ignoresSafeArea()
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("--qualification-fixtures") {
                FixtureLibrary()
            } else {
                LiveTestScreen()
            }
            #else
            LiveTestScreen()
            #endif
        }
    }
}

@MainActor
private struct FixtureLibrary: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var session = PlaybackSession(resumeKey: "omakase.tv.fixture.resume.v1")
    @State private var selected: LibraryCard?
    @State private var lastCard: LibraryIdentity?
    @State private var showingTestLibrary = false
    private let fixture: LibraryFixture? = {
        guard let baseline = Bundle.main.url(forResource: "baseline", withExtension: "mp4"),
              let tracks = Bundle.main.url(forResource: "track-check", withExtension: "mp4")
        else { return nil }
        return LibraryFixture(baselineURL: baseline, tracksURL: tracks)
    }()

    var body: some View {
        Group {
            if showingTestLibrary {
                NetworkLibraryScreen { showingTestLibrary = false }
            } else if let fixture {
                if let selected {
                    let next = fixture.adjacentEpisode(to: selected, step: 1)
                    TitleScreen(
                        card: selected, saved: resumePosition(for: selected, in: session), session: session,
                        previous: fixture.adjacentEpisode(to: selected, step: -1),
                        next: next,
                        selectEpisode: {
                            self.selected = $0
                            session.clearFailure()
                        },
                        sampleDescription: selected.id.mediaVersion == LibraryFixture.tracksVersion
                            ? "Track check · 30 seconds · Test tones and selectable captions"
                            : "Sample video · 30 seconds · Silent",
                        playNext: {
                            guard let current = self.selected,
                                  let card = fixture.adjacentEpisode(to: current, step: 1),
                                  let media = fixture.media(for: card.id) else { throw CancellationError() }
                            try await session.replace(with: media, card: card)
                            session.canPlayNext = fixture.adjacentEpisode(to: card, step: 1) != nil
                            self.selected = card
                        },
                        startPlayback: { resume in
                            guard let media = fixture.media(for: selected.id),
                                  session.begin(media: media, resume: resume, card: selected) else { return false }
                            session.canPlayNext = next != nil
                            return true
                        },
                        back: { self.selected = nil }
                    )
                    .id(lastCard)
                } else {
                    LibraryShelf(
                        cards: fixture.cards, returnTo: lastCard, heading: "Your library",
                        description: "Offline preview · Synthetic playback and track checks",
                        actionTitle: "Connect to test library", action: { showingTestLibrary = true },
                        resume: { resumePosition(for: $0, in: session) }
                    ) { card in
                        lastCard = card.id
                        session.clearFailure()
                        selected = card
                    }
                }
            } else {
                Text("Sample video is missing. Rebuild OmakasePlusTV from the complete source package.")
                    .font(.title3)
            }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            session.scenePhaseChanged(AppScenePhase(phase))
        }
    }
}

@MainActor
struct LibraryShelf: View {
    let cards: [LibraryCard]
    let returnTo: LibraryIdentity?
    let heading: String
    let description: String
    let actionTitle: String
    let action: () -> Void
    let resume: (LibraryCard) -> Double?
    let select: (LibraryCard) -> Void
    @FocusState private var focus: LibraryIdentity?

    var body: some View {
        VStack(alignment: .leading, spacing: 30) {
            Text("OMAKASE PLUS").font(.headline.weight(.semibold)).tracking(5)
                .foregroundStyle(.white.opacity(0.7))
            Text(heading).font(.largeTitle.bold())
                .accessibilityAddTraits(.isHeader)
            Text(description)
                .font(.callout).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 28) {
                ForEach(cards) { card in
                    Button { select(card) } label: {
                        VStack(alignment: .leading, spacing: 14) {
                            Image(systemName: card.kind.symbol)
                                .font(.system(size: 64, weight: .light))
                                .foregroundStyle(card.kind.tint)
                                .frame(height: 105)
                                .accessibilityHidden(true)
                            Text(card.kind.label.uppercased()).font(.caption.weight(.semibold)).tracking(2)
                            Text(card.title).font(.title3.bold())
                            Text(resumeLabel(for: card)).font(.callout).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, minHeight: 245, alignment: .leading)
                        .padding(28)
                        .background(Color.white.opacity(focus == card.id ? 0.11 : 0.07), in: RoundedRectangle(cornerRadius: 24))
                        .overlay {
                            RoundedRectangle(cornerRadius: 24)
                                .strokeBorder(focus == card.id ? Color.tvFocus : Color.white.opacity(0.14), lineWidth: 2)
                        }
                    }
                    .buttonStyle(.plain)
                    .focusEffectDisabled()
                    .focused($focus, equals: card.id)
                    .accessibilityLabel("\(card.title), \(card.kind.label)")
                    .accessibilityValue(resumeLabel(for: card))
                    .accessibilityIdentifier("fixture.card.\(card.kind.rawValue)")
                }
            }
            Button(actionTitle, action: action)
        }
        .padding(.horizontal, 70)
        .defaultFocus($focus, returnTo ?? cards.first?.id)
    }

    private func resumeLabel(for card: LibraryCard) -> String {
        if let saved = resume(card) {
            return "Resume at \(PlaybackSession.clock(saved))"
        }
        return card.episodeTitle ?? "Movie"
    }
}

@MainActor
struct TitleScreen: View {
    @Environment(\.scenePhase) private var scenePhase
    let card: LibraryCard
    let saved: Double?
    @ObservedObject var session: PlaybackSession
    var previous: LibraryCard? = nil
    var next: LibraryCard? = nil
    var selectEpisode: ((LibraryCard) -> Void)? = nil
    var sampleDescription = "Sample video · 30 seconds · Silent"
    var backTitle = "Back to library"
    var isSaving = false
    var needsReload = false
    var externalError: String? = nil
    var showsTechnicalErrors = true
    var playNext: (() async throws -> Void)? = nil
    var artwork: TVLibraryArtwork? = nil
    var overview: String? = nil
    var episodeName: String? = nil
    var runtimeSeconds: Int? = nil
    var audioLanguages: [String] = []
    var subtitleLanguages: [String] = []
    var resumeImmediately = false
    let startPlayback: (Bool) async -> Bool
    let back: () -> Void
    @State private var showingPlayer = false
    @State private var showingSynopsis = false
    @State private var attemptedDirectPlayback = false
    @State private var passClosed = false
    @State private var dismissedByPlayer = false
    @State private var launch: Task<Void, Never>?
    @FocusState private var focus: Action?

    private enum Action: Hashable { case play, restart, synopsis, library }

    private var summary: String { overview ?? card.synopsis }

    private var episodeHeading: String? {
        card.episodeTitle.map { episode in
            episodeName.map { "\(episode) · \($0)" } ?? episode
        }
    }

    private var preparingDirectPlayback: Bool {
        resumeImmediately && (!attemptedDirectPlayback || launch != nil || showingPlayer)
    }

    var body: some View {
        GeometryReader { space in
            ZStack(alignment: .leading) {
                TitleArtworkPanel(artwork: artwork)
                if !preparingDirectPlayback {
                    titleInformation
                        .frame(width: space.size.width * 0.64, alignment: .leading)
                        .frame(maxHeight: .infinity, alignment: .topLeading)
                        .padding(.horizontal, 76).padding(.top, 104)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .overlay {
            if preparingDirectPlayback || launch != nil || showingPlayer {
                TVPlaybackLoading(message: needsReload ? "Loading updated video…" : "Starting playback…")
            }
        }
        .defaultFocus($focus, .play)
        .onAppear {
            guard resumeImmediately, !attemptedDirectPlayback else { return }
            attemptedDirectPlayback = true
            start(resume: true)
        }
        .onExitCommand {
            if showingPlayer || showingSynopsis || isSaving { return }
            if launch != nil {
                launch?.cancel()
                launch = nil
                session.stop()
                focus = .play
                return
            }
            back()
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            session.scenePhaseChanged(AppScenePhase(phase))
            if phase == .background { launch?.cancel() }
        }
        .onChange(of: isSaving) { _, saving in
            if !saving && launch == nil && !showingPlayer { focus = .play }
        }
        .onChange(of: card.id) { _, _ in
            passClosed = false
            if !showingPlayer { focus = .play }
        }
        .onDisappear { launch?.cancel() }
        .fullScreenCover(isPresented: $showingSynopsis, onDismiss: { focus = .synopsis }) {
            TVSynopsisScreen(title: episodeHeading ?? card.title, synopsis: summary) {
                showingSynopsis = false
            }
        }
        .fullScreenCover(isPresented: $showingPlayer, onDismiss: {
            session.stop()
            let leave = dismissedByPlayer || (
                session.errorMessage == nil && externalError == nil && !needsReload && !isSaving
            )
            dismissedByPlayer = false
            guard !passClosed else { return }
            if leave {
                passClosed = true
                back()
            } else {
                focus = .play
            }
        }) {
            PlayerScreen(session: session, showsDiagnostics: false, playNext: playNext) {
                dismissedByPlayer = true
                showingPlayer = false
            }
        }
    }

    private var titleInformation: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 20) {
                Button(action: back) {
                    Image(systemName: "chevron.left").frame(width: 30)
                }
                .buttonStyle(TVActionStyle(focused: focus == .library, compact: true))
                .focusEffectDisabled()
                .focused($focus, equals: .library)
                .accessibilityLabel(backTitle)
                .accessibilityIdentifier("fixture.back")
                .disabled(launch != nil || isSaving)
                Label(card.kind.label, systemImage: card.kind.symbol)
                    .font(.headline).foregroundStyle(card.kind.tint)
            }
            Text(card.title).font(.largeTitle.bold()).lineLimit(2)
                .accessibilityAddTraits(.isHeader)
            if let episodeHeading {
                Text(episodeHeading).font(.title3).lineLimit(2)
            }
            if runtimeSeconds != nil || !audioLanguages.isEmpty || !subtitleLanguages.isEmpty {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 20) { playbackMetadata }
                    VStack(alignment: .leading, spacing: 8) { playbackMetadata }
                }
                .font(.callout).foregroundStyle(Color.tvMuted)
                .lineLimit(1)
            }
            HStack(spacing: 18) {
                Button {
                    start(resume: !needsReload && (resumeImmediately || saved != nil))
                } label: {
                    Label(primaryLabel, systemImage: needsReload ? "arrow.clockwise" : "play.fill")
                }
                .buttonStyle(TVActionStyle(focused: focus == .play, tint: .tvFocus, compact: true))
                .focusEffectDisabled()
                .focused($focus, equals: .play)
                .accessibilityIdentifier("fixture.play")
                if saved != nil && !needsReload && !resumeImmediately {
                    Button("Play from start") { start(resume: false) }
                        .buttonStyle(TVActionStyle(focused: focus == .restart, compact: true))
                        .focusEffectDisabled()
                        .focused($focus, equals: .restart)
                        .accessibilityIdentifier("fixture.restart")
                }
                if episodeHeading != nil && !summary.isEmpty {
                    Button("Episode details") { showingSynopsis = true }
                        .buttonStyle(TVActionStyle(focused: focus == .synopsis, compact: true))
                        .focusEffectDisabled()
                        .focused($focus, equals: .synopsis)
                }
            }
            .disabled(launch != nil || isSaving)
            if !summary.isEmpty {
                Text(summary)
                    .font(.callout)
                    .foregroundStyle(Color.tvMuted)
                    .lineLimit(4)
            }
            if let selectEpisode, previous != nil || next != nil {
                HStack(spacing: 26) {
                    if let previous {
                        Button("Previous episode") { selectEpisode(previous) }
                            .accessibilityIdentifier("fixture.previous")
                    }
                    if let next {
                        Button("Next episode") { selectEpisode(next) }
                            .accessibilityIdentifier("fixture.next")
                    }
                }
            }
            if let saved, artwork != nil && !needsReload {
                Text("Resume from \(PlaybackSession.clock(saved))")
                    .font(.callout.monospacedDigit()).foregroundStyle(Color.tvMuted)
            }
            if isSaving && launch == nil {
                ProgressView("Saving resume")
            }
            if let error = externalError ?? session.errorMessage {
                let message = externalError ?? (showsTechnicalErrors ? error :
                    "Couldn’t play this video. Retry playback or go back and choose another title.")
                Label(message, systemImage: "exclamationmark.circle")
                    .font(.callout).foregroundStyle(Color.tvWarning)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(message)
                    .accessibilityIdentifier("fixture.playback-error")
            }
            if !sampleDescription.isEmpty {
                Text(sampleDescription)
                    .font(.callout).foregroundStyle(Color.tvMuted)
            }
        }
    }

    private var primaryLabel: String {
        if needsReload { return "Reload video" }
        if session.errorMessage != nil && !showsTechnicalErrors { return "Retry playback" }
        return saved.map { artwork == nil ? "Resume at \(PlaybackSession.clock($0))" : "Continue Watching" } ?? "Play"
    }

    @ViewBuilder
    private var playbackMetadata: some View {
        if let runtimeSeconds {
            Text(runtimeSeconds < 60 ? "\(runtimeSeconds) sec" : "\((runtimeSeconds + 59) / 60) min")
                .monospacedDigit()
        }
        if !audioLanguages.isEmpty {
            Text("Audio · \(audioLanguages.joined(separator: ", "))")
        }
        if !subtitleLanguages.isEmpty {
            Text("Subtitles · \(subtitleLanguages.joined(separator: ", "))")
        }
    }

    private func start(resume: Bool) {
        guard launch == nil, !isSaving else { return }
        launch = Task { @MainActor in
            let started = await startPlayback(resume)
            if started && !Task.isCancelled && scenePhase != .background {
                showingPlayer = true
            } else if started {
                session.stop()
            }
            launch = nil
            if !showingPlayer { focus = .play }
        }
    }
}

@MainActor
private struct TitleArtworkPanel: View {
    let artwork: TVLibraryArtwork?

    var body: some View {
        Color.tvBackground
            .overlay {
                if let artwork { artwork } else { TVArtworkPlaceholder(compact: true) }
            }
            .overlay {
                LinearGradient(stops: [.init(color: .tvBackground, location: 0),
                                       .init(color: .tvBackground.opacity(0.96), location: 0.25),
                                       .init(color: .tvBackground.opacity(0.92), location: 0.74),
                                       .init(color: .tvBackground.opacity(0.15), location: 1)],
                               startPoint: .leading, endPoint: .trailing)
            }
            .overlay {
                LinearGradient(colors: [.clear, .tvBackground], startPoint: .center, endPoint: .bottom)
            }
            .ignoresSafeArea()
            .accessibilityHidden(true)
    }
}

struct TVBrandMark: View {
    var size: CGFloat = 46

    var body: some View {
        Circle().trim(from: 0, to: 0.84)
            .stroke(Color.tvAccent, style: StrokeStyle(lineWidth: max(2, size * 0.075), lineCap: .round))
            .rotationEffect(.degrees(-29))
            .frame(width: size, height: size)
            .overlay(alignment: .topTrailing) {
                Image(systemName: "plus").font(.system(size: size * 0.34, weight: .medium))
                    .foregroundStyle(Color.tvAccent)
                    .offset(x: size * 0.08, y: -size * 0.08)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Omakase Plus")
            .accessibilityAddTraits(.isImage)
    }
}

struct TVArtworkPlaceholder: View {
    var compact = false
    var showsGlow = true

    var body: some View {
        Color.tvSurface.overlay {
            if showsGlow {
                RadialGradient(colors: [Color.tvAccent.opacity(0.13), .clear],
                               center: .center, startRadius: 0, endRadius: compact ? 145 : 320)
            }
            VStack(spacing: compact ? 12 : 24) {
                TVBrandMark(size: compact ? 46 : 88)
                Text("OMAKASE PLUS")
                    .font((compact ? Font.caption2 : Font.caption).weight(.semibold))
                    .tracking(compact ? 2.5 : 4)
                    .foregroundStyle(Color.tvMuted)
            }
        }
        .accessibilityHidden(true)
    }
}

@MainActor
private func resumePosition(for card: LibraryCard, in session: PlaybackSession) -> Double? {
    guard let record = session.resumeRecord(for: card.identity.resumeIdentity),
          !record.reachedEnd, record.position > 0, record.position < record.duration else { return nil }
    return record.position
}

private extension LibraryKind {
    var label: String {
        switch self {
        case .anime: return "Anime"
        case .movie: return "Movie"
        case .cloud: return "Cloud"
        case .tv: return "TV"
        }
    }

    var symbol: String {
        switch self {
        case .anime: return "sparkles"
        case .movie: return "film"
        case .cloud: return "cloud"
        case .tv: return "mountain.2"
        }
    }

    var tint: Color {
        switch self {
        case .anime: return Color(red: 0.77, green: 0.68, blue: 1)
        case .movie: return Color(red: 1, green: 0.72, blue: 0.55)
        case .cloud: return Color(red: 0.77, green: 0.68, blue: 1)
        case .tv: return Color(red: 0.54, green: 0.88, blue: 0.85)
        }
    }
}
