import XCTest

// The host fixture moves the native simulator hinge. Stable Duo runtimes support
// Device Hub posture changes but reject the newer in-process XCUIHinge API.
struct DuoHingeFixture {
    let endpoint: URL
    init?() {
        guard let raw = ProcessInfo.processInfo.environment["REELDUB_DUO_HINGE_URL"],
              let url = URL(string: raw), url.scheme == "http", url.host == "127.0.0.1" else { return nil }
        endpoint = url
    }

    struct Panel: Decodable {
        let width: CGFloat
        let height: CGFloat
    }

    @discardableResult
    func move(to pose: String) async throws -> Panel {
        let data = try await request(pose)
        return try JSONDecoder().decode(Panel.self, from: data)
    }

    func screenshot(named name: String) async throws -> XCTAttachment {
        let data = try await request("screenshot")
        let image = XCTAttachment(data: data, uniformTypeIdentifier: "public.png")
        image.name = name
        image.lifetime = .keepAlways
        return image
    }

    private func request(_ action: String) async throws -> Data {
        var request = URLRequest(url: endpoint.appendingPathComponent(action))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 25
        let (data, response) = try await URLSession.shared.data(for: request)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        return data
    }
}

@MainActor
final class DuoLayouts: XCTestCase {
    func testFormAndHelpSurviveFoldingAndRotation() async throws {
        let device = XCUIDevice.shared
        guard let hinge = DuoHingeFixture() else {
            throw XCTSkip("Run deploy/qa/test_duo_layout.py to control the simulated hinge.")
        }
        let originalOrientation = device.orientation
        addTeardownBlock { @MainActor in
            try await hinge.move(to: "closed")
            if originalOrientation != .unknown { device.orientation = originalOrientation }
        }

        continueAfterFailure = false
        try await hinge.move(to: "closed")
        device.orientation = .portrait
        let app = XCUIApplication()
        app.launch()
        let selfHost = app.buttons["Use my own server"]
        if !selfHost.waitForExistence(timeout: 8) {
            throw XCTSkip("Run on a signed-out iPhone Duo simulator.")
        }
        add(try await hinge.screenshot(named: "Duo closed — Welcome"))
        selfHost.tap()
        let field = app.textFields["Server address"]
        XCTAssertTrue(field.waitForExistence(timeout: 4))
        field.tap()
        field.typeText("https://duo.example")
        let restore = app.buttons["self-host-restore"]
        let closed = try await hinge.move(to: "closed")
        let open = try await hinge.move(to: "open")
        XCTAssertGreaterThan(min(open.width, open.height), min(closed.width, closed.height))
        XCTAssertEqual(field.value as? String, "https://duo.example")
        add(try await hinge.screenshot(named: "Duo open — Server form"))
        let rotated = try await hinge.move(to: "left")
        XCTAssertEqual(rotated.width, open.height)
        XCTAssertEqual(rotated.height, open.width)
        XCTAssertEqual(field.value as? String, "https://duo.example")
        add(try await hinge.screenshot(named: "Duo open rotated — Server form"))
        let restored = try await hinge.move(to: "right")
        XCTAssertEqual(restored.width, open.width)
        XCTAssertEqual(restored.height, open.height)
        try await hinge.move(to: "tabletop")
        XCTAssertTrue(field.waitForExistence(timeout: 4))
        XCTAssertEqual(field.value as? String, "https://duo.example")
        add(try await hinge.screenshot(named: "Duo half-open — Server form"))
        XCTAssertTrue(app.buttons["Cancel"].isHittable)
        XCTAssertEqual(field.value as? String, "https://duo.example")
        app.buttons["Cancel"].tap()
        let faq = app.buttons["Help & FAQ"]
        if !faq.isHittable { app.swipeUp() }
        faq.tap()
        let question = app.buttons["How long can my video be?"]
        XCTAssertTrue(question.waitForExistence(timeout: 5))
        question.tap()
        let answer = app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH %@", "Choose a video up to 2 minutes")).firstMatch
        XCTAssertTrue(answer.exists)
        add(try await hinge.screenshot(named: "Duo half-open — Help"))
        try await hinge.move(to: "closed")
        device.orientation = .portrait
        XCTAssertTrue(answer.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Done"].isHittable)
        add(try await hinge.screenshot(named: "Duo closed again — Help"))
        app.buttons["Done"].tap()
        selfHost.tap()
        XCTAssertTrue(restore.waitForExistence(timeout: 5))
        XCTAssertTrue(restore.isHittable)
    }
}
