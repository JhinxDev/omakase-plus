import XCTest

final class RemoteFocusTests: XCTestCase {
    @MainActor
    func testBackRestoresFocusToTheSelectedShelfCard() {
        let app = XCUIApplication()
        app.launchArguments = ["--qualification-fixtures"]
        app.launch()
        let remote = XCUIRemote.shared
        let anime = app.buttons["fixture.card.anime"]
        let movie = app.buttons["fixture.card.movie"]

        assertFocused(anime)
        remote.press(.right)
        assertFocused(movie)
        remote.press(.select)
        assertFocused(app.buttons["fixture.play"])
        remote.press(.menu)
        assertFocused(movie)
    }

    @MainActor
    private func assertFocused(_ element: XCUIElement, file: StaticString = #filePath, line: UInt = #line) {
        let focused = XCTNSPredicateExpectation(predicate: NSPredicate(format: "hasFocus == true"), object: element)
        XCTAssertEqual(XCTWaiter.wait(for: [focused], timeout: 6), .completed, file: file, line: line)
    }
}

final class DirectControlsLayoutTests: XCTestCase {
    @MainActor
    func testTransportAndSubtitleMenuStayReadableAndFocusable() {
        let app = XCUIApplication()
        app.launchArguments = ["--direct-controls-fixture"]
        app.launch()

        let play = control("play", in: app)
        XCTAssertTrue(play.waitForExistence(timeout: 20), "Direct fixture must open")
        capture("01-transport", app)
        let subtitles = control("subtitles", in: app)
        let selector = element("direct.track.selector", in: app)
        let off = app.buttons["direct.track.off"]
        XCTAssertFalse(selector.exists, "Track selector must stay hidden until opened")
        XCTAssertFalse(off.exists, "Subtitle options must stay hidden until opened")
        XCTAssertEqual(app.buttons.matching(
            NSPredicate(format: "identifier BEGINSWITH %@ OR identifier BEGINSWITH %@ OR label CONTAINS %@ OR label CONTAINS %@",
                        "direct.track.audio.", "direct.track.subtitle.", "Fixture 01", "Alternate Stereo")
        ).count, 0, "Track options must stay out of the transport")

        let viewport = app.windows.firstMatch.frame
        XCTAssertGreaterThan(viewport.width, 1000)
        let safeFrame = viewport.insetBy(dx: 48, dy: 28)
        var frames: [CGRect] = []
        for id in ["back", "play", "forward", "audio", "subtitles", "next", "hide"] {
            let control = control(id, in: app)
            XCTAssertTrue(control.exists, "Missing transport control: \(id)")
            guard control.exists else { continue }
            let frame = control.frame
            XCTAssertGreaterThanOrEqual(frame.width, 56, "Undersized focus target: \(id)")
            XCTAssertTrue(safeFrame.contains(frame), "Unsafe \(id): \(frame), viewport \(viewport)")
            for previous in frames {
                XCTAssertFalse(previous.intersects(frame), "Overlapping \(id): \(previous) and \(frame)")
            }
            frames.append(frame)
        }

        moveFocus(to: subtitles)
        guard focused(subtitles) else {
            XCTFail("Subtitles cannot be focused using the Siri Remote")
            return
        }
        XCUIRemote.shared.press(.select)
        XCTAssertTrue(off.waitForExistence(timeout: 8), "Subtitles must expose Off")
        capture("02-subtitles", app)
        XCTAssertFalse(play.exists, "Transport must leave the focus tree when the track selector opens")
        let tracks = app.buttons.matching(
            NSPredicate(format: "identifier BEGINSWITH %@", "direct.track.subtitle."))
        XCTAssertGreaterThanOrEqual(tracks.count, 12, "Synthetic subtitle selection lost tracks")
        let last = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Fixture 12")).firstMatch
        for _ in 0..<20 where !focused(last) { XCUIRemote.shared.press(.down) }
        XCTAssertTrue(focused(last), "Last subtitle must be reachable while scrolling")
        if focused(last) {
            XCTAssertTrue(viewport.contains(last.frame), "Last subtitle must scroll into the viewport")
        }
        capture("03-scrolled", app)
        for _ in 0..<20 where !focused(off) { XCUIRemote.shared.press(.up) }
        XCTAssertTrue(focused(off), "Off must be reachable by remote")
        if focused(off) { XCUIRemote.shared.press(.select) }
        assertFocusReturns(to: subtitles)

        moveFocus(to: subtitles)
        if focused(subtitles) { XCUIRemote.shared.press(.select) }
        XCTAssertTrue(off.waitForExistence(timeout: 8), "Subtitles must reopen after Off")
        XCTAssertTrue(off.isSelected || (off.value as? String) == "Selected",
                      "Off must remain selected when the panel reopens")
        XCUIRemote.shared.press(.menu)
        assertFocusReturns(to: subtitles)

        let audio = control("audio", in: app)
        moveFocus(to: audio)
        XCTAssertTrue(focused(audio), "Audio picker must be reachable by remote")
        if focused(audio) { XCUIRemote.shared.press(.select) }
        let audioTracks = app.buttons.matching(
            NSPredicate(format: "identifier BEGINSWITH %@", "direct.track.audio."))
        XCTAssertTrue(audioTracks.firstMatch.waitForExistence(timeout: 8))
        XCTAssertEqual(audioTracks.count, 2)
        let alternate = audioTracks.element(boundBy: 1)
        moveFocus(to: alternate)
        XCTAssertTrue(focused(alternate), "Alternate audio must be reachable")
        if focused(alternate) { XCUIRemote.shared.press(.select) }
        assertFocusReturns(to: audio)
        if focused(audio) { XCUIRemote.shared.press(.select) }
        XCTAssertTrue(alternate.waitForExistence(timeout: 8))
        XCTAssertTrue(alternate.isSelected, "Alternate audio selection must persist")
        XCUIRemote.shared.press(.menu)
        assertFocusReturns(to: audio)

        let hide = control("hide", in: app)
        moveFocus(to: hide)
        XCTAssertTrue(focused(hide), "Hide must be reachable")
        if focused(hide) { XCUIRemote.shared.press(.select) }
        let show = control("show", in: app)
        assertFocusReturns(to: show)
        XCTAssertFalse(play.exists, "Transport must be absent when hidden")
        if focused(show) { XCUIRemote.shared.press(.select) }
        assertFocusReturns(to: play)
    }

    @MainActor
    private func control(_ id: String, in app: XCUIApplication) -> XCUIElement {
        app.buttons["direct.control." + id]
    }

    @MainActor
    private func element(_ identifier: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "identifier == %@", identifier)).firstMatch
    }

    @MainActor
    private func assertFocusReturns(to control: XCUIElement) {
        let selected = XCTNSPredicateExpectation(predicate: NSPredicate(format: "hasFocus == true"),
                                                  object: control)
        XCTAssertEqual(XCTWaiter.wait(for: [selected], timeout: 8), .completed,
                       "Closing subtitle selection must restore focus to Subtitles")
    }

    @MainActor
    private func moveFocus(to control: XCUIElement) {
        let remote = XCUIRemote.shared
        let directions: [XCUIRemote.Button] = [.right, .down, .right, .up, .left]
        for direction in directions {
            for _ in 0..<7 where !focused(control) { remote.press(direction) }
            if focused(control) { break }
        }
    }

    @MainActor
    private func focused(_ control: XCUIElement) -> Bool {
        control.exists && NSPredicate(format: "hasFocus == true").evaluate(with: control)
    }

    @MainActor
    private func capture(_ name: String, _ app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
