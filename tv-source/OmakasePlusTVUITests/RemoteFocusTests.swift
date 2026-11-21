import XCTest
import UIKit

// Runs the actual authenticated Home and navigation, using the existing loopback
// companion fixture. No duplicate button renderer or app-only test view.
final class RemoteFocusRenderingTests: XCTestCase {
    private struct Stop: Error {}

    @MainActor
    private func launchHome() throws -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["-omakase.tv.server-origin", "https://127.0.0.1:8854"]
        app.launch()
        if app.buttons["live.enter"].waitForExistence(timeout: 4) {
            try reach(app.buttons["live.enter"], in: app)
            XCUIRemote.shared.press(.select)
            try reach(app.buttons["live.showCode"], in: app)
            XCUIRemote.shared.press(.select)
        }
        let home = app.buttons.matching(NSPredicate(
            format: "identifier == %@ OR label == %@", "nav-home", "Home")).firstMatch
        try reach(home, in: app)
        return app
    }

    @MainActor
    func testHomeNavigationHasNoWhiteBackplate() throws {
        let app = try launchHome()
        let home = app.buttons.matching(NSPredicate(
            format: "identifier == %@ OR label == %@", "nav-home", "Home")).firstMatch
        try checkBackplate(home, in: app, name: "01-home-tab", card: false)
    }

    @MainActor
    func testContinueWatchingSelectReturnHasNoWhiteBackplate() async throws {
        let app = try launchHome()
        XCUIRemote.shared.press(.select)

        let card = app.buttons["home-card-continue-movies-qa-movie"]
        try reach(card, in: app)
        try checkBackplate(card, in: app, name: "02-continue-watching", card: true)
        let before = try await fixturePasses()
        XCUIRemote.shared.press(.select)
        // Read the same synthetic companion used by ConnectedQualification, so
        // an inert Select gesture or a loading/error surface cannot count as play.
        var started = false
        for _ in 0..<20 {
            if try await fixturePasses() > before { started = true; break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertTrue(started, "Select must open one authenticated playback pass")
        // AVKit may consume one Menu press to dismiss its transport controls.
        for _ in 0..<2 {
            if card.exists && card.hasFocus { break }
            XCUIRemote.shared.press(.menu)
            try await Task.sleep(for: .seconds(1))
        }
        let returned = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND hasFocus == true"), object: card)
        XCTAssertEqual(XCTWaiter.wait(for: [returned], timeout: 10), .completed,
                       "Menu must restore the exact selected Home card")
        try checkBackplate(card, in: app, name: "03-returned-card", card: true)
    }

    private func fixturePasses() async throws -> Int {
        let url = try XCTUnwrap(URL(string: "https://127.0.0.1:8854/qa/state"))
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        let (data, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        let state = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(state["fresh_passes_opened"] as? Int)
    }

    @MainActor
    func testLibraryLongSelectOpensDetailsWithoutAlsoPlaying() async throws {
        let app = try launchHome()
        let movies = app.buttons.matching(NSPredicate(
            format: "identifier == %@ OR label == %@", "nav-movie", "Movies")).firstMatch
        try reach(movies, in: app)
        XCUIRemote.shared.press(.select)
        let card = app.buttons["continue-tt0000002"]
        try reach(card, in: app)
        let before = try await fixturePasses()
        XCUIRemote.shared.press(.select, forDuration: 0.9)
        let close = app.buttons.matching(NSPredicate(format: "label == %@", "Close")).firstMatch
        XCTAssertTrue(close.waitForExistence(timeout: 10), "Long select must open library details")
        try await Task.sleep(for: .seconds(1))
        let afterLongSelect = try await fixturePasses()
        XCTAssertEqual(afterLongSelect, before, "Long select must never also invoke short-select playback")
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "04-library-long-select-details"; shot.lifetime = .keepAlways; add(shot)
        XCUIRemote.shared.press(.menu)
        let returned = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND hasFocus == true"), object: card)
        XCTAssertEqual(XCTWaiter.wait(for: [returned], timeout: 8), .completed)
        XCUIRemote.shared.press(.select)
        var started = false
        for _ in 0..<20 {
            if try await fixturePasses() > before { started = true; break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertTrue(started, "Short select after closing details must still play")
        for _ in 0..<2 {
            if card.exists && card.hasFocus { break }
            XCUIRemote.shared.press(.menu)
            try await Task.sleep(for: .seconds(1))
        }
        let returnedAfterPlay = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND hasFocus == true"), object: card)
        XCTAssertEqual(XCTWaiter.wait(for: [returnedAfterPlay], timeout: 10), .completed)
    }

    @MainActor
    private func reach(_ target: XCUIElement, in app: XCUIApplication) throws {
        guard target.waitForExistence(timeout: 20) else {
            XCTFail("Missing production focus target: \(target)"); throw Stop()
        }
        for _ in 0..<24 {
            if target.hasFocus { return }
            let current = app.descendants(matching: .any)
                .matching(NSPredicate(format: "hasFocus == true")).firstMatch
            guard current.exists else {
                XCUIRemote.shared.press(.down)
                continue
            }
            let from = current.frame, to = target.frame
            let direction: XCUIRemote.Button
            if to.minY >= from.maxY - 8 { direction = .down }
            else if to.maxY <= from.minY + 8 { direction = .up }
            else { direction = to.midX > from.midX ? .right : .left }
            XCUIRemote.shared.press(direction)
        }
        XCTFail("Siri Remote cannot focus \(target.identifier)"); throw Stop()
    }

    @MainActor
    private func checkBackplate(_ target: XCUIElement, in app: XCUIApplication,
                                name: String, card: Bool) throws {
        // Let the 160 ms scale transition settle before measuring the screenshot.
        Thread.sleep(forTimeInterval: 0.35)
        let shot = app.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
        let frame = target.frame
        let viewport = app.windows.firstMatch.frame
        XCTAssertTrue(viewport.contains(frame), "Focus target must be fully visible")
        let regions: [CGRect]
        if card {
            // The fixture has no artwork. Only sample the blank right side of the
            // subtitle panel, below the artwork and away from title/subtitle ink.
            regions = [CGRect(x: frame.maxX - frame.width * 0.22,
                              y: frame.maxY - 22, width: frame.width * 0.15, height: 14)]
        } else {
            // Outside the app's purple outline and away from centered white text.
            regions = [CGRect(x: frame.minX + 14, y: frame.minY - 9,
                              width: frame.width - 28, height: 6),
                       CGRect(x: frame.minX + 14, y: frame.maxY + 3,
                              width: frame.width - 28, height: 6),
                       CGRect(x: frame.minX + 8, y: frame.minY + 10,
                              width: 6, height: frame.height - 20)]
        }
        let image = try XCTUnwrap(shot.image.cgImage)
        let width = image.width, height = image.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let rendered = pixels.withUnsafeMutableBytes { bytes -> Bool in
            guard let context = CGContext(data: bytes.baseAddress, width: width, height: height,
                bitsPerComponent: 8, bytesPerRow: width * 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        XCTAssertTrue(rendered, "Screenshot must decode for pixel assertions")
        guard rendered else { throw Stop() }
        let sx = CGFloat(width) / viewport.width, sy = CGFloat(height) / viewport.height
        for (index, region) in regions.enumerated() {
            XCTAssertTrue(viewport.contains(region), "Sample must not be clipped")
            var white = 0, count = 0
            for y in Int((region.minY - viewport.minY) * sy)..<Int((region.maxY - viewport.minY) * sy) {
                for x in Int((region.minX - viewport.minX) * sx)..<Int((region.maxX - viewport.minX) * sx) {
                    guard x >= 0, x < width, y >= 0, y < height else { continue }
                    let offset = (y * width + x) * 4
                    let rgb = [Int(pixels[offset]), Int(pixels[offset + 1]), Int(pixels[offset + 2])]
                    if rgb.min()! >= 200 && rgb.max()! - rgb.min()! <= 28 { white += 1 }
                    count += 1
                }
            }
            XCTAssertGreaterThan(count, 50, "Empty pixel sample must fail")
            let fraction = Double(white) / Double(max(count, 1))
            print("FOCUS_PIXELS \(name) region=\(index) rect=\(region) white=\(white)/\(count)")
            XCTAssertLessThan(fraction, 0.08,
                              "\(name): near-white backplate occupies \(fraction) of blank region \(index)")
        }
    }
}

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
