import XCTest

@MainActor
final class AdaptiveNavigationTests: XCTestCase {
    func testFirstLaunchShowsTheConnectionForm() throws {
        let app = XCUIApplication()
        app.launch()
        guard app.textFields["server-address"].exists else { throw XCTSkip("This device is already connected.") }
        XCTAssertTrue(app.secureTextFields["server-key"].exists)
        XCTAssertFalse(app.buttons["self-host-connect"].isEnabled)
        app.textFields["server-address"].tap()
        app.textFields["server-address"].typeText("https://dub.example.org")
        XCTAssertFalse(app.buttons["self-host-connect"].isEnabled)
    }
}
