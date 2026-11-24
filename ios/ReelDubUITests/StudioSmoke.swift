import XCTest
import Vision

@MainActor
final class StudioSmoke: XCTestCase {
    private func enterServer(_ app: XCUIApplication, url: String, key: String) {
        let address = app.textFields["Server address"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap()
        address.typeText(url)
        let secret = app.secureTextFields["Connection key"]
        secret.tap()
        secret.typeText(key)
    }

    private func testAndConnect(_ app: XCUIApplication) {
        let testButton = app.buttons["self-host-test"]
        testButton.tap()
        let connectButton = app.buttons["self-host-connect"]
        XCTAssertTrue(connectButton.waitForExistence(timeout: 12), app.debugDescription)
        connectButton.tap()
        // The fixture key can trigger an asynchronous system prompt as the library appears.
        // Its controls are hosted by different processes across iOS releases.
        let hosts = [app, XCUIApplication(bundleIdentifier: "com.apple.springboard"),
                     XCUIApplication(bundleIdentifier: "com.apple.SafariViewService")]
        let candidates = hosts.map { $0.buttons["Not Now"].firstMatch }
        let deadline = Date().addingTimeInterval(14)
        var tappedVisiblePrompt = false
        repeat {
            if let target = visiblePasswordDismissal(in: app) {
                app.coordinate(withNormalizedOffset: target).tap()
                tappedVisiblePrompt = true
            } else if tappedVisiblePrompt {
                return
            } else if let dismiss = candidates.first(where: { $0.exists && $0.isHittable }) {
                dismiss.tap()
                if dismiss.waitForNonExistence(timeout: 1), visiblePasswordDismissal(in: app) == nil {
                    return
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.4))
        } while Date() < deadline
    }

    /// Some runtimes draw the system password prompt above the app but expose only the library in XCTest.
    /// In the simulator fixture, require both labels and full-screen coordinates before tapping.
    private func visiblePasswordDismissal(in app: XCUIApplication) -> CGVector? {
        #if targetEnvironment(simulator)
        let screen = XCUIScreen.main.screenshot().image
        let bounds = app.frame
        guard bounds.width > 0, bounds.height > 0, bounds.minX == 0, bounds.minY == 0,
              abs(screen.size.width / screen.size.height - bounds.width / bounds.height) < 0.02 else { return nil }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let upright = UIGraphicsImageRenderer(size: screen.size, format: format).image { _ in
            screen.draw(in: CGRect(origin: .zero, size: screen.size))
        }
        guard let frame = upright.cgImage else { return nil }
        let scan = VNRecognizeTextRequest()
        scan.recognitionLevel = .fast
        scan.recognitionLanguages = ["en-US"]
        guard (try? VNImageRequestHandler(cgImage: frame).perform([scan])) != nil else { return nil }
        let items = (scan.results ?? []).compactMap { $0.topCandidates(1).first }
        guard items.contains(where: { $0.string.localizedCaseInsensitiveContains("Save Password?") }),
              let dismiss = items.first(where: { $0.string.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "not now" }),
              let box = try? dismiss.boundingBox(for: dismiss.string.startIndex..<dismiss.string.endIndex) else { return nil }
        return CGVector(dx: box.boundingBox.midX, dy: 1 - box.boundingBox.midY)
        #else
        return nil
        #endif
    }

    func testFreeSelfHostConnection() throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["REELDUB_SELFHOST_PROBE_URL"], let key = env["REELDUB_SELFHOST_PROBE_KEY"] else {
            throw XCTSkip("Run the disposable self-host connection fixture.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        welcome(app)
        let entry = app.buttons["Use my own server"]
        entry.tap()
        let restore = app.buttons["self-host-restore"]
        XCTAssertTrue(restore.waitForExistence(timeout: 5))
        let buy = app.buttons["self-host-buy"]
        XCTAssertTrue(buy.waitForExistence(timeout: 12), app.debugDescription)
        XCTAssertTrue(buy.label.contains("4.99"), buy.label)
        XCTAssertFalse(buy.isEnabled)
        restore.tap()
        let noPurchase = app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH %@", "No self-host purchase was found")).firstMatch
        XCTAssertTrue(noPurchase.waitForExistence(timeout: 10), app.debugDescription)

        enterServer(app, url: url, key: key)
        app.buttons["self-host-test"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["self-host-verified"].waitForExistence(timeout: 10))
        XCTAssertTrue(buy.isEnabled)
        XCTAssertFalse(app.buttons["library-options"].exists)
        let image = XCTAttachment(screenshot: app.screenshot())
        image.name = "Self-host unlock review"
        image.lifetime = .keepAlways
        add(image)

        let secret = app.secureTextFields["Connection key"]
        secret.tap()
        secret.typeText("x")
        XCTAssertFalse(buy.isEnabled, "Changing the tested credentials must invalidate checkout.")
        app.buttons["self-host-test"].tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH %@", "Your server did not recognize")).firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(buy.isEnabled)
        app.terminate()
        app.launch()
        XCTAssertTrue(entry.waitForExistence(timeout: 8), "A free probe must not save the tested credentials.")
    }

    func testRestoreSelfHostOnFreshDevice() throws {
        guard ProcessInfo.processInfo.environment["REELDUB_UI_STORE"] == "owned" else {
            throw XCTSkip("Run with a locally verified StoreKit purchase.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        let entry = app.buttons["Use my own server"]
        let restore = app.buttons["self-host-restore"]
        app.launch()
        XCTAssertTrue(entry.waitForExistence(timeout: 8), "Restore must be reachable on a signed-out device.")
        entry.tap()
        restore.tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH %@", "Purchase restored")).firstMatch.waitForExistence(timeout: 12), app.debugDescription)
        XCTAssertTrue(app.secureTextFields["Connection key"].exists)
        XCTAssertFalse(app.buttons["self-host-connect"].exists, "The new device must still test its server.")
    }

    private func prepareRangeSelection(_ app: XCUIApplication, signOutAfterTest: Bool) throws -> (Int) -> XCUIElement {
        let environment = ProcessInfo.processInfo.environment
        guard let port = environment["REELDUB_DRAG_FIXTURE_PORT"].flatMap(UInt16.init),
              let key = environment["REELDUB_DRAG_FIXTURE_KEY"], !key.isEmpty else {
            throw XCTSkip("Run python3 deploy/qa/test_drag_selection.py with a signed-out simulator UUID.")
        }
        continueAfterFailure = false
        app.launch()
        let selfHost = app.buttons["Use my own server"]
        XCTAssertTrue(selfHost.waitForExistence(timeout: 8), "Use a signed-out QA simulator.")
        selfHost.tap()
        enterServer(app, url: "http://127.0.0.1:\(port)", key: key)
        testAndConnect(app)

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
        // A unique title proves this is our synthetic library before any selection or deletion.
        XCTAssertTrue(row(0).waitForExistence(timeout: 12), app.debugDescription)
        if signOutAfterTest {
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
        XCTAssertEqual(delete.label, "Remove (4)")
        for index in 0...3 { XCTAssertTrue(row(index).isSelected) }
        drag(2, through: 1)
        XCTAssertEqual(delete.label, "Remove (2)")
        XCTAssertTrue(row(0).isSelected)
        XCTAssertFalse(row(1).isSelected)
        XCTAssertFalse(row(2).isSelected)
        XCTAssertTrue(row(3).isSelected)
        capture(app, "Drag selection before confirmation")

        return row
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let image = XCTAttachment(screenshot: app.screenshot())
        image.name = name
        image.lifetime = .keepAlways
        add(image)
    }

    func testLibrarySelectionSurvivesUnfolding() async throws {
        guard let hinge = DuoHingeFixture() else {
            throw XCTSkip("Run the disposable Duo fixture.")
        }
        let closed = try await hinge.move(to: "closed")
        let app = XCUIApplication()
        defer { app.terminate() }
        let row = try prepareRangeSelection(app, signOutAfterTest: false)
        let inner = try await hinge.move(to: "open")
        XCTAssertGreaterThan(min(inner.width, inner.height), min(closed.width, closed.height))
        XCTAssertEqual(app.buttons["delete-selected"].label, "Remove (2)")
        XCTAssertTrue(row(0).isSelected)
        XCTAssertTrue(row(3).isSelected)
        add(try await hinge.screenshot(named: "Duo unfolded library keeps selection"))
    }

    func testRangeSelectionAndConfirmedDeletion() throws {
        #if targetEnvironment(simulator)
        let app = XCUIApplication()
        let row = try prepareRangeSelection(app, signOutAfterTest: true)
        let delete = app.buttons["delete-selected"]
        delete.tap()
        let confirm = app.buttons.matching(identifier: "confirm-library-removal").firstMatch
        XCTAssertTrue(confirm.waitForExistence(timeout: 4))
        let cancel = app.buttons["Cancel"]
        if cancel.exists && cancel.isHittable {
            cancel.tap()
        } else {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.25, dy: 0.12)).tap()
        }
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 3))
        XCTAssertEqual(delete.label, "Remove (2)")
        XCTAssertTrue(row(0).exists)
        delete.tap()
        confirm.tap()
        XCTAssertTrue(row(0).waitForNonExistence(timeout: 6))
        XCTAssertTrue(row(3).waitForNonExistence(timeout: 6))
        for index in [1, 2, 4] { XCTAssertTrue(row(index).exists) }
        capture(app, "Library after confirmed deletion")

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
        let port = ProcessInfo.processInfo.environment["REELDUB_TEST_PORT"].flatMap(UInt16.init) ?? 8300
        enterServer(app, url: "http://127.0.0.1:\(port)", key: key)
        testAndConnect(app)
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

        let device = XCUIDevice.shared
        let startingPose = device.orientation
        defer { device.orientation = startingPose }
        device.orientation = .landscapeLeft
        let share = app.buttons["Share English video"]
        let landscape = NSPredicate { _, _ in
            app.frame.width > app.frame.height && share.isHittable && app.frame.contains(share.frame)
        }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: landscape, object: nil)], timeout: 6), .completed)
        XCTAssertTrue(sound.isHittable)
        XCTAssertTrue(subtitles.isHittable)
        let wide = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        wide.name = "Landscape playback controls"
        wide.lifetime = .keepAlways
        add(wide)
        device.orientation = .portrait

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
        let upload = app.buttons["choose-video"]
        let update = app.staticTexts["This server needs an update to accept video uploads."]
        XCTAssertTrue(upload.waitForExistence(timeout: 3) || update.exists)
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
