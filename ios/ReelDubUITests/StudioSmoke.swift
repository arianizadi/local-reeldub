import XCTest
import Vision

final class StudioSmoke: XCTestCase {
    private func welcome(_ app: XCUIApplication) {
        app.launch()
        if app.buttons["library-options"].waitForExistence(timeout: 2) {
            app.buttons["library-options"].tap()
            app.buttons["account"].tap()
            app.buttons["Sign out"].tap()
        }
        XCTAssertTrue(app.buttons["continue-apple"].waitForExistence(timeout: 6))
    }

    func testCreatorCatalogOnSignedInDevice() throws {
        let app = XCUIApplication()
        app.launch()
        guard app.buttons["library-options"].waitForExistence(timeout: 5) else {
            throw XCTSkip("The plan picker requires a signed-in review device.")
        }
        app.buttons["library-options"].tap()
        app.buttons["account"].tap()
        let plans = app.buttons["Plans & minutes"]
        guard plans.waitForExistence(timeout: 3) else { throw XCTSkip("Cloud plans require a Cloud account.") }
        plans.tap()
        XCTAssertTrue(app.staticTexts["5 short dubs each day"].waitForExistence(timeout: 4))
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Creator plan picker"; shot.lifetime = .keepAlways; add(shot)
        XCTAssertTrue(app.staticTexts["Minute pack"].exists)
    }

    func testHostedSignInOpensAppleAuthorization() throws {
        guard ProcessInfo.processInfo.environment["REELDUB_TEST_HOSTED_AUTH"] == "1" else {
            throw XCTSkip("Enable the live hosted sign-in smoke test on a configured iPhone simulator.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        welcome(app)
        app.buttons["continue-apple"].tap()
        // Apple can host the sheet in a separate system process outside both XCUI trees.
        let deadline = Date().addingTimeInterval(15)
        var presented = false
        repeat {
            let scan = VNRecognizeTextRequest()
            scan.recognitionLevel = .accurate
            let screenshot = XCUIScreen.main.screenshot()
            try VNImageRequestHandler(data: screenshot.pngRepresentation).perform([scan])
            let labels = (scan.results ?? []).compactMap { $0.topCandidates(1).first?.string.lowercased() }
            presented = labels.contains { $0.contains("sign in to your apple account") || $0.contains("sign in with apple") || $0.contains("use your apple account") }
            if !presented { RunLoop.current.run(until: Date().addingTimeInterval(0.35)) }
        } while !presented && Date() < deadline
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
        let libraryImage = XCTAttachment(screenshot: app.screenshot())
        libraryImage.name = "Dubs library"
        libraryImage.lifetime = .keepAlways
        add(libraryImage)
        app.buttons["add-reel"].tap()
        let composer = app.textFields["new-dub"]
        composer.tap()
        composer.typeText("example")
        app.buttons["confirm-dub"].tap()
        XCTAssertTrue(app.staticTexts["reel-link-error"].waitForExistence(timeout: 4))
        app.buttons["Clear link"].tap()
        app.buttons["Close"].tap()
        if !job.isHittable { app.swipeDown() }
        job.tap()
        XCTAssertTrue(app.buttons["Share English video"].waitForExistence(timeout: 15))
        let subtitles = app.switches["english-subtitles"]
        XCTAssertTrue(subtitles.waitForExistence(timeout: 12))
        let previous = subtitles.value as? String
        XCTAssertNotNil(previous)
        subtitles.tap()
        XCTAssertNotEqual(subtitles.value as? String, previous)
        subtitles.tap()
        XCTAssertEqual(subtitles.value as? String, previous)
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
