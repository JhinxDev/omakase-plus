import XCTest
import UIKit

// Runs the actual authenticated Home and navigation, using the existing loopback
// companion fixture. No duplicate button renderer or app-only test view.
final class RemoteFocusRenderingTests: XCTestCase {
    private struct Stop: Error {}

    private struct FixturePlayback: Decodable {
        let freshPassesOpened: Int
        let checkpointSeq: Int

        enum CodingKeys: String, CodingKey {
            case freshPassesOpened = "fresh_passes_opened"
            case checkpointSeq = "checkpoint_seq"
        }
    }

    @MainActor
    private func launchHome() throws -> XCUIApplication {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["-omakase.tv.server-origin", "https://127.0.0.1:8854"]
        app.launch()
        if app.buttons["live.enter"].waitForExistence(timeout: 10) {
            try reach(app.buttons["live.enter"], in: app)
            XCUIRemote.shared.press(.select)
            try reach(app.buttons["live.showCode"], in: app)
            XCUIRemote.shared.press(.select)
        }
        XCTAssertFalse(app.staticTexts["Couldn’t restore your profile"].exists,
                       "The simulator must enable Keychain access before the app can pair.\n\(app.debugDescription)")
        let home = app.buttons.matching(NSPredicate(
            format: "identifier == %@ OR label == %@", "nav-home", "Home")).firstMatch
        try reach(home, in: app)
        return app
    }

    @MainActor
    func testHomeNavigationHasNoWhiteBackplate() async throws {
        let app = try launchHome()
        let home = app.buttons.matching(NSPredicate(
            format: "identifier == %@ OR label == %@", "nav-home", "Home")).firstMatch
        try await checkBackplate(home, in: app, name: "01-home-tab", card: false)
    }

    @MainActor
    func testContinueWatchingSelectReturnHasNoWhiteBackplate() async throws {
        let app = try launchHome()
        XCUIRemote.shared.press(.select)

        let card = app.buttons["home-card-continue-movies-qa-movie"]
        try reach(card, in: app)
        try await checkBackplate(card, in: app, name: "02-continue-watching", card: true)
        let before = try await fixturePlayback().freshPassesOpened
        XCUIRemote.shared.press(.select)
        try await waitForPlayback(after: before)
        try await returnFromPlayer(to: card, in: app)
        try await checkBackplate(card, in: app, name: "03-returned-card", card: true)
    }

    @MainActor
    func testAnimeContinueWatchingUsesEpisodeStillAndFullPoster() async throws {
        let app = try launchHome()
        XCUIRemote.shared.press(.select)

        let episode = app.buttons["home-card-continue-anime-qa-anime-7101"]
        try reach(episode, in: app)
        try await checkAnimeArtwork(episode, in: app, expectEpisode: true)

        let fallback = app.buttons["home-card-continue-anime-qa-anime-7102"]
        try reach(fallback, in: app)
        try await checkAnimeArtwork(fallback, in: app, expectEpisode: false)
    }

    @MainActor
    private func checkAnimeArtwork(_ card: XCUIElement, in app: XCUIApplication,
                                   expectEpisode: Bool) async throws {
        let viewport = app.windows.firstMatch.frame
        let frame = try await visibleFocusFrame(card, in: viewport)
        let artworkHeight = frame.width * 214 / 380
        let y = frame.minY + artworkHeight * 0.21
        let name = expectEpisode ? "05-anime-episode-still" : "06-anime-poster-fallback"
        for attempt in 0..<16 {
            try await Task.sleep(for: .milliseconds(350))
            let screenshot = app.screenshot()
            let (pixels, width, height) = try renderedPixels(screenshot)

            func pixel(at fraction: CGFloat) -> (r: Int, g: Int, b: Int) {
                let x = frame.minX + frame.width * fraction
                let column = Int((x - viewport.minX) * CGFloat(width) / viewport.width)
                let row = Int((y - viewport.minY) * CGFloat(height) / viewport.height)
                guard (0..<width).contains(column), (0..<height).contains(row) else { return (255, 255, 255) }
                let offset = (row * width + column) * 4
                return (Int(pixels[offset]), Int(pixels[offset + 1]), Int(pixels[offset + 2]))
            }

            let side = pixel(at: 0.18), center = pixel(at: 0.5)
            let matches = expectEpisode
                ? side.b > side.g + 50 && side.b > side.r + 120 && center.b > center.g + 50
                : center.g > center.r + 95 && center.g > center.b + 95 && max(side.r, side.g, side.b) < 95
            if matches || attempt == 15 {
                let attachment = XCTAttachment(screenshot: screenshot)
                attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
                XCTAssertTrue(matches, "\(name) rendered unexpected artwork. Side: \(side), center: \(center).")
                return
            }
        }
    }

