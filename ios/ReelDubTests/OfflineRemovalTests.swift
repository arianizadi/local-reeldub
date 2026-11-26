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
        case "/v1/me": body = ["library_id": host, "output_languages": ["en"], "quality_languages": ["best": ["en"]]] as [String: Any]
        case "/v1/jobs/fixture" where request.httpMethod == "DELETE":
            let revision = request.value(forHTTPHeaderField: "X-ReelDub-Library-Revision") ?? "missing"
            Self.lock.withLock { Self.counts.add("removal/" + host + "/" + revision) }
            code = 412
            body = ["detail": "Requested again"]
        case "/v1/jobs": body = [Self.job]
        case "/v1/jobs/fixture/poster":
            Self.lock.withLock { Self.counts.add("poster/" + host) }
            body = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMwypgDAAIGATcR4TxKAAAAAElFTkSuQmCC")!
        case "/v1/jobs/fixture/received":
            Self.lock.withLock { Self.counts.add(host) }
            code = 410
            body = ["detail": "Expired"]
        default: code = 404; body = [:] as [String: String]
        }
        let reply = HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: reply, cacheStoragePolicy: .notAllowed)
        let data = (body as? Data) ?? (try! JSONSerialization.data(withJSONObject: body))
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}

@MainActor
final class OfflineRemovalTests: XCTestCase {
    func testSavingReplacesAnIncompleteMovieDirectory() throws {
        let host = UUID().uuidString + ".example"
        let connection = Connection(base: URL(string: "https://" + host)!, token: "test", libraryID: host)
        let library = try XCTUnwrap(DeviceLibrary(connection: connection))
        defer { try? library.erase() }
        let job = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: ExpiredReceiptServer.job))
        let directory = library.movie(job.id).deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data("partial".utf8).write(to: directory.appending(path: "job.json"))
        XCTAssertNil(library.inventory()[job.id])
        let temp = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try Data([1, 2, 3]).write(to: temp)
        try library.save(temp, job: job)
        XCTAssertEqual(library.inventory()[job.id]?.id, job.id)
        XCTAssertEqual(try Data(contentsOf: library.movie(job.id)), Data([1, 2, 3]))
    }

    func testSavedPosterSurvivesColdLaunchAndIsRemovedWithMovie() async throws {
        let host = UUID().uuidString.lowercased() + ".example"
        let connection = Connection(base: URL(string: "https://" + host)!, token: "fixture", libraryID: host)
        let library = try XCTUnwrap(DeviceLibrary(connection: connection))
        defer { try? library.erase() }
        let job = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: ExpiredReceiptServer.job))
        let movie = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try Data([1]).write(to: movie)
        try library.save(movie, job: job)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ExpiredReceiptServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let api = API(connection: connection, transport: session)
        let key = RDPosterStore.key(for: job.id, api: api)
        for _ in 0..<2 {
            let image = await RDPosterStore().image(for: job.id, key: key, api: api)
            XCTAssertNotNil(image)
        }
        XCTAssertEqual(ExpiredReceiptServer.receipts("poster/" + host), 1)
        XCTAssertTrue(FileManager.default.fileExists(atPath: library.poster(job.id).path))
        try library.remove(job.id)
        let remote = await RDPosterStore().image(for: job.id, key: key, api: api)
        XCTAssertNotNil(remote)
        XCTAssertEqual(ExpiredReceiptServer.receipts("poster/" + host), 2)
        XCTAssertFalse(FileManager.default.fileExists(atPath: library.poster(job.id).path))
    }

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

    func testMissingDeletionEndpointDoesNotBlockFreshJobsOrForgetRemovedDownloads() async throws {
        let host = UUID().uuidString.lowercased() + ".example"
        let connection = Connection(base: URL(string: "https://" + host)!, token: "fixture", libraryID: host)
        let library = try XCTUnwrap(DeviceLibrary(connection: connection))
        defer { try? library.erase() }
        for id in ["fixture", "old"] {
            var row = ExpiredReceiptServer.job
            row["id"] = id
            let job = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: row))
            let temp = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
            try Data([1]).write(to: temp)
            try library.save(temp, job: job)
            try library.markReceived(id)
            if id == "old" {
                // An earlier ReelDub build removed the download but kept the library entry.
                let folder = library.movie(id).deletingLastPathComponent()
                let markers = library.root.appending(path: ".manual-downloads")
                try FileManager.default.createDirectory(at: markers, withIntermediateDirectories: true)
                try Data().write(to: markers.appending(path: folder.lastPathComponent))
                try library.retain(job)
                try FileManager.default.removeItem(at: folder)
            }
        }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ExpiredReceiptServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let studio = Studio(connection: connection, transport: session)
        await studio.refresh()
        XCTAssertNil(studio.failure)
        XCTAssertNotNil(studio.account)
        XCTAssertEqual(Set(studio.jobs.map(\.id)), ["fixture", "old"])
        XCTAssertTrue(studio.canDownloadAgain("old"))
        try library.remove("old")
        XCTAssertFalse(library.isManualDownload("old"))
        XCTAssertNil(library.retainedJobs()["old"])
    }

    func testRemovingTwoLibraryEntriesOfflineReclaimsBytesAndSurvivesRelaunch() async throws {
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
        let exported = try library.export(ids[0])
        defer { try? FileManager.default.removeItem(at: exported) }
        for id in ids.prefix(2) {
            try studio.removeFromLibrary(XCTUnwrap(studio.jobs.first { $0.id == id }))
            XCTAssertNil(studio.localMovie(id))
            XCTAssertFalse(FileManager.default.fileExists(atPath: library.movie(id).path))
        }
        let reopened = Studio(connection: connection)
        XCTAssertEqual(Set(reopened.saved.keys), [ids[2]])
        XCTAssertEqual(Set(reopened.jobs.map(\.id)), [ids[2]])
        XCTAssertEqual(Set(library.pendingRemovals().keys), Set(ids.prefix(2)))
        await reopened.syncLibraryRemovals()
        XCTAssertTrue(reopened.hasPendingRemovals)
        XCTAssertTrue(FileManager.default.fileExists(atPath: exported.path))

    }
    func testNewServerRequestReusingRemovedIDSurvivesOfflineRemovalSync() async throws {
        let host = UUID().uuidString.lowercased() + ".example"
        let connection = Connection(base: URL(string: "https://" + host)!, token: "fixture", libraryID: host)
        let library = try XCTUnwrap(DeviceLibrary(connection: connection))
        defer { try? library.erase() }
        var payload = ExpiredReceiptServer.job
        payload["last_requested"] = "2026-11-18T12:00:00Z"
        let old = try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: payload))
        try library.retain(old)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ExpiredReceiptServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let studio = Studio(connection: connection, transport: session)
        try studio.removeFromLibrary(old)
        await studio.refresh()
        XCTAssertFalse(studio.jobs.contains { $0.id == old.id })
        await studio.syncLibraryRemovals()
        for _ in 0..<100 where !studio.jobs.contains(where: { $0.id == old.id }) {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(ExpiredReceiptServer.receipts("removal/" + host + "/2026-11-18T12:00:00Z"), 1)
        XCTAssertFalse(studio.hasPendingRemovals)
        XCTAssertTrue(studio.jobs.contains { $0.id == old.id })
    }

}
