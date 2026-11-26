import UniformTypeIdentifiers
import XCTest
@testable import ReelDub

final class ReelSubmissionTests: XCTestCase {
    @MainActor
    func testInstagramURLAndCaptionWorkAlongsideAPreviewImage() async throws {
        let expected = "https://www.instagram.com/reel/ABCde123/"
        let url = try XCTUnwrap(URL(string: expected))
        for provider in [NSItemProvider(object: url as NSURL), NSItemProvider(object: ("Watch this " + expected) as NSString)] {
            let item = NSExtensionItem()
            item.attachments = [NSItemProvider(object: UIImage()), provider]
            guard case .reel(let value) = try await SharedReel.extract(from: [item]) else { return XCTFail("Expected Reel") }
            XCTAssertEqual(value, expected)
        }
        let item = NSExtensionItem()
        item.attributedContentText = NSAttributedString(string: expected)
        guard case .reel(let value) = try await SharedReel.extract(from: [item]) else { return XCTFail("Expected caption") }
        XCTAssertEqual(value, expected)
    }

    func testBothModesSubmitToTheSameDirectEndpoint() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [SubmissionCapture.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let api = API(connection: Connection(base: try Connection.server("https://studio.example"), token: "fixture"), transport: session)
        for mode in DubQuality.allCases {
            let job = try await api.submit(.reel("https://www.instagram.com/reel/ABCde123/"), id: UUID(), target: "en", quality: mode)
            XCTAssertEqual(job.quality, mode.rawValue)
        }
    }
}

private final class SubmissionCapture: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        XCTAssertEqual(request.url?.path, "/v1/jobs")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture")
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
