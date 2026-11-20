import XCTest

// Connected qualification for Omakase Plus on Apple TV.
//
// WHAT THIS PROVES
//   The real app, paired against the loopback qualification fixture, can: request
//   and status-check a catalog title, search the catalog, resume Continue
//   Watching, deep-link from Top Shelf to Details and Resume, survive a paused
//   viewer whose server plan was reclaimed, and ignore a stale close of an older
//   copy. Focus and playback are asserted in separate groups, so a playback start
//   can never cover a missing focus return, or the reverse.
//
// WHAT THIS DOES NOT PROVE
//   That the artifact is signed, that it installed, or that a physical Home Screen
//   shows the Top Shelf extension. Those are the install and physical acceptance
//   steps in docs/qualification/APPLE-TV-QUALIFICATION.md.
//
// HOST DEPENDENCY
//   Every server-side assertion reads the companion fixture documented in
//   scripts/fixtures/tvos-connected/README.md. A fixture that is down, or answers
//   a control with a non-200 status, FAILS the test. It never skips: the audited
//   fixture turned a non-200 into a skip, so a broken control read as "not
//   proven" and a suite that proved nothing still exited green. The only skips
//   left are capabilities this runtime genuinely cannot supply, each with a named
//   reason, and `XCTSkip` marks the test skipped, never passed.
//
// RUN (macOS with the tvOS SDK; UNVERIFIED ON WINDOWS BY DESIGN)
//   bash scripts/fixtures/tvos-connected/start_connected_fixture.sh <udid> <scratch>
//   xcodebuild test -only-testing:OmakasePlusTVUITests/ConnectedQualification \
//     OMAKASE_QUALIFICATION_ORIGIN=https://127.0.0.1:8854
//
// IDENTIFIER CONTRACT (all of these already exist under tv-source/OmakasePlusTV)
//   live.enter, live.showCode .......... pairing setup
//   for-you, for-you.title-back ........ For You recommendation details
//   library.search, library.search.field  catalog search
//   continue-<entry> ................... Continue Watching cards
//   playback.loading ................... player startup surface
//   library.error ...................... library failure surface
//   Sidebar labels "For You", "Movies", "TV Shows" are the app's visible tab text.
//   native-home and native-requests ... projected Home and Requests screens.

final class ConnectedQualification: XCTestCase {

    // MARK: - Configuration

    private var origin: String {
        ProcessInfo.processInfo.environment["OMAKASE_QUALIFICATION_ORIGIN"]
            ?? "https://127.0.0.1:8854"
    }

    /// Bounded long-pause hold for the idle-plan regression.
    private var longPauseSeconds: Int {
        Int(ProcessInfo.processInfo.environment["OMAKASE_LONG_PAUSE_SECONDS"] ?? "45") ?? 45
    }

    /// Seconds a freshly opened pass may take to report its first checkpoint.
    private let playbackDeadline = 30

    /// Seconds a recovered session must stay quiet before "one pass" is proven.
    private let quietWindow = 5

    /// Thrown after an assertion has already failed, to stop the test.
    private struct QualificationStop: Error {}

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    // MARK: - Fixture transport (hard failure, never a skip)

    @discardableResult
    private func fixture(_ path: String, method: String = "GET",
                        body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let url = URL(string: origin + path) else {
            XCTFail("Fixture origin \(origin) does not form a URL for \(path)")
            throw QualificationStop()
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 12
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            XCTFail("""
                The companion fixture did not answer \(method) \(origin)\(path): \
                \(error.localizedDescription). Start it with \
                scripts/fixtures/tvos-connected/start_connected_fixture.sh <simulator-udid> \
                <scratch-dir> and point OMAKASE_QUALIFICATION_ORIGIN at it. A missing \
                fixture is a harness prerequisite, so this test fails: an unproven \
                recovery check must never be reported as a pass.
                """)
            throw QualificationStop()
        }
        guard let http = response as? HTTPURLResponse else {
            XCTFail("\(origin)\(path) answered without an HTTP status")
            throw QualificationStop()
        }
        guard http.statusCode == 200 else {
            XCTFail("""
                The fixture answered \(http.statusCode) for \(method) \(origin)\(path). \
                Check the control name and its method in \
                scripts/fixtures/tvos-connected/README.md: every control that changes \
                state is a POST. A control that does not answer must fail, not skip.
                """)
            throw QualificationStop()
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            XCTFail("The fixture answered \(method) \(origin)\(path) with a non-object body")
            throw QualificationStop()
        }
        return object
    }

    private func freshPasses(_ state: [String: Any]) -> Int {
        state["fresh_passes_opened"] as? Int ?? -1
    }

    private func checkpoint(_ state: [String: Any]) -> Double? {
        state["checkpoint"] as? Double
    }

    private func resumePosition(_ state: [String: Any]) -> Double? {
        state["resume_position"] as? Double
    }

    private func gateOwner(_ state: [String: Any]) -> String? {
        state["gate_owner"] as? String
    }

    private func passes(_ state: [String: Any]) -> [[String: Any]] {
        state["passes"] as? [[String: Any]] ?? []
    }

    private func pass(_ state: [String: Any], media: String) -> [String: Any]? {
        passes(state).first { $0["media_id"] as? String == media }
    }

