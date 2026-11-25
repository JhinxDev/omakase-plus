import XCTest

final class PrototypeJourneyTests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor
    func testSyntheticBrowsePlaybackSeekAndScopedSessionResume() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OmakaseSyntheticPrototype"]
        app.launch()
        XCTAssertTrue(app.staticTexts["prototypeNotice"].waitForExistence(timeout: 10))
        app.buttons["title-movie"].tap()
        app.buttons["playClip"].tap()
        let state = app.staticTexts["playbackState"]
        let advancing = NSPredicate(format: "label == %@", "Playback advancing")
        expectation(for: advancing, evaluatedWith: state)
        waitForExpectations(timeout: 15)
        app.buttons["seekForward"].tap()
        let position = app.staticTexts["playbackPosition"]
        let seeked = NSPredicate { _, _ in
            let value = Int(position.label.components(separatedBy: " ").dropFirst().first ?? "") ?? 0
            return value >= 5
        }
        expectation(for: seeked, evaluatedWith: position)
        waitForExpectations(timeout: 10)
        app.buttons["stopPlayback"].tap()
        XCTAssertTrue(app.buttons["playClip"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons["playClip"].label, "Resume synthetic clip")
        app.buttons["playClip"].tap()
        expectation(for: seeked, evaluatedWith: position)
        waitForExpectations(timeout: 10)
        app.buttons["stopPlayback"].tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["title-anime"].tap()
        XCTAssertEqual(app.staticTexts["resumePosition"].label, "Session position: 0 seconds")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        app.buttons["title-movie"].tap()
        XCTAssertEqual(app.buttons["playClip"].label, "Resume synthetic clip")
    }

    @MainActor
    func testSyntheticSearch() throws {
        let app = XCUIApplication()
        app.launchArguments = ["-OmakaseSyntheticPrototype"]
        app.launch()
        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 10))
        search.tap()
        search.typeText("Violet")
        XCTAssertTrue(app.buttons["title-movie"].exists)
        XCTAssertFalse(app.buttons["title-anime"].exists)
    }

    @MainActor
    func testRealOnboardingLaunchAndInviteToggle() throws {
        let app = XCUIApplication()
        app.launch()
        let serverField = app.textFields["phone.onboarding.server"]
        XCTAssertTrue(serverField.waitForExistence(timeout: 10))
        let pairButton = app.buttons["phone.onboarding.pair"]
        XCTAssertTrue(pairButton.exists)
        let inviteToggle = app.buttons["phone.onboarding.invite"]
        XCTAssertTrue(inviteToggle.exists)
        inviteToggle.tap()
        XCTAssertTrue(app.textFields["phone.onboarding.inviteField"].waitForExistence(timeout: 5))
    }
}

final class PhoneShowcaseCaptureTests: XCTestCase {
    @MainActor
    func testCaptureNativeiOSViews() async throws {
        let origin = ProcessInfo.processInfo.environment["OMAKASE_SHOWCASE_ORIGIN"]
        try XCTSkipUnless(origin != nil && !(origin?.isEmpty ?? true), "Showcase capture only")
        continueAfterFailure = false

        let app = XCUIApplication()
        app.launch()

        let serverField = app.textFields["phone.onboarding.server"]
        if serverField.waitForExistence(timeout: 10) {
            serverField.tap()
            let existing = (serverField.value as? String) ?? ""
            let target = origin ?? "https://127.0.0.1:8854"
            if existing == "https://" && target.hasPrefix("https://") {
                serverField.typeText(String(target.dropFirst("https://".count)))
            } else {
                serverField.tap(withNumberOfTaps: 3, numberOfTouches: 1)
                serverField.typeText(target)
            }
            app.buttons["phone.onboarding.pair"].tap()
        }

        let homeTab = app.tabBars.buttons["Home"]
        XCTAssertTrue(homeTab.waitForExistence(timeout: 20), "iPhone Home tab must load after pairing")
        try await Task.sleep(for: .seconds(4))
        attach("01-ios-home", app)

        app.swipeUp(velocity: .slow)
        try await Task.sleep(for: .seconds(1.5))
        attach("02-ios-home-shelves", app)

        app.tabBars.buttons["Library"].tap()
        try await Task.sleep(for: .seconds(2.8))
        attach("03-ios-library", app)

        app.tabBars.buttons["Requests"].tap()
        try await Task.sleep(for: .seconds(2.5))
        attach("04-ios-requests", app)
    }

    @MainActor
    private func attach(_ name: String, _ app: XCUIApplication) {
        let att = XCTAttachment(screenshot: app.screenshot())
        att.name = name
        att.lifetime = .keepAlways
        add(att)
    }
}
