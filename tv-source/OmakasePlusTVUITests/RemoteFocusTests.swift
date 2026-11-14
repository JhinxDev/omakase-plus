import XCTest

final class RemoteFocusTests: XCTestCase {
    @MainActor
    func testBackRestoresFocusToTheSelectedShelfCard() {
        let app = XCUIApplication()
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
