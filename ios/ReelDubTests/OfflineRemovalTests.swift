import XCTest
@testable import ReelDub

@MainActor
final class OfflineRemovalTests: XCTestCase {
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
