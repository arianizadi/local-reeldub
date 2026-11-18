import XCTest

final class StudioSmoke: XCTestCase {
    func testConnectedStudioCanPlayAndReport() throws {
        guard let key = ProcessInfo.processInfo.environment["REELDUB_TEST_KEY"], !key.isEmpty else {
            throw XCTSkip("Set TEST_RUNNER_REELDUB_TEST_KEY to exercise a running local server.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launch()
        app.buttons["Server and account"].tap()
        let address = app.textFields["https://your-server.example"]
        XCTAssertTrue(address.waitForExistence(timeout: 4))
        let secret = app.secureTextFields["Connection key"]
        secret.tap()
        secret.typeText(key)
        app.buttons["Connect with key"].tap()
        let dismissPasswordPrompt = app.buttons["Not Now"]
        if dismissPasswordPrompt.waitForExistence(timeout: 4) { dismissPasswordPrompt.tap() }
        let job = app.buttons.matching(identifier: "job-done").firstMatch
        XCTAssertTrue(job.waitForExistence(timeout: 15))
        job.tap()
        app.buttons["Play English dub"].tap()
        XCTAssertTrue(app.buttons["Share English video"].waitForExistence(timeout: 20))
        let report = app.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'issue report' OR label == 'Report an issue'")).firstMatch
        report.tap()
        let note = app.descendants(matching: .any).matching(NSPredicate(format: "placeholderValue BEGINSWITH %@", "For example")).firstMatch
        note.tap()
        note.typeText("Local simulator verification of the report workflow.")
        app.buttons["Send"].tap()
        XCTAssertTrue(app.buttons["Update issue report"].waitForExistence(timeout: 8))
    }
}