    private func fixturePlayback() async throws -> FixturePlayback {
        let url = try XCTUnwrap(URL(string: "https://127.0.0.1:8854/qa/state"))
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        let (data, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        return try JSONDecoder().decode(FixturePlayback.self, from: data)
    }

    @MainActor
    private func waitForPlayback(after previousPasses: Int) async throws {
        // Creating the pass precedes presenting AVKit. The fixture resets the
        // checkpoint sequence when it opens a new pass.
        for _ in 0..<20 {
            let proof = try await fixturePlayback()
            if proof.freshPassesOpened == previousPasses + 1 && proof.checkpointSeq > 0 { return }
            try await Task.sleep(for: .seconds(1))
        }
        XCTFail("Select must open one authenticated playback pass and reach its first checkpoint")
        throw Stop()
    }

    @MainActor
    private func returnFromPlayer(to card: XCUIElement, in app: XCUIApplication) async throws {
        let ready = NSPredicate(format: "exists == true AND enabled == true")
        // AVKit may consume one Menu press to hide its transport. Give each
        // transition time to finish before deciding whether to press again.
        for _ in 0..<2 {
            if card.exists && card.isEnabled { break }
            XCUIRemote.shared.press(.menu)
            let redisplayed = XCTNSPredicateExpectation(predicate: ready, object: card)
            if await XCTWaiter.fulfillment(of: [redisplayed], timeout: 2.5) == .completed { break }
        }
        let focused = NSPredicate(format: "exists == true AND enabled == true AND hasFocus == true")
        let restored = XCTNSPredicateExpectation(predicate: focused, object: card)
        let result = await XCTWaiter.fulfillment(of: [restored], timeout: 10)
        XCTAssertEqual(result, .completed,
                       "Menu must restore the enabled card that opened playback.\n\(app.debugDescription)")
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
        let before = try await fixturePlayback().freshPassesOpened
        XCUIRemote.shared.press(.select, forDuration: 0.9)
        let close = app.buttons.matching(NSPredicate(format: "label == %@", "Close")).firstMatch
        XCTAssertTrue(close.waitForExistence(timeout: 10), "Long select must open library details")
        try await Task.sleep(for: .seconds(1))
        let afterLongSelect = try await fixturePlayback().freshPassesOpened
        XCTAssertEqual(afterLongSelect, before, "Long select must never also invoke short-select playback")
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "04-library-long-select-details"; shot.lifetime = .keepAlways; add(shot)
        XCUIRemote.shared.press(.menu)
        let returned = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND hasFocus == true"), object: card)
        XCTAssertEqual(XCTWaiter.wait(for: [returned], timeout: 8), .completed)
        XCUIRemote.shared.press(.select)
        try await waitForPlayback(after: before)
        try await returnFromPlayer(to: card, in: app)
    }

    @MainActor
    private func reach(_ target: XCUIElement, in app: XCUIApplication) throws {
        guard target.waitForExistence(timeout: 20) else {
            XCTFail("Missing production focus target: \(target)\n\(app.debugDescription)"); throw Stop()
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
    private func visibleFocusFrame(_ target: XCUIElement, in viewport: CGRect) async throws -> CGRect {
        // The focus engine can report the destination before accessibility has
        // published its final frame after the enclosing scroll view moves.
        var previous: CGRect?
        for _ in 0..<20 {
            try await Task.sleep(for: .milliseconds(250))
            let frame = target.frame
            guard target.hasFocus, !frame.isEmpty, viewport.contains(frame) else {
                previous = nil
                continue
            }
            if frame == previous { return frame }
            previous = frame
        }
        XCTFail("\(target.identifier) must settle on screen. Frame: \(target.frame), viewport: \(viewport)")
        throw Stop()
    }

    @MainActor
    private func checkBackplate(_ target: XCUIElement, in app: XCUIApplication,
                                name: String, card: Bool) async throws {
        let viewport = app.windows.firstMatch.frame
        let bounds: CGRect
        if card {
            try await Task.sleep(for: .milliseconds(500))
            XCTAssertTrue(target.hasFocus, "The movie card must have focus before capture")
            bounds = target.frame
        } else {
            bounds = try await visibleFocusFrame(target, in: viewport)
        }
        let shot = app.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
        let (pixels, width, height) = try renderedPixels(shot)
        let sx = CGFloat(width) / viewport.width, sy = CGFloat(height) / viewport.height
        let regions: [CGRect]
        if card {
            // Vertical scrolling can leave the movie button at its unscrolled
            // accessibility offset. Locate the visible artwork outline using
            // the unaffected horizontal bounds and the known artwork ratio.
            let columns = Array(stride(from: Int((bounds.minX - viewport.minX + bounds.width * 0.24) * sx),
                                       to: Int((bounds.minX - viewport.minX + bounds.width * 0.76) * sx), by: 4))
            guard columns.count > 20, columns.allSatisfy({ (0..<width).contains($0) }) else {
                XCTFail("Movie focus outline sample falls outside the card's horizontal bounds")
                throw Stop()
            }
            let strokeRows = (0..<height).filter { row in
                let count = columns.reduce(into: 0) { count, column in
                    let offset = (row * width + column) * 4
                    let r = Int(pixels[offset]), g = Int(pixels[offset + 1]), b = Int(pixels[offset + 2])
                    if r >= 155, r - g >= 8, b >= 175, b - g >= 24 { count += 1 }
                }
                return count * 4 >= columns.count * 3
            }
            let artworkHeight = Int((bounds.width * 214 / 380 * sy).rounded())
            let tolerance = max(5, Int((8 * sy).rounded()))
            guard let top = strokeRows.first(where: { top in
                strokeRows.contains { abs($0 - top - artworkHeight) <= tolerance }
            }) else {
                XCTFail("Could not locate the focused movie artwork outline in the screenshot")
                throw Stop()
            }
            let frame = CGRect(x: bounds.minX, y: viewport.minY + CGFloat(top) / sy,
                               width: bounds.width, height: bounds.height)
            XCTAssertTrue(viewport.contains(frame), "The rendered movie card must be fully visible")
            print("FOCUS_STROKE \(name) screenY=\(top) accessibilityY=\(bounds.minY)")
            // The fixture has no artwork. Only sample the blank right side of the
            // subtitle panel, below the artwork and away from title/subtitle ink.
            regions = [CGRect(x: frame.maxX - frame.width * 0.22,
                              y: frame.maxY - 22, width: frame.width * 0.15, height: 14)]
        } else {
            let frame = bounds
            // Outside the app's purple outline and away from centered white text.
            regions = [CGRect(x: frame.minX + 14, y: frame.minY - 9,
                              width: frame.width - 28, height: 6),
                       CGRect(x: frame.minX + 14, y: frame.maxY + 3,
                              width: frame.width - 28, height: 6),
                       CGRect(x: frame.minX + 8, y: frame.minY + 10,
                              width: 6, height: frame.height - 20)]
        }
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

    private func renderedPixels(_ screenshot: XCUIScreenshot) throws -> ([UInt8], Int, Int) {
        let image = try XCTUnwrap(screenshot.image.cgImage)
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
        return (pixels, width, height)
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
