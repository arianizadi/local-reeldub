import UniformTypeIdentifiers
import XCTest
@testable import ReelDub

final class ReelSubmissionTests: XCTestCase {
    @MainActor
    func testVideoURLsAndCaptionsWorkAlongsideAPreviewImage() async throws {
        let expected = "https://www.youtube.com/watch?v=AbCdE123&t=5"
        let url = try XCTUnwrap(URL(string: expected))
        for provider in [NSItemProvider(object: url as NSURL), NSItemProvider(object: ("Watch this " + expected) as NSString)] {
            let item = NSExtensionItem()
            item.attachments = [NSItemProvider(object: UIImage()), provider]
            guard case .link(let value) = try await SharedVideo.extract(from: [item]) else { return XCTFail("Expected Reel") }
            XCTAssertEqual(value, expected)
        }
        let expectedCaption = "https://vimeo.com/123456789"
        let item = NSExtensionItem()
        item.attributedContentText = NSAttributedString(string: expectedCaption)
        guard case .link(let value) = try await SharedVideo.extract(from: [item]) else { return XCTFail("Expected caption") }
        XCTAssertEqual(value, expectedCaption)
    }

    func testQueueControlsSendTheExpectedOperationAndDecodeCancellation() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [QueueCapture.self]
        let transport = URLSession(configuration: config)
        defer { transport.invalidateAndCancel() }
        let api = API(connection: Connection(base: try Connection.server("https://studio.example"), token: "fixture"), transport: transport)
        for (action, result) in [(QueueAction.first, "queued"), (.last, "queued"), (.cancel, "cancelled")] {
            let job = try await api.changeQueue("fixture", action: action)
            XCTAssertEqual(job.state, result)
            XCTAssertEqual(job.rdPhase.isActive, action != .cancel)
            if action == .first { XCTAssertEqual(job.ahead, 0) }
            if action == .last { XCTAssertEqual(job.ahead, 5) }
        }
    }

    @MainActor
    func testCancellationRaceKeepsTheFinishedVideoAndRefreshesItsState() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [CompletionRace.self]
        let transport = URLSession(configuration: config)
        defer { transport.invalidateAndCancel() }
        let server = try Connection.server("https://studio.example")
        let studio = Studio(connection: Connection(base: server, token: "fixture", libraryID: "queue-race",
                                                  outputLanguages: ["en"], qualityLanguages: ["best":["en"]]), transport: transport)
        let running = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: CompletionRace.job("running")))
        studio.jobs = [running]
        await studio.changeQueue(running, action: .cancel)
        XCTAssertEqual(studio.jobs.first?.state, "done")
        XCTAssertEqual(studio.queueIssue, "Already completed.")
        XCTAssertFalse(studio.queueBusy)
    }

    func testBothModesSubmitToTheSameDirectEndpoint() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [SubmissionCapture.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let api = API(connection: Connection(base: try Connection.server("https://studio.example"), token: "fixture"), transport: session)
        for mode in DubQuality.allCases {
            let job = try await api.submit(.link("https://www.instagram.com/reel/ABCde123/"), id: UUID(), target: "en", quality: mode)
            XCTAssertEqual(job.quality, mode.rawValue)
        }
    }
}

private func bodyData(_ request: URLRequest) -> Data {
    let bytes: Data
    if let body = request.httpBody { bytes = body }
    else if let stream = request.httpBodyStream {
        stream.open()
        defer { stream.close() }
        var result = Data()
        var buffer = [UInt8](repeating: 0, count: 512)
        while stream.hasBytesAvailable {
        let n = stream.read(&buffer, maxLength: buffer.count)
        if n <= 0 { break }
        result.append(contentsOf: buffer.prefix(n))
        }
        bytes = result
    } else { bytes = Data() }
    return bytes
}

private final class QueueCapture: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture")
        let body = try? JSONDecoder().decode([String: String].self, from: bodyData(request))
        let cancel = request.url?.path == "/v1/jobs/fixture/cancel"
        if cancel { XCTAssertEqual(body, [:]) }
        else {
            XCTAssertEqual(request.url?.path, "/v1/jobs/fixture/queue")
            XCTAssertTrue(["first", "last"].contains(body?["position"] ?? ""))
        }
        let payload: [String: Any] = ["id":"fixture", "url":"", "created":"now", "state":cancel ? "cancelled" : "queued",
                                      "stage":cancel ? "Cancelled" : "Queued", "ahead":body?["position"] == "last" ? 5 : 0, "reported":false]
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: (try? JSONSerialization.data(withJSONObject: payload)) ?? Data())
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private final class SubmissionCapture: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        XCTAssertEqual(request.url?.path, "/v1/jobs")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture")
        let bytes = bodyData(request)
        let body = (try? JSONDecoder().decode([String: String].self, from: bytes)) ?? [:]
        XCTAssertEqual(Set(body.keys), ["url", "request_id", "target_language", "quality"])
        XCTAssertNotNil(body["request_id"].flatMap(UUID.init(uuidString:)))
        let payload: [String: Any] = ["id": "1", "url": body["url"] ?? "", "created":"now", "state":"queued",
                                       "stage":"Queued", "reported":false, "quality":body["quality"] ?? ""]
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: (try? JSONSerialization.data(withJSONObject: payload)) ?? Data())
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}


private final class CompletionRace: URLProtocol {
    static func job(_ state: String) -> [String: Any] {
        ["id":"fixture", "url":"", "state":state, "stage":state, "created":"now", "reported":false, "available_on_server":false]
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let status: Int
        let payload: Any
        switch request.url?.path {
        case "/v1/jobs/fixture/cancel": status = 409; payload = ["detail":"Already completed."]
        case "/v1/jobs": status = 200; payload = [Self.job("done")]
        case "/v1/me": status = 200; payload = ["library_id":"queue-race", "output_languages":["en"], "quality_languages":["best":["en"]]] as [String: Any]
        default: status = 404; payload = [:] as [String: Any]
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: (try? JSONSerialization.data(withJSONObject: payload)) ?? Data())
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
