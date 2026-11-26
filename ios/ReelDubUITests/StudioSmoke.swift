import XCTest
import Vision

@MainActor
final class StudioSmoke: XCTestCase {
    private func enterServer(_ app: XCUIApplication, url: String, key: String) {
        let address = app.textFields["server-address"]
        XCTAssertTrue(address.waitForExistence(timeout: 5))
        address.tap()
        address.typeText(url)
        let secret = app.secureTextFields["server-key"]
        secret.tap()
        secret.typeText(key)
    }

    private func testAndConnect(_ app: XCUIApplication) {
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

    private func prepareRangeSelection(_ app: XCUIApplication, signOutAfterTest: Bool) throws -> (Int) -> XCUIElement {
        let environment = ProcessInfo.processInfo.environment
        guard let port = environment["REELDUB_DRAG_FIXTURE_PORT"].flatMap(UInt16.init),
              let key = environment["REELDUB_DRAG_FIXTURE_KEY"], !key.isEmpty else {
            throw XCTSkip("Run python3 deploy/qa/test_drag_selection.py with a signed-out simulator UUID.")
        }
        continueAfterFailure = false
        app.launch()
        let selfHost = app.textFields["server-address"]
        XCTAssertTrue(selfHost.waitForExistence(timeout: 8), "Use a signed-out QA simulator.")
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
                app.buttons["Settings"].tap()
                let signOut = app.buttons["Disconnect"]
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

    private func connectToPlaybackLibrary(_ app: XCUIApplication) throws -> XCUIElement {
        guard let key = ProcessInfo.processInfo.environment["REELDUB_TEST_KEY"], !key.isEmpty else {
            throw XCTSkip("Provide TEST_RUNNER_REELDUB_TEST_KEY and a completed job on the simulator's local server.")
        }
        continueAfterFailure = false
        app.launch()
        let welcomeImage = XCTAttachment(screenshot: app.screenshot())
        welcomeImage.name = "Welcome"
        welcomeImage.lifetime = .keepAlways
        add(welcomeImage)
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

    func testManageFixtureQueueFromLibraryAndDetails() throws {
        guard ProcessInfo.processInfo.environment["REELDUB_QUEUE_UI"] == "1" else {
            throw XCTSkip("Requires a disposable queue fixture.")
        }
        let app = XCUIApplication()
        app.launch()
        continueAfterFailure = false
        let beta = "Queue QA Beta"
        let running = "Queue QA Running"
        let betaMenu = app.buttons["Actions for " + beta]
        guard betaMenu.waitForExistence(timeout: 10), app.buttons["Actions for " + running].exists else {
            XCTFail("Expected labelled disposable fixtures before changing the queue.")
            return
        }
        func row(_ title: String, state: String, detail: String = "") -> XCUIElement {
            let candidates = app.buttons.matching(NSPredicate(format: "identifier == %@ AND label CONTAINS %@", "job-" + state, title))
            return detail.isEmpty ? candidates.firstMatch : candidates.matching(NSPredicate(format: "label CONTAINS %@", detail)).firstMatch
        }
        func pick(_ title: String, _ action: String) {
            app.buttons["Actions for " + title].tap()
            app.buttons[action].firstMatch.tap()
        }

        pick(beta, "Move to front")
        XCTAssertTrue(row(beta, state: "queued", detail: "Next in line").waitForExistence(timeout: 6))
        pick(beta, "Move to back")
        XCTAssertTrue(row(beta, state: "queued", detail: "2 dubs queued ahead").waitForExistence(timeout: 6))
        pick(beta, "Cancel dub")
        app.sheets.buttons["confirm-cancel-dub"].firstMatch.tap()
        XCTAssertTrue(row(beta, state: "cancelled").waitForExistence(timeout: 6))

        row(running, state: "running").tap()
        app.buttons["Manage dub"].tap()
        app.buttons["Cancel dub"].firstMatch.tap()
        app.sheets.buttons["confirm-cancel-dub"].firstMatch.tap()
        XCTAssertTrue(app.buttons["Translate again"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["delete-dub"].exists)
    }

    func testWelcomeAndSelfHostedPlayback() throws {
        let app = XCUIApplication()
        let job = try connectToPlaybackLibrary(app)
        app.buttons["add-video"].tap()
        let upload = app.textFields["video-link"]
        XCTAssertTrue(upload.waitForExistence(timeout: 3))
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
