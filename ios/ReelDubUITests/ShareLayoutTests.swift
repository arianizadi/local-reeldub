import XCTest

@MainActor
final class ShareLayoutTests: XCTestCase {
    func testReelDubOpensFromTheSystemShareSheet() throws {
        guard let value = ProcessInfo.processInfo.environment["REELDUB_SHARE_FIXTURE_URL"],
              let url = URL(string: value), url.host == "127.0.0.1" else {
            throw XCTSkip("Requires a local simulator page with a Web Share button.")
        }
        continueAfterFailure = false
        let safari = XCUIApplication(bundleIdentifier: "com.apple.mobilesafari")
        safari.open(url)
        let share = safari.buttons["Share fixture video"]
        for title in ["Continue", "Not Now", "Done"] where !share.exists {
            let setup = safari.buttons[title].firstMatch
            if setup.exists && setup.isHittable { setup.tap() }
        }
        XCTAssertTrue(share.waitForExistence(timeout: 8), safari.debugDescription)
        share.tap()
        let reelDub = safari.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "ReelDub")).firstMatch
        let more = safari.buttons["More"].firstMatch
        let actionsLoaded = NSPredicate { _, _ in reelDub.exists || more.exists }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: actionsLoaded, object: nil)],
                                      timeout: 25), .completed, safari.debugDescription)
        if !reelDub.exists || !reelDub.isHittable {
            XCTAssertTrue(more.exists, safari.debugDescription)
            if !more.isHittable {
                let apps = safari.scrollViews.containing(.button, identifier: "More").firstMatch
                if apps.exists { apps.swipeLeft() }
            }
            more.tap()
        }
        XCTAssertTrue(reelDub.waitForExistence(timeout: 6), safari.debugDescription)
        reelDub.tap()
        let close = safari.buttons["Close"].firstMatch
        XCTAssertTrue(close.waitForExistence(timeout: 8), safari.debugDescription)
        XCTAssertTrue(safari.staticTexts["Sign in to ReelDub first"].exists)
        XCTAssertTrue(close.isHittable)
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = "ReelDub share extension"
        attachment.lifetime = .keepAlways
        add(attachment)
        close.tap()
        XCTAssertTrue(share.waitForExistence(timeout: 5))
    }
}
