import XCTest

@MainActor
final class AdaptiveNavigationTests: XCTestCase {
    func testWelcomeFAQAndSelfHostAcrossRotation() throws {
        #if targetEnvironment(simulator)
        let app = XCUIApplication()
        app.launch()
        let signIn = app.buttons["continue-apple"]
        if !signIn.waitForExistence(timeout: 8), app.buttons["library-options"].exists {
            throw XCTSkip("This check uses a signed-out simulator.")
        }
        XCTAssertTrue(signIn.exists)
        continueAfterFailure = false

        let device = XCUIDevice.shared
        let priorOrientation = device.orientation
        defer { device.orientation = priorOrientation }
        device.orientation = .portrait

        func tap(_ element: XCUIElement) {
            for _ in 0..<5 where !element.isHittable { app.swipeUp() }
            XCTAssertTrue(element.isHittable, element.debugDescription)
            XCTAssertGreaterThanOrEqual(element.frame.minX, app.frame.minX - 1)
            XCTAssertLessThanOrEqual(element.frame.maxX, app.frame.maxX + 1)
            element.tap()
        }

        func capture(_ name: String) {
            let shot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            shot.name = name
            shot.lifetime = .keepAlways
            add(shot)
        }

        capture("Welcome in portrait")
        tap(app.buttons["Help & FAQ"])
        let question = app.buttons["How long can my video be?"]
        XCTAssertTrue(question.waitForExistence(timeout: 5))
        tap(question)
        let answer = app.staticTexts.containing(
            NSPredicate(format: "label BEGINSWITH %@", "Choose a video up to 2 minutes")
        ).firstMatch
        // At accessibility text sizes the expanded answer begins below the fold.
        for _ in 0..<3 where !answer.exists { app.swipeUp() }
        XCTAssertTrue(answer.exists)
        capture("Expanded FAQ")
        tap(app.buttons["Done"])

        tap(app.buttons["Use my own server"])
        let address = app.textFields["Server address"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        capture("Self-host setup")
        let restore = app.buttons["self-host-restore"]
        for _ in 0..<5 where !restore.isHittable { app.swipeUp() }
        XCTAssertTrue(restore.isHittable)
        tap(app.buttons["Cancel"])

        device.orientation = .landscapeLeft
        tap(app.buttons["Use my own server"])
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        XCTAssertTrue(app.secureTextFields["Connection key"].exists)
        tap(app.buttons["Cancel"])
        XCTAssertTrue(signIn.waitForExistence(timeout: 5))
        XCTAssertGreaterThan(app.frame.width, app.frame.height)
        capture("Welcome in landscape")
        #else
        throw XCTSkip("Run this layout check on a simulator.")
        #endif
    }
}