    private func renditions(_ state: [String: Any], media: String) -> [String] {
        pass(state, media: media)?["renditions"] as? [String] ?? []
    }

    private func releases(_ state: [String: Any], media: String) -> Int {
        (state["qa_calls"] as? [[String: Any]] ?? []).filter {
            $0["action"] as? String == "release" && $0["media_id"] as? String == media
        }.count
    }

    private func requests(_ state: [String: Any]) -> [[String: Any]] {
        state["requests"] as? [[String: Any]] ?? []
    }

    private func calls(_ state: [String: Any], _ action: String) -> [[String: Any]] {
        (state["qa_calls"] as? [[String: Any]] ?? []).filter {
            $0["action"] as? String == action
        }
    }

    // MARK: - Launch

    @MainActor
    private func launchConnected() throws -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-omakase.tv.server-origin", origin]
        app.launch()
        let setup = app.buttons["live.enter"]
        if setup.waitForExistence(timeout: 4) {
            try select(setup, in: app)
            try select(app.buttons["live.showCode"], in: app)
        }
        XCTAssertTrue(app.buttons["For You"].waitForExistence(timeout: 20),
                      "The paired library must open against \(origin)")
        return app
    }

    // MARK: - Remote focus driving

    @MainActor
    private func select(_ target: XCUIElement, in app: XCUIApplication) throws {
        guard target.waitForExistence(timeout: 12) else {
            capture("missing-target", app)
            XCTFail("Remote target missing: \(target)")
            throw QualificationStop()
        }
        for step in 0..<18 {
            if target.hasFocus {
                XCUIRemote.shared.press(.select)
                return
            }
            let current = app.descendants(matching: .any)
                .matching(NSPredicate(format: "hasFocus == true")).firstMatch
            guard current.exists else {
                XCUIRemote.shared.press(.down)
                continue
            }
            let from = current.frame
            let to = target.frame
            // Move between nonoverlapping rows before attempting horizontal
            // alignment. Header controls need not sit above the first card.
            let direction: XCUIRemote.Button
            if to.minY >= from.maxY - 8 {
                direction = .down
            } else if to.maxY <= from.minY + 8 {
                direction = .up
            } else {
                direction = to.midX > from.midX ? .right : .left
            }
            print("QA_FOCUS step=\(step) from=\(current.label) fromFrame=\(from) "
                  + "target=\(target.identifier) targetFrame=\(to) direction=\(direction)")
            XCUIRemote.shared.press(direction)
        }
        capture("focus-unreachable", app)
        XCTFail("Remote focus could not reach \(target.identifier): \(target.label)")
        throw QualificationStop()
    }

    @MainActor
    private func assertFocus(_ target: XCUIElement, name: String,
                             in app: XCUIApplication) async throws {
        let expectation = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND hasFocus == true"),
            object: target)
        await fulfillment(of: [expectation], timeout: 5)
        guard target.exists, target.hasFocus else {
            capture(name + "-focus-failed", app)
            XCTFail("Focus assertion failed: \(name) on \(target.identifier)")
            throw QualificationStop()
        }
        capture(name, app)
    }

    @MainActor
    private func capture(_ name: String, _ app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        let hierarchy = XCTAttachment(string: app.debugDescription)
        hierarchy.name = name + "-hierarchy"
        hierarchy.lifetime = .keepAlways
        add(hierarchy)
    }

    /// Some tvOS runtimes omit the post-player accessibility focus flag even
    /// after SwiftUI reapplies the navigation focus style. In that case, prove
    /// the real remote moves to the adjacent tab and back. TVActionStyle scales
    /// the focused button by 1.045; the selected tab style does not scale it.
    @MainActor
    private func assertCategoryReturn(_ target: XCUIElement, right: XCUIElement,
                                      name: String, in app: XCUIApplication) async throws {
        guard target.exists, target.isEnabled, right.exists, right.isEnabled else {
            capture(name + "-category-missing", app)
            XCTFail("The returned category and its right-hand neighbor must be enabled")
            throw QualificationStop()
        }
        if target.hasFocus {
            try await assertFocus(target, name: name, in: app)
            return
        }

        let returnedTargetWidth = target.frame.width
        let unfocusedRightWidth = right.frame.width
        capture(name + "-before-remote", app)
        XCUIRemote.shared.press(.right)
        func rightResponded() -> Bool {
            target.frame.width * 1.025 < returnedTargetWidth
                && (right.hasFocus || right.frame.width > unfocusedRightWidth * 1.025)
        }
        for _ in 0..<12 where !rightResponded() {
            try await Task.sleep(for: .milliseconds(250))
        }
        guard rightResponded() else {
            capture(name + "-right-failed", app)
            XCTFail("Right must remove the return category's focus style and focus its right-hand neighbor")
            throw QualificationStop()
        }
        let unfocusedTargetWidth = target.frame.width
        let focusedRightWidth = right.frame.width
        capture(name + "-right", app)

        XCUIRemote.shared.press(.left)
        func returned() -> Bool {
            right.frame.width * 1.025 < focusedRightWidth
                && (target.hasFocus || target.frame.width > unfocusedTargetWidth * 1.025)
        }
        for _ in 0..<12 where !returned() {
            try await Task.sleep(for: .milliseconds(250))
        }
        guard returned() else {
            capture(name + "-left-failed", app)
            XCTFail("Left must restore the category's focus style from its right-hand neighbor")
            throw QualificationStop()
        }
        capture(name + "-left", app)
    }

    // MARK: - Details controls

    /// For You recommendation details: the app sets `for-you.<key>` on each action.
    /// SwiftUI on tvOS also inherits the container identifier onto its children, so
    /// a label match is kept as the fallback.
    @MainActor
    private func detailsAction(_ key: String, label: String,
                              in app: XCUIApplication) -> XCUIElement {
        let identified = app.buttons.matching(
            NSPredicate(format: "identifier == %@", "for-you." + key)).firstMatch
        if identified.exists { return identified }
        return app.buttons.matching(
            NSPredicate(format: "identifier == %@ AND label == %@", "for-you", label)
        ).firstMatch
    }

    /// The saved-series details overlay, opened by a Top Shelf "details" deep link.
    ///
    /// This overlay is NOT the For You recommendation details: it has no
    /// `for-you.title-back` action. Its own visible controls are "Play now" (the
    /// default focus) and "Close", and Menu dismisses it through the screen's
    /// `onExitCommand`. The audited fixture asserted `for-you.title-back` here,
    /// which the destination never presents, so that check could not pass.
    @MainActor
    private func savedSeriesOverlayClose(in app: XCUIApplication) -> XCUIElement {
        app.buttons.matching(NSPredicate(format: "label == %@", "Close")).firstMatch
    }

    @MainActor
    private func continueCard(in app: XCUIApplication) -> XCUIElement {
        app.buttons.matching(
            NSPredicate(format: "identifier BEGINSWITH %@", "continue-")).firstMatch
    }

    // MARK: - AVKit state

    @MainActor
    private func assertPlayerSurface(_ app: XCUIApplication, name: String) {
        XCTAssertFalse(app.descendants(matching: .any)["library.error"].exists,
                       "A reclaimed plan must not surface as a library error")
        XCTAssertFalse(app.descendants(matching: .any)["playback.loading"].exists,
                       "The player presentation must not restart its startup surface")
        let libraryNavigation = app.buttons["For You"]
        XCTAssertFalse(libraryNavigation.exists && libraryNavigation.isEnabled,
                       "Playback must keep the library from receiving interaction")
        capture(name + "-presented", app)
    }

    private func reported(_ control: String, for mediaID: String, in state: [String: Any]) -> Bool {
        calls(state, "diagnostic").contains {
            $0["media_id"] as? String == mediaID && $0["time_control"] as? String == control
        }
    }

    /// Require the real AVPlayer pause diagnostic and its stable playback
    /// position across a periodic checkpoint. AVKit's button labels are
    /// unavailable on some simulator runtimes.
    @MainActor
    private func waitForPausedCheckpoint(after playing: [String: Any], mediaID: String,
                                         in app: XCUIApplication) async throws -> [String: Any] {
        let playingSequence = playing["checkpoint_seq"] as? Int ?? 0
        var paused = playing
        for _ in 0..<20 {
            paused = try await fixture("/qa/state")
            if reported("paused", for: mediaID, in: paused),
               (paused["checkpoint_seq"] as? Int ?? 0) > playingSequence { break }
            try await Task.sleep(for: .milliseconds(250))
        }
        guard reported("paused", for: mediaID, in: paused),
              (paused["checkpoint_seq"] as? Int ?? 0) > playingSequence,
              let position = checkpoint(paused) else {
            capture("06-pause-checkpoint-missing", app)
            XCTFail("Play/Pause did not produce the app's pause diagnostic and checkpoint")
            throw QualificationStop()
        }
        let held = try await stretchQuiet(seconds: 6)
        XCTAssertEqual(gateOwner(held), mediaID, "The paused pass must remain current")
        XCTAssertEqual(pass(held, media: mediaID)?["plan"] as? String, "live",
                       "A playback failure cannot stand in for pause proof")
        XCTAssertEqual(freshPasses(held), freshPasses(playing))
        guard let heldPosition = checkpoint(held) else {
            XCTFail("The paused media position must remain observable")
            throw QualificationStop()
        }
        XCTAssertEqual(heldPosition, position, accuracy: 0.4,
                       "The engine must stay at its paused position for a checkpoint interval")
        assertPlayerSurface(app, name: "06-paused")
        return held
    }

    // MARK: - Track selection

    /// Choose an audio or subtitle rendition while the player is paused.
    ///
    /// Two surfaces can expose track selection and neither has been exercised on a
    /// tvOS build from this host, so both are tried in a documented order: the
    /// app's own native transport selector (`direct.control.*`, `direct.track.*`)
    /// first, then AVKit's own transport menu by visible label. The assertion does
    /// not trust either surface: it reads which rendition playlist the player
    /// actually fetched, which is the fixture's `renditions` counter.
    @MainActor
    private func chooseTrack(kind: String, choice: String,
                             in app: XCUIApplication) async throws {
        let title = kind == "subtitle" ? "Subtitles" : "Audio"
        let labels = kind == "subtitle"
            ? ["Subtitles", "Subtitles and Captions", "Closed Captions", "Captions"]
            : ["Audio", "Audio Options", "Audio Track", "Audio Tracks"]
        let candidates = [
            app.buttons["direct.control." + title.lowercased()],
            app.buttons.matching(NSPredicate(
                format: "label IN[c] %@ OR identifier IN[c] %@",
                argumentArray: [labels, labels]
            )).firstMatch,
        ]
        var opener = candidates.first(where: { $0.exists && $0.isEnabled })
        for _ in 0..<2 where opener == nil {
            // Up reveals the playback bar and a second Up reaches its options.
            // Neither press is a playback-state toggle.
            XCUIRemote.shared.press(.up)
            try await Task.sleep(for: .milliseconds(450))
            opener = candidates.first(where: { $0.exists && $0.isEnabled })
        }
        if opener == nil {
            opener = candidates.first(where: { $0.waitForExistence(timeout: 2) && $0.isEnabled })
        }
        guard let opener else {
            capture("track-" + kind + "-missing", app)
            XCTFail("""
                No \(title) selection surface was reachable on the paused player, so \
                the carried-\(kind) claim for "\(choice)" cannot be asserted. Report \
                the attached hierarchy; do not skip this.
                """)
            throw QualificationStop()
        }
        try select(opener, in: app)
        try await Task.sleep(for: .milliseconds(700))
        let item: XCUIElement
        if choice == "Off" {
            item = app.buttons.matching(
                NSPredicate(format: "identifier == %@ OR label == %@", "direct.track.off", "Off")
            ).firstMatch
        } else {
            item = app.buttons.matching(
                NSPredicate(format: "label CONTAINS[c] %@", choice)).firstMatch
        }
        // The system Audio Options pop-up can place renditions behind Audio Track.
        if kind == "audio", !item.waitForExistence(timeout: 2) {
            let audioTrack = app.buttons.matching(
                NSPredicate(format: "label ==[c] %@", "Audio Track")
            ).firstMatch
            if audioTrack.exists, audioTrack.isEnabled { try select(audioTrack, in: app) }
        }
        XCTAssertTrue(item.waitForExistence(timeout: 8),
                      "Track choice \"\(choice)\" must exist in the \(title) selector")
        try select(item, in: app)
        try await Task.sleep(for: .milliseconds(700))
        let done = app.buttons["direct.track.done"]
        if done.exists { try select(done, in: app) }
        try await Task.sleep(for: .milliseconds(500))
        capture("track-" + kind + "-" + choice, app)
    }

    // MARK: - Playback waiting

    /// Wait for a fresh authenticated pass that checkpoints at or past `minimum`.
    /// A focused card is not playback proof; a new pass with a checkpoint is.
    @MainActor
    private func assertPlaybackStarted(from before: [String: Any], minimum: Double = 8,
                                      seconds: Int? = nil) async throws -> [String: Any] {
        let deadline = seconds ?? playbackDeadline
        var proof = before
        for _ in 0..<deadline {
            proof = try await fixture("/qa/state")
            if freshPasses(proof) > freshPasses(before),
               let position = checkpoint(proof), position >= minimum {
                break
            }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertGreaterThan(freshPasses(proof), freshPasses(before),
                             "A new authenticated play must occur")
        XCTAssertGreaterThanOrEqual(checkpoint(proof) ?? -1, minimum,
                                    "Playback must checkpoint at or past the saved position")
        return proof
    }

    @MainActor
    private func returnFromPlayer(_ app: XCUIApplication, name: String,
                                 previousReleases: Int,
                                 focusTarget: XCUIElement,
                                 rightCategory: XCUIElement? = nil) async throws {
        let destination = continueCard(in: app)
        // AVKit may consume the first Menu to hide visible transport controls.
        // Never send a blind third Menu: it could leave the application.
        for attempt in 1...2 {
            if destination.exists && destination.isEnabled { break }
            XCUIRemote.shared.press(.menu)
            for _ in 0..<8 {
                if destination.exists && destination.isEnabled { break }
                try await Task.sleep(for: .milliseconds(250))
            }
            capture(name + "-menu-" + String(attempt), app)
        }
        guard destination.exists, destination.isEnabled else {
            XCTFail("Library did not return after at most two Menu presses")
            throw QualificationStop()
        }
        if let rightCategory {
            try await assertCategoryReturn(focusTarget, right: rightCategory,
                                           name: name + "-library-focus", in: app)
        } else {
            try await assertFocus(focusTarget, name: name + "-library-focus", in: app)
        }
        var proof: [String: Any] = [:]
        for _ in 0..<20 {
            proof = try await fixture("/qa/state")
            let releaseTotal = (proof["qa_calls"] as? [[String: Any]] ?? []).filter {
                $0["action"] as? String == "release"
            }.count
            if releaseTotal > previousReleases { break }
            try await Task.sleep(for: .milliseconds(250))
        }
        let releaseTotal = (proof["qa_calls"] as? [[String: Any]] ?? []).filter {
            $0["action"] as? String == "release"
        }.count
        XCTAssertGreaterThan(releaseTotal, previousReleases,
                             "Return must release the newly opened media pass")
    }

    // MARK: - External deep-link consent

    @MainActor
    private func acceptExternalOpenIfPresent(_ app: XCUIApplication) throws {
        // An external open goes through PineBoard. This system consent is not the
        // destination screen and must be accepted with the real remote.
        let home = XCUIApplication(bundleIdentifier: "com.apple.PineBoard")
        let homeOpen = home.buttons.matching(
            NSPredicate(format: "label == %@", "Open")).firstMatch
        let appOpen = app.buttons.matching(
            NSPredicate(format: "label == %@", "Open")).firstMatch
        if homeOpen.waitForExistence(timeout: 4) {
            capture("external-link-consent", home)
            try select(homeOpen, in: home)
        } else if appOpen.waitForExistence(timeout: 1) {
            capture("external-link-consent", app)
            try select(appOpen, in: app)
        }
        // A remembered consent may skip the dialog entirely. The routed
        // destination is always required below, never a successful open call alone.
    }

    // MARK: - Home and Requests

    @MainActor
    func test10HomeAndRequestsNavigation() async throws {
        let app = try launchConnected()
        let card = app.buttons.matching(
            NSPredicate(format: "identifier == %@ OR label CONTAINS %@",
                        "home-card-continue-movies-qa-movie", "Synthetic HLS movie")
        ).firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 12), "Home must render the authenticated projection")
        let requests = app.buttons.matching(
            NSPredicate(format: "identifier == %@ OR label == %@", "library.requests", "Requests")
        ).firstMatch
        try select(requests, in: app)
        let pending = app.buttons.matching(
            NSPredicate(format: "identifier == %@ OR label CONTAINS %@",
                        "requests-row-movie:tt9000011", "QA Pending Movie")
        ).firstMatch
        try select(pending, in: app)
        XCTAssertTrue(app.staticTexts["Request saved"].waitForExistence(timeout: 8))
        capture("10-request-timeline", app)
        XCUIRemote.shared.press(.menu)
        try await assertFocus(pending, name: "10-returned-request-focus", in: app)
        XCUIRemote.shared.press(.menu)
        try await assertFocus(requests, name: "10-returned-navigation-focus", in: app)
    }

    @MainActor
    func test11AccountToolsRemoteNavigation() async throws {
        let app = try launchConnected()
        let account = app.buttons["library.account-tools"]
        try select(account, in: app)
        let toolsTitle = app.staticTexts["Account & tools"]
        XCTAssertTrue(toolsTitle.waitForExistence(timeout: 8))
        XCTAssertTrue(detailsAction("account-tools.profiles", label: "Profiles", in: app).exists)
        XCTAssertTrue(detailsAction("account-tools.refresh", label: "Refresh library", in: app).exists)
        capture("11-account-tools", app)

        try select(detailsAction("account-tools.settings", label: "Settings", in: app), in: app)
        XCTAssertTrue(app.staticTexts["This Apple TV"].waitForExistence(timeout: 8))
        XCUIRemote.shared.press(.menu)
        XCTAssertTrue(toolsTitle.waitForExistence(timeout: 8))

        XCUIRemote.shared.press(.menu)
        try await assertFocus(account, name: "11-account-return-focus", in: app)
    }

    // MARK: - 01 Catalog request and check

    @MainActor
    func test01CatalogRequestAndCheck() async throws {
        let app = try launchConnected()
        _ = try await fixture("/qa/state")   // fail early when the fixture is down
        try select(app.buttons["For You"], in: app)
        try select(detailsAction("movies", label: "Movies", in: app), in: app)

        let movie = app.buttons.matching(
            NSPredicate(format: "label CONTAINS %@", "QA Requested Movie")).firstMatch
        try select(movie, in: app)
        try await assertFocus(detailsAction("title-back", label: "Back", in: app),
                              name: "01-detail-back-focus", in: app)

        try select(detailsAction("request", label: "Request", in: app), in: app)
        try select(detailsAction("check", label: "Check status", in: app), in: app)
        let proof = try await fixture("/qa/state")
        XCTAssertEqual(calls(proof, "request").count, 1,
                       "Exactly one authenticated request must be posted")
        XCTAssertEqual(calls(proof, "check").count, 1,
                       "Exactly one authenticated status check must be posted")
        XCTAssertEqual(requests(proof).count, 1, "One title must be requested")
        XCTAssertEqual(requests(proof).first?["state"] as? String, "ready",
                       "The requested title must reach the ready state")
        capture("03-request-status", app)

        XCUIRemote.shared.press(.menu)
        XCTAssertTrue(movie.waitForExistence(timeout: 5))
        try await assertFocus(movie, name: "03-returned-card-focus", in: app)
    }

    // MARK: - 02 Catalog search

    @MainActor
    func test02CatalogSearchAndFocusReturn() async throws {
        let app = try launchConnected()
        try await fixture("/qa/state")
        XCTAssertTrue(app.buttons["library.search"].waitForExistence(timeout: 10))
        try select(app.buttons["library.search"], in: app)
        try select(app.buttons["library.search.field"], in: app)
        app.typeText("QA\n")
        XCTAssertTrue(app.staticTexts["Search results"].waitForExistence(timeout: 12),
                      "A non-empty query must switch the header to Search results")
        capture("04-catalog-search", app)

        XCUIRemote.shared.press(.menu)
        try await assertFocus(app.buttons["library.search"],
                              name: "04-search-focus", in: app)

        let proof = try await fixture("/qa/state")
        XCTAssertTrue(calls(proof, "catalog-search").contains {
            $0["query"] as? String == "QA"
        }, "The typed query must be posted as a catalog search")
    }

    // MARK: - 03 Continue Watching

    @MainActor
    func test03ContinueWatchingPlaybackAndFocus() async throws {
        let app = try launchConnected()
        try await fixture("/qa/state")
        try select(app.buttons["Movies"], in: app)
        let before = try await fixture("/qa/state")
        let releaseCountBefore = calls(before, "release").count
        let card = continueCard(in: app)
        XCTAssertTrue(card.waitForExistence(timeout: 12),
                      "A Continue Watching card (identifier continue-*) must exist")
        try select(card, in: app)

        _ = try await assertPlaybackStarted(from: before)
        capture("05-continue-playing", app)
        try await returnFromPlayer(app, name: "05-continue-return",
                                   previousReleases: releaseCountBefore,
                                   focusTarget: continueCard(in: app))
    }

    // MARK: - 04 Top Shelf details

    @MainActor
    func test04TopShelfDetails() async throws {
        let app = try launchConnected()
        let shelf = try await fixture("/qa/shelf")
        guard shelf["available"] as? Bool == true else {
            let reason = shelf["reason"] as? String ?? "unknown"
            throw XCTSkip("""
                The simulator did not expose the Top Shelf app-group snapshot \
                (`available: false`, reason `\(reason)`). The save/deep-link path \
                stays unproven for this run; physical Home Screen rendering is a \
                separate acceptance step. Set OMAKASE_SIMULATOR_UDID and launch the \
                fixture with start_connected_fixture.sh to enable it.
                """)
        }
        XCTAssertEqual(Set(shelf["actions"] as? [String] ?? []),
                       Set(["resume", "details"]),
                       "Both Top Shelf actions must be published")
        let opened = try await fixture("/qa/open-shelf?action=details")
        XCTAssertEqual(opened["opened"] as? String, "details",
                       "The details deep link must be the one delivered")
        try acceptExternalOpenIfPresent(app)

        XCTAssertTrue(app.staticTexts["Synthetic saved series details."]
            .waitForExistence(timeout: 12),
                      "Top Shelf details must route to the saved series details")
        // The destination is the saved-series details overlay. The For You
        // recommendation details control must NOT be required here.
        XCTAssertFalse(app.buttons["for-you.title-back"].exists,
                       """
                       for-you.title-back belongs to the For You recommendation \
                       details, not to the Top Shelf saved-series destination
                       """)
        let close = savedSeriesOverlayClose(in: app)
        XCTAssertTrue(close.waitForExistence(timeout: 8),
                      "The saved-series details overlay must expose its Close control")
        let playNow = app.buttons.matching(
            NSPredicate(format: "label == %@", "Play now")).firstMatch
        if playNow.exists {
            try await assertFocus(playNow, name: "06-details-default-focus", in: app)
        }
        capture("06-top-shelf-details", app)

        // Menu is the overlay's own exit command; it must return to the library.
        XCUIRemote.shared.press(.menu)
        XCTAssertTrue(app.buttons["For You"].waitForExistence(timeout: 8),
                      "The details overlay must dismiss back to the library")
    }

    // MARK: - 05 Top Shelf resume

    @MainActor
    func test05TopShelfResume() async throws {
        let app = try launchConnected()
        let shelf = try await fixture("/qa/shelf")
        guard shelf["available"] as? Bool == true else {
            let reason = shelf["reason"] as? String ?? "unknown"
            throw XCTSkip("""
                The simulator did not expose the Top Shelf app-group snapshot \
                (`available: false`, reason `\(reason)`), so the resume deep link \
                cannot be driven. Physical Home Screen rendering stays a separate \
                acceptance step.
                """)
        }
        XCUIRemote.shared.press(.menu)
        let before = try await fixture("/qa/state")
        let opened = try await fixture("/qa/open-shelf?action=resume")
        XCTAssertEqual(opened["opened"] as? String, "resume",
                       "The resume deep link must be the one delivered")
        // An external open asks for consent first. The audited fixture skipped
        // this prompt and then waited for playback that could never start.
        try acceptExternalOpenIfPresent(app)

        _ = try await assertPlaybackStarted(from: before)
        capture("07-top-shelf-resume", app)
        try await returnFromPlayer(app, name: "07-top-shelf-return",
                                   previousReleases: calls(before, "release").count,
                                   focusTarget: app.buttons["Movies"],
                                   rightCategory: app.buttons["TV Shows"])
    }

    // MARK: - 06 Paused viewer: expiry waits for Play

    @MainActor
    func test06LongPausePlanExpiryWaitsForPlay() async throws {
        let app = try launchConnected()
        let outcome = try await drivePausedPlanExpiry(app, hold: longPauseSeconds)
        let reopened = outcome.reopened

        XCTAssertEqual(freshPasses(reopened) - freshPasses(outcome.playing), 1,
                       "Play must reopen exactly one fresh pass")
        guard let newMedia = outcome.newMedia else {
            XCTFail("The fixture must report the pass reopened by Play")
            throw QualificationStop()
        }
        XCTAssertEqual(pass(reopened, media: newMedia)?["plan"] as? String, "live",
                       "The reopened pass must serve a live plan")
        XCTAssertTrue(renditions(reopened, media: newMedia).contains("index.m3u8"),
                      "The reopened pass must actually fetch its master playlist")

        // Position: the reopened pass was handed the last checkpoint.
        guard let savedPosition = resumePosition(reopened) else {
            XCTFail("The fixture must report the position handed to the fresh pass")
            throw QualificationStop()
        }
        XCTAssertEqual(savedPosition, outcome.pausedAt, accuracy: 2.0,
                       "The fresh pass must reopen at the saved checkpoint, not at zero")

        // The pass it replaced is released, and nothing else opens.
        XCTAssertGreaterThanOrEqual(releases(reopened, media: outcome.oldMedia), 1,
                                    "The replaced pass must be released")
        XCTAssertEqual(pass(reopened, media: outcome.oldMedia)?["registered"] as? Bool, false,
                       "The replaced pass must no longer be registered")
        let quiet = try await stretchQuiet(seconds: quietWindow)
        XCTAssertEqual(freshPasses(quiet), freshPasses(reopened),
                       "No second pass may open after the first recovery")
        assertPlayerSurface(app, name: "09-recovered")
        capture("09-long-pause-resumed", app)
    }

    // MARK: - 07 Carry an explicit subtitle Off across the reopen

    @MainActor
    func test07CarriedSubtitleOffSurvivesReopen() async throws {
        let app = try launchConnected()
        let outcome = try await drivePausedPlanExpiry(app, hold: 0) { paused in
            try await self.chooseTrack(kind: "subtitle", choice: "Off", in: paused)
        }
        guard let newMedia = outcome.newMedia else {
            XCTFail("The fixture must report the pass reopened by Play")
            throw QualificationStop()
        }
        XCTAssertEqual(freshPasses(outcome.reopened) - freshPasses(outcome.playing), 1,
                       "Play must reopen exactly one fresh pass")
        let reopenedRenditions = renditions(outcome.reopened, media: newMedia)
        XCTAssertFalse(reopenedRenditions.contains { $0.hasPrefix("captions-") },
                       """
                       An explicit subtitle Off must be carried without fetching any \
                       captions rendition. Reopened pass renditions: \
                       \(reopenedRenditions)
                       """)
    }

    // MARK: - 08 Carry a chosen subtitle and audio across the reopen

    @MainActor
    func test08CarriedTracksSurviveReopen() async throws {
        let app = try launchConnected()
        let outcome = try await drivePausedPlanExpiry(app, hold: 0) { paused in
            try await self.chooseTrack(kind: "subtitle", choice: "French", in: paused)
            try await self.chooseTrack(kind: "audio", choice: "Commentary", in: paused)
        }
        guard let newMedia = outcome.newMedia else {
            XCTFail("The fixture must report the pass reopened by Play")
            throw QualificationStop()
        }
        XCTAssertEqual(freshPasses(outcome.reopened) - freshPasses(outcome.playing), 1,
                       "Play must reopen exactly one fresh pass")
        let reopenedRenditions = renditions(outcome.reopened, media: newMedia)
        XCTAssertTrue(reopenedRenditions.contains("captions-1.m3u8"),
                      """
                      The French subtitle choice must be carried: the reopen must fetch \
                      captions-1.m3u8. Reopened pass renditions: \(reopenedRenditions)
                      """)
        XCTAssertFalse(reopenedRenditions.contains("captions-0.m3u8"),
                       "The reopen must not fetch the rendition the viewer did not choose")
        XCTAssertTrue(reopenedRenditions.contains("media-a1.m3u8"),
                      """
                      The Commentary audio choice must be carried: the reopen must fetch \
                      media-a1.m3u8. Reopened pass renditions: \(reopenedRenditions)
                      """)
    }

    // MARK: - 09 Stale close must not end the playing session

    @MainActor
    func test09StaleCloseDoesNotEndPlayingSession() async throws {
        let app = try launchConnected()
        let outcome = try await drivePausedPlanExpiry(app, hold: 0)
        guard let successor = outcome.newMedia else {
            XCTFail("The fixture must report the successor pass")
            throw QualificationStop()
        }
        let beforeClose = outcome.reopened
        let advanced = checkpoint(beforeClose) ?? -1
        XCTAssertGreaterThan(advanced, 0, "The successor must have checkpointed before a close")

        // The server still holds the older copy; this close names it while the
        // successor keeps playing. POST: a close is not a read.
        let closed = try await fixture("/qa/stale-close", method: "POST", body: [
            "media_id": outcome.oldMedia,
        ])
        XCTAssertEqual(closed["stale_close"] as? String, outcome.oldMedia,
                       "The stale close must name the older pass")
        XCTAssertEqual(closed["older_registered"] as? Bool, false,
                       "The stale close must deregister the older pass")
        XCTAssertEqual(closed["successor"] as? String, successor,
                       "The successor must be the live pass")
        XCTAssertEqual(closed["successor_live"] as? Bool, true,
                       "The stale close must leave the successor live")
        XCTAssertEqual(closed["gate_owner"] as? String, successor,
                       "The stale close must not move the playback gate")

        // And the app must keep playing: the checkpoint advances, the player is
        // still presented, and nothing restarts.
        let later = try await stretchQuiet(seconds: quietWindow)
        XCTAssertEqual(freshPasses(later), freshPasses(beforeClose),
                       "A stale close must not open a pass")
        XCTAssertEqual(gateOwner(later), successor,
                       "The gate must still belong to the successor after a stale close")
        XCTAssertEqual(pass(later, media: successor)?["plan"] as? String, "live",
                       "The successor's plan must be untouched")
        XCTAssertGreaterThan(checkpoint(later) ?? -1, advanced,
                            "The active session must keep checking in after a stale close")
        assertPlayerSurface(app, name: "10-stale-close")
        capture("10-stale-close", app)
    }

    // MARK: - Recovery driver

    private struct RecoveryOutcome {
        let before: [String: Any]
        let playing: [String: Any]
        let paused: [String: Any]
        let reopened: [String: Any]
        let oldMedia: String
        let newMedia: String?
        let pausedAt: Double
    }

    /// Play, pause with the real remote, reclaim the server plan, then press Play.
    ///
    /// Everything that must be true *before* Play is asserted here: the player is
    /// still on screen and paused, the grant stays authorized, the plan is
    /// reclaimed, and the fresh-pass counter does not move while the viewer is
    /// idle. `hold` is the idle window; `tracks` selects renditions while paused.
    @MainActor
    private func drivePausedPlanExpiry(
        _ app: XCUIApplication, hold: Int,
        tracks: (@MainActor (XCUIApplication) async throws -> Void)? = nil
    ) async throws -> RecoveryOutcome {
        try select(app.buttons["Movies"], in: app)
        let before = try await fixture("/qa/state")
        let card = continueCard(in: app)
        XCTAssertTrue(card.waitForExistence(timeout: 12),
                      "A Continue Watching card must exist")
        try select(card, in: app)
        let minimum = max(8, (resumePosition(before) ?? 0) + 1.5)
        let playing = try await assertPlaybackStarted(from: before, minimum: minimum)
        guard let oldMedia = gateOwner(playing) ?? passes(playing).last?["media_id"] as? String
        else {
            XCTFail("The fixture must report the pass the app just opened")
            throw QualificationStop()
        }

        XCUIRemote.shared.press(.playPause)
        _ = try await waitForPausedCheckpoint(after: playing, mediaID: oldMedia, in: app)
        if let tracks { try await tracks(app) }
        let pausedState = try await fixture("/qa/state")
        guard let pausedAt = checkpoint(pausedState) else {
            XCTFail("A paused player must have checkpointed a position")
            throw QualificationStop()
        }

        let expired = try await fixture("/qa/expire-plan", method: "POST")
        XCTAssertEqual(expired["expired"] as? String, oldMedia,
                       "The control must reclaim the plan of the playing pass")
        XCTAssertEqual(expired["stream"] as? String, "expired")
        XCTAssertEqual(expired["recover"] as? String, "reopen")

        var idle = try await fixture("/qa/state")
        func clientObservedExpiry(_ state: [String: Any]) -> Bool {
            calls(state, "expired-access").contains { $0["media_id"] as? String == oldMedia }
        }
        for _ in 0..<24 where !clientObservedExpiry(idle) {
            try await Task.sleep(for: .milliseconds(500))
            idle = try await fixture("/qa/state")
        }
        XCTAssertTrue(clientObservedExpiry(idle), "The client must read the explicit expiry hint")
        XCTAssertEqual(pass(idle, media: oldMedia)?["plan"] as? String, "expired",
                       "The media plan must be reclaimed")
        XCTAssertEqual(pass(idle, media: oldMedia)?["registered"] as? Bool, true,
                       "The grant must stay authorized after plan expiry")
        XCTAssertEqual(freshPasses(idle), freshPasses(playing),
                       "An idle paused viewer must not open a fresh pass")

        if hold > 0 {
            let held = try await stretchQuiet(seconds: hold)
            XCTAssertEqual(freshPasses(held), freshPasses(playing),
                           """
                           No pass may open while the viewer stays paused, even after \
                           the plan was reclaimed
                           """)
            XCTAssertEqual(pass(held, media: oldMedia)?["registered"] as? Bool, true,
                           "Plan expiry must not revoke the grant")
        }

        // Play is intercepted and reopens exactly one fresh pass.
        XCUIRemote.shared.press(.playPause)
        var reopened = idle
        var newMedia: String?
        for _ in 0..<40 {
            reopened = try await fixture("/qa/state")
            if freshPasses(reopened) > freshPasses(playing),
               let successor = gateOwner(reopened), successor != oldMedia,
               reported("playing", for: successor, in: reopened) {
                newMedia = successor
                break
            }
            try await Task.sleep(for: .milliseconds(500))
        }
        XCTAssertNotNil(newMedia, "The reopened pass must report real AVPlayer playback")
        return RecoveryOutcome(before: before, playing: playing, paused: pausedState,
                               reopened: reopened, oldMedia: oldMedia,
                               newMedia: newMedia, pausedAt: pausedAt)
    }

    /// Wait `seconds` while asserting nothing else opens a pass.
    private func stretchQuiet(seconds: Int) async throws -> [String: Any] {
        try await Task.sleep(for: .seconds(seconds))
        return try await fixture("/qa/state")
    }
}
