import XCTest
@testable import ReelDub

private final class ExpiredReceiptServer: URLProtocol {
    static let job = ["id": "fixture", "url": "", "state": "done", "stage": "Ready",
                      "created": "2026-11-18", "reported": false, "available_on_server": true] as [String: Any]
    static let counts = NSCountedSet()
    static let lock = NSLock()
    static func receipts(_ host: String) -> Int { lock.withLock { counts.count(for: host) } }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        let url = request.url!, host = url.host!
        var code = 200
        let body: Any
        switch url.path {
        case "/v1/me": body = ["tier": "free", "used": 0, "library_id": host] as [String: Any]
        case "/v1/jobs": body = [Self.job]
        case "/v1/jobs/fixture/received":
            Self.lock.withLock { Self.counts.add(host) }
            code = 410
            body = ["detail": "Expired"]
        default: code = 404; body = [:] as [String: String]
        }
        let reply = HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: reply, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
        client?.urlProtocolDidFinishLoading(self)
    }
}

@MainActor
final class OfflineRemovalTests: XCTestCase {
    func testExpiredReceiptStopsAutomaticRetriesAndPreservesTheDeviceCopy() async throws {
        let host = UUID().uuidString.lowercased() + ".example"
        let connection = Connection(base: URL(string: "https://" + host)!, token: "fixture", libraryID: host)
        let library = try XCTUnwrap(DeviceLibrary(connection: connection))
        defer { try? library.erase() }
        let job = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: ExpiredReceiptServer.job))
        let file = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try Data([1, 2, 3]).write(to: file)
        try library.save(file, job: job)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ExpiredReceiptServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let studio = Studio(connection: connection, transport: session)
        await studio.refresh()
        for _ in 0..<100 where studio.transferring != nil { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertNil(studio.transferring)
        XCTAssertNil(studio.nextTransferRetry)
        studio.retryTransfers()
        await studio.refresh()
        XCTAssertNil(studio.transferring)
        XCTAssertEqual(ExpiredReceiptServer.receipts(host), 1)
        XCTAssertNotNil(studio.localMovie(job.id))

        var last: TransferRetry?
        let date = Date()
        for seconds in [60, 120, 240, 480, 960, 1920, 3600, 3600] {
            last = TransferRetry.next(for: job, error: URLError(.timedOut), previous: last, at: date)
            XCTAssertEqual(last?.deadline, date.addingTimeInterval(Double(seconds)))
        }
    }

    func testProgressOnlyTracksActiveDubsAndReconcilesCompletions() throws {
        func job(_ id: String, _ state: String) throws -> Job {
            try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: [
                "id": id, "url": "", "state": state, "stage": state, "created": "2026-11-18", "reported": false
            ]))
        }
        let studio = Studio(connection: nil)
        let saved = try job("saved", "done")
        studio.jobs = [saved]
        XCTAssertNil(studio.progressDelay(failures: 0))
        studio.jobs.append(try job("dub", "queued"))
        XCTAssertEqual(studio.progressDelay(failures: 0), 10)
        XCTAssertTrue(studio.applyActiveProgress([try job("dub", "running")], expected: ["dub"]))
        XCTAssertEqual(studio.jobs.map(\.id), ["saved", "dub"])
        XCTAssertEqual(studio.progressDelay(failures: 0), 5)
        XCTAssertEqual([1, 2, 3, 4].map { studio.progressDelay(failures: $0) }, [15, 30, 60, 60])
        XCTAssertFalse(studio.applyActiveProgress([try job("dub", "done")], expected: ["dub"]))
        XCTAssertFalse(studio.applyActiveProgress([], expected: ["dub"]))
    }

    func testRemovingTwoDownloadsWithServerOfflineSurvivesRelaunch() async throws {
        let connection = Connection(base: URL(string: "http://127.0.0.1:1")!, token: "offline-test", libraryID: UUID().uuidString)
        let library = try XCTUnwrap(DeviceLibrary(connection: connection))
        defer { try? library.erase() }
        var ids: [String] = []
        for _ in 0..<3 {
            let id = UUID().uuidString
            let job = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: [
                "id":id, "url":"https://www.instagram.com/reel/OfflineExample/", "state":"done", "stage":"Ready",
                "created":"2026-11-18T12:00:00Z", "reported":false, "available_on_server":true]))
            let temp = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
            try Data(repeating: 7, count: 65536).write(to: temp)
            try library.save(temp, job: job)
            ids.append(id)
        }
        let studio = Studio(connection: connection)
        await studio.refresh()
        XCTAssertNotNil(studio.failure)
        XCTAssertEqual(studio.saved.count, 3)
        for id in ids.prefix(2) {
            try studio.removeDownload(id)
            XCTAssertNil(studio.localMovie(id))
            XCTAssertFalse(FileManager.default.fileExists(atPath: library.movie(id).path))
        }
        let reopened = Studio(connection: connection)
        XCTAssertEqual(Set(reopened.saved.keys), [ids[2]])
        XCTAssertTrue(reopened.canDownloadAgain(ids[0]))
        XCTAssertTrue(reopened.canDownloadAgain(ids[1]))
        XCTAssertFalse(reopened.canDownloadAgain(ids[2]))
        try reopened.downloadAgain(ids[0])
        XCTAssertFalse(library.isManualDownload(ids[0]))
        XCTAssertTrue(library.isManualDownload(ids[1]))
    }
}
