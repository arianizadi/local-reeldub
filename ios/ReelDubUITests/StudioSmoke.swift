import XCTest

final class StudioSmoke: XCTestCase {
    private func welcome(_ app: XCUIApplication) {
        app.launch()
        if app.buttons["Account"].waitForExistence(timeout: 2) {
            app.buttons["Account"].tap()
            app.buttons["Sign out"].tap()
        }
        XCTAssertTrue(app.buttons["continue-apple"].waitForExistence(timeout: 6))
    }

    func testHostedSignInOpensAppleAuthorization() throws {
        guard ProcessInfo.processInfo.environment["REELDUB_TEST_HOSTED_AUTH"] == "1" else {
            throw XCTSkip("Enable the live hosted sign-in smoke test on a configured iPhone simulator.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        welcome(app)
        app.buttons["continue-apple"].tap()
        let sheetLabels = NSPredicate(format: "label CONTAINS[c] 'Apple Account' OR label CONTAINS[c] 'Apple ID' OR label CONTAINS[c] 'Sign in with Apple'")
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let presented = app.descendants(matching: .any).matching(sheetLabels).firstMatch.waitForExistence(timeout: 8)
            || springboard.descendants(matching: .any).matching(sheetLabels).firstMatch.waitForExistence(timeout: 5)
        let image = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        image.name = "Apple authorization"
        image.lifetime = .keepAlways
        add(image)
        XCTAssertTrue(presented, "Expected the operating system's Apple authorization UI.")
    }

    func testWelcomeAndSelfHostedPlayback() throws {
        guard let key = ProcessInfo.processInfo.environment["REELDUB_TEST_KEY"], !key.isEmpty else {
            throw XCTSkip("Provide TEST_RUNNER_REELDUB_TEST_KEY and a completed job on the simulator's local server.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        welcome(app)
        let welcomeImage = XCTAttachment(screenshot: app.screenshot())
        welcomeImage.name = "Welcome"
        welcomeImage.lifetime = .keepAlways
        add(welcomeImage)
        app.buttons["Use my own server"].tap()
        let address = app.textFields["Server address"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap()
        address.typeText("http://127.0.0.1:8300")
        let secret = app.secureTextFields["Connection key"]
        secret.tap()
        secret.typeText(key)
        app.buttons["Connect with key"].tap()
        let prompt = app.buttons["Not Now"]
        if prompt.waitForExistence(timeout: 2) { prompt.tap() }
        let job = app.buttons.matching(identifier: "job-done").firstMatch
        XCTAssertTrue(job.waitForExistence(timeout: 15))
        app.buttons["Dub in English"].tap()
        XCTAssertTrue(app.staticTexts["Reel link error. Paste an Instagram Reel link to get started."].waitForExistence(timeout: 4))
        app.swipeUp()
        if !job.isHittable { app.swipeDown() }
        job.tap()
        XCTAssertTrue(app.buttons["Share English video"].waitForExistence(timeout: 15))
        let report = app.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'issue report' OR label == 'Report an issue'")).firstMatch
        if !report.isHittable { app.swipeUp() }
        report.tap()
        let note = app.descendants(matching: .any).matching(NSPredicate(format: "placeholderValue BEGINSWITH %@", "For example")).firstMatch
        note.tap()
        note.typeText("Simulator verification of the issue report.")
        app.buttons["Send"].tap()
        XCTAssertTrue(app.buttons["Update issue report"].waitForExistence(timeout: 8))
    }
}
