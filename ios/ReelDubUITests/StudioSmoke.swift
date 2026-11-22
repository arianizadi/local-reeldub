import XCTest
import Vision

final class StudioSmoke: XCTestCase {
    func testRangeSelectionAndConfirmedDeletion() throws {
        #if targetEnvironment(simulator)
        let environment = ProcessInfo.processInfo.environment
        guard let port = environment["REELDUB_DRAG_FIXTURE_PORT"].flatMap(UInt16.init),
              let key = environment["REELDUB_DRAG_FIXTURE_KEY"], !key.isEmpty else {
            throw XCTSkip("Run python3 deploy/qa/test_drag_selection.py with a signed-out simulator UUID.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launch()
        let selfHost = app.buttons["Use my own server"]
        XCTAssertTrue(selfHost.waitForExistence(timeout: 8), "Use a signed-out QA simulator.")
        selfHost.tap()
        let address = app.textFields["Server address"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap()
        address.typeText("http://127.0.0.1:\(port)")
        let secret = app.secureTextFields["Connection key"]
        secret.tap()
        secret.typeText(key)
        app.buttons["Connect with key"].tap()

        func row(_ index: Int) -> XCUIElement {
            let title = "Drag \(key) " + String(format: "%02d", index)
            return app.buttons.matching(NSPredicate(format: "identifier == %@ AND label CONTAINS %@", "job-failed", title)).firstMatch
        }
        func drag(_ from: Int, through to: Int) {
            let center = CGVector(dx: 0.5, dy: 0.5)
            row(from).coordinate(withNormalizedOffset: center).press(
                forDuration: 0.1, thenDragTo: row(to).coordinate(withNormalizedOffset: center),
                withVelocity: .slow, thenHoldForDuration: 0.1)
        }
        func capture(_ name: String) {
            let image = XCTAttachment(screenshot: app.screenshot())
            image.name = name
            image.lifetime = .keepAlways
            add(image)
        }

        // A unique title proves this is our synthetic library before any selection or deletion.
        XCTAssertTrue(row(0).waitForExistence(timeout: 12), app.debugDescription)
        addTeardownBlock {
            app.terminate()
            app.launch()
            let options = app.buttons["library-options"]
            XCTAssertTrue(options.waitForExistence(timeout: 5))
            options.tap()
            app.buttons["Account"].tap()
            let signOut = app.buttons["Sign out"]
            if !signOut.isHittable { app.swipeUp() }
            signOut.tap()
            XCTAssertTrue(selfHost.waitForExistence(timeout: 5))
        }
        app.terminate()
        app.launch()
        XCTAssertTrue(row(0).waitForExistence(timeout: 8))
        app.buttons["library-options"].tap()
        app.buttons["Select dubs"].tap()
        let delete = app.buttons["delete-selected"]
        XCTAssertTrue(delete.waitForExistence(timeout: 4), app.debugDescription)
        row(0).tap()
        drag(1, through: 3)
        XCTAssertEqual(delete.label, "Delete (4)")
        for index in 0...3 { XCTAssertTrue(row(index).isSelected) }
        drag(2, through: 1)
        XCTAssertEqual(delete.label, "Delete (2)")
        XCTAssertTrue(row(0).isSelected)
        XCTAssertFalse(row(1).isSelected)
        XCTAssertFalse(row(2).isSelected)
        XCTAssertTrue(row(3).isSelected)
        capture("Drag selection before confirmation")

        delete.tap()
        let confirm = app.buttons["Delete 2 dubs from library"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 4))
        let cancel = app.buttons["Cancel"]
        if cancel.exists && cancel.isHittable {
            cancel.tap()
        } else {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.12)).tap()
        }
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 3))
        XCTAssertEqual(delete.label, "Delete (2)")
        XCTAssertTrue(row(0).exists)
        delete.tap()
        confirm.tap()
        XCTAssertTrue(row(0).waitForNonExistence(timeout: 6))
        XCTAssertTrue(row(3).waitForNonExistence(timeout: 6))
        for index in [1, 2, 4] { XCTAssertTrue(row(index).exists) }
        capture("Library after confirmed deletion")

        #else
        throw XCTSkip("The loopback library fixture runs only on a simulator.")
        #endif
    }

    private func welcome(_ app: XCUIApplication) {
        app.launch()
        if app.buttons["library-options"].waitForExistence(timeout: 2) {
            app.buttons["library-options"].tap()
            app.buttons["account"].tap()
            app.buttons["Sign out"].tap()
        }
        XCTAssertTrue(app.buttons["continue-apple"].waitForExistence(timeout: 6))
    }

    func testFAQIsAvailableBeforeSignIn() {
        continueAfterFailure = false
        let app = XCUIApplication()
        welcome(app)
        let help = app.buttons["Help & FAQ"]
        if !help.isHittable { app.swipeUp() }
        help.tap()
        let length = app.buttons["How long can my video be?"]
        XCTAssertTrue(length.waitForExistence(timeout: 4))
        length.tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH %@", "Choose a video up to 2 minutes")).firstMatch.exists)
        let hosting = app.buttons["Can I host ReelDub myself?"]
        if !hosting.isHittable { app.swipeUp() }
        hosting.tap()
        XCTAssertTrue(app.links["Open-source setup guide"].exists || app.buttons["Open-source setup guide"].exists)
    }

    func testConsoleOnSignedInAdministratorDevice() throws {
        let app = XCUIApplication()
        app.launch()
        guard app.buttons["library-options"].waitForExistence(timeout: 6) else { throw XCTSkip("Signed-in device required.") }
        app.buttons["library-options"].tap()
        app.buttons["account"].tap()
        guard app.buttons["Admin console"].waitForExistence(timeout: 8) else { throw XCTSkip("Pinned administrator required.") }
        app.buttons["Admin console"].tap()
        XCTAssertTrue(app.buttons["continue-apple"].waitForExistence(timeout: 6))
        XCTAssertFalse(app.buttons["Render lab"].exists)

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

    private func connectToPlaybackLibrary(_ app: XCUIApplication) throws -> XCUIElement {
        guard let key = ProcessInfo.processInfo.environment["REELDUB_TEST_KEY"], !key.isEmpty else {
            throw XCTSkip("Provide TEST_RUNNER_REELDUB_TEST_KEY and a completed job on the simulator's local server.")
        }
        continueAfterFailure = false
        welcome(app)
        let welcomeImage = XCTAttachment(screenshot: app.screenshot())
        welcomeImage.name = "Welcome"
        welcomeImage.lifetime = .keepAlways
        add(welcomeImage)
        app.buttons["Use my own server"].tap()
        let address = app.textFields["Server address"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap()
        let port = ProcessInfo.processInfo.environment["REELDUB_TEST_PORT"].flatMap(UInt16.init) ?? 8300
        address.typeText("http://127.0.0.1:\(port)")
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
        return job
    }

    func testPlaybackPreferencesPersistAcrossRelaunch() throws {
        let app = XCUIApplication()
        let job = try connectToPlaybackLibrary(app)
        job.tap()
        XCTAssertTrue(app.buttons["Share English video"].waitForExistence(timeout: 15))
        let subtitles = app.switches["english-subtitles"]
        XCTAssertTrue(subtitles.waitForExistence(timeout: 12))
        let sound = app.buttons["playback-sound"]
        XCTAssertTrue(sound.exists)
        let initialSound = sound.label
        XCTAssertTrue(["Mute video", "Unmute video"].contains(initialSound))
        let previous = subtitles.value as? String
        XCTAssertNotNil(previous)
        if ProcessInfo.processInfo.environment["REELDUB_TEST_FRESH_PLAYBACK_PREFS"] == "1" {
            XCTAssertEqual(previous, "1")
            XCTAssertEqual(initialSound, "Mute video")
        }
        if let cue = ProcessInfo.processInfo.environment["REELDUB_TEST_CAPTION"], !cue.isEmpty {
            let deadline = Date().addingTimeInterval(10)
            var visible = false
            repeat {
                let scan = VNRecognizeTextRequest()
                try VNImageRequestHandler(data: app.screenshot().pngRepresentation).perform([scan])
                visible = (scan.results ?? []).contains { result in
                    result.topCandidates(1).first?.string.localizedCaseInsensitiveContains(cue) == true
                }
                if !visible { RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
            } while !visible && Date() < deadline
            XCTAssertTrue(visible, "The selected caption cue should be visible on the video.")
        }
        let playing = XCTAttachment(screenshot: app.screenshot())
        playing.name = "Dub playing with sound and caption controls"
        playing.lifetime = .keepAlways
        add(playing)
        sound.tap()
        subtitles.tap()
        XCTAssertNotEqual(subtitles.value as? String, previous)
        let changedSound = initialSound == "Mute video" ? "Unmute video" : "Mute video"
        XCTAssertEqual(sound.label, changedSound)
        app.terminate()
        app.launch()
        XCTAssertTrue(job.waitForExistence(timeout: 8))
        job.tap()
        XCTAssertTrue(subtitles.waitForExistence(timeout: 12))
        XCTAssertNotEqual(subtitles.value as? String, previous)
        XCTAssertEqual(sound.label, changedSound)
        sound.tap()
        subtitles.tap()
        XCTAssertEqual(sound.label, initialSound)
        XCTAssertEqual(subtitles.value as? String, previous)
    }

    func testWelcomeAndSelfHostedPlayback() throws {
        let app = XCUIApplication()
        let job = try connectToPlaybackLibrary(app)
        app.buttons["add-reel"].tap()
        let composer = app.textFields["new-dub"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        if !app.keyboards.firstMatch.waitForExistence(timeout: 2) { composer.tap() }
        composer.typeText("example")
        app.buttons["confirm-dub"].tap()
        XCTAssertTrue(app.staticTexts["reel-link-error"].waitForExistence(timeout: 4))
        app.buttons["Clear link"].tap()
        app.buttons["Close"].tap()
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
