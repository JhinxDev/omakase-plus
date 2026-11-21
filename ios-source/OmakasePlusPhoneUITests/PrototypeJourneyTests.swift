import XCTest

final class PrototypeJourneyTests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor
    func testSyntheticBrowsePlaybackSeekAndScopedSessionResume() throws {
        let app = XCUIApplication()
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
        app.launch()
        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 10))
        search.tap()
        search.typeText("Violet")
        XCTAssertTrue(app.buttons["title-movie"].exists)
        XCTAssertFalse(app.buttons["title-anime"].exists)
    }
}
