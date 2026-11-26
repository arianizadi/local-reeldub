import CoreMedia
import UniformTypeIdentifiers
import XCTest
@testable import ReelDub

final class TransportPolicyTests: XCTestCase {
    func testSharedMovieOwnsAValidatedCopyAfterProviderReturns() async throws {
        let sample = try XCTUnwrap(Bundle(for: type(of: self)).url(forResource: "clip", withExtension: "mp4"))
        let provider = NSItemProvider()
        provider.suggestedName = "Original clip.mp4"
        provider.registerFileRepresentation(forTypeIdentifier: UTType.mpeg4Movie.identifier, fileOptions: [], visibility: .all) { completion in
            completion(sample, false, nil)
            return nil
        }
        let movie = try await ImportedMovie.shared(from: provider)
        defer { movie.remove() }
        XCTAssertEqual(movie.title, "Original clip")
        XCTAssertNotEqual(movie.file, sample)
        XCTAssertEqual(try Data(contentsOf: movie.file), try Data(contentsOf: sample))
        movie.remove()
        XCTAssertFalse(FileManager.default.fileExists(atPath: movie.file.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: sample.path))
    }

    func testFileURLProviderImportsFilesMovieAndRejectsAnInternetLink() async throws {
        let sample = try XCTUnwrap(Bundle(for: type(of: self)).url(forResource: "clip", withExtension: "mp4"))
        let provider = NSItemProvider(item: sample as NSURL, typeIdentifier: UTType.fileURL.identifier)
        let movie = try await ImportedMovie.shared(from: provider)
        defer { movie.remove() }
        XCTAssertEqual(try Data(contentsOf: movie.file), try Data(contentsOf: sample))

        let webpage = try XCTUnwrap(URL(string: "https://example.invalid/movie.mp4"))
        let link = NSItemProvider(item: webpage as NSURL, typeIdentifier: UTType.url.identifier)
        do {
            _ = try await ImportedMovie.shared(from: link)
            XCTFail("A link is not a shared movie")
        } catch { XCTAssertEqual(error.localizedDescription, ImportedMovie.typeError) }
    }

    func testMovieImportRejectsEmptyAndDisguisedFiles() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
        defer { try? FileManager.default.removeItem(at: url) }
        for (bytes, issue) in [(Data(), "This video is empty."), (Data("text".utf8), ImportedMovie.typeError)] {
            try bytes.write(to: url)
            do {
                _ = try await ImportedMovie.copy(url)
                XCTFail("Invalid video was accepted")
            } catch { XCTAssertEqual(error.localizedDescription, issue) }
        }
    }

    func testRecoveryMessagesDistinguishWaitingFromAConfirmedHandoff() throws {
        let row: [String: Any] = ["id": "demo", "url": "", "stage": "Recovering render", "state": "queued", "created": "now", "reported": false]
        func job(_ added: [String: Any]) throws -> Job {
            try JSONDecoder().decode(Job.self, from: JSONSerialization.data(withJSONObject: row.merging(added) { _, new in new }))
        }
        XCTAssertNil(try job(["stage": "Queued"]).recoveryNotice)
        XCTAssertEqual(try job(["stage": "Waiting for a render server"]).recoveryCue, "Waiting for a render server")
        XCTAssertTrue(try XCTUnwrap(job(["recovery_count": 1, "ahead": 0]).recoveryNotice).contains("front"))
        XCTAssertFalse(try XCTUnwrap(job(["recovery_count": 1, "ahead": 2]).recoveryNotice).contains("front"))
        XCTAssertFalse(try XCTUnwrap(job(["state": "running", "recovery_count": 1]).recoveryNotice).contains("another server"))
        XCTAssertTrue(try XCTUnwrap(job(["state": "running", "recovery_count": 1, "recovery_switched_server": true]).recoveryNotice).contains("another server"))
        let created = "2026-11-21T00:00:00Z"
        let waiting = try job(["created": created, "recovery_count": 1]).timing(at: XCTUnwrap(Job.date(created)))
        XCTAssertEqual(waiting, "Recovery priority · Queued · 0s waiting")
        XCTAssertEqual(try job(["ahead":0]).queuePosition(processing: 2), "Next in line · 2 processing")
        XCTAssertEqual(try job(["ahead":0]).queuePosition(processing: 0), "Next in line")
        XCTAssertEqual(try job(["ahead":1]).queuePosition(processing: 2), "1 dub queued ahead")
        XCTAssertEqual(try job(["ahead":3]).queuePosition(processing: 0), "3 dubs queued ahead")
        XCTAssertEqual(try job([:]).queuePosition(processing: 3), "Queued")
    }

    func testCaptionsFromPasteAndShareUseTheSameInstagramValidation() {
        XCTAssertEqual(RDLink.reel(in: "A clip: https://m.instagram.com/reel/ABCDE/"), "https://m.instagram.com/reel/ABCDE/")
        XCTAssertNotNil(RDLink.reel(in: "Watch HTTPS://WWW.INSTAGRAM.COM/reels/ABcd1/ today"))
        XCTAssertEqual(RDLink.reel(in: "https://example.com/ then https://www.instagram.com/reel/abc12/"), "https://www.instagram.com/reel/abc12/")
        XCTAssertNil(RDLink.reel(in: "https://instagram.com.example.invalid/reel/abc12/"))
        XCTAssertNil(RDLink.reel(in: "https://instagram.com/p/abc12/"))
        XCTAssertEqual(RDLink.reel(in: "https://www.instagram.com/share/reel/AbCdE/?igsh=123"), "https://www.instagram.com/share/reel/AbCdE/?igsh=123")
    }

    func testStoredConnectionsAreValidatedBeforeAttachingTheSessionToken() throws {
        for origin in ["http://reeldub.arianizadi.com", "http://my-server.local", "http://192.168.1.2",
                       "https://user@example.org", "https://example.org/other"] {
            let connection = Connection(base: try XCTUnwrap(URL(string: origin)), token: "test-only")
            XCTAssertThrowsError(try API(connection: connection).request("v1/me"), origin)
        }
        let base = try Connection.server("https://my-server.local:8443")
        let request = try API(connection: Connection(base: base, token: "test-only")).request("v1/me")
        XCTAssertEqual(request.url?.absoluteString, "https://my-server.local:8443/v1/me")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-only")
    }

    func testPrivateServerKeyCanSubmitAndFetchWithoutAnAccount() throws {
        let server = try Connection.server("https://studio.example")
        let api = API(connection: Connection(base: server, token: "fixture"))
        XCTAssertEqual(try api.request("v1/jobs", method: "POST").value(forHTTPHeaderField: "Authorization"), "Bearer fixture")
        XCTAssertEqual(try api.request("v1/jobs/example/video").url?.host, "studio.example")
    }

    func testPlaintextLoopbackIsConfinedToSimulatorDevelopment() throws {
        for host in ["127.0.0.1", "localhost", "[::1]"] {
            #if DEBUG && targetEnvironment(simulator)
            XCTAssertNoThrow(try Connection.server("http://\(host):8300"))
            #else
            XCTAssertThrowsError(try Connection.server("http://\(host):8300"))
            #endif
        }
    }

    func testPrivateAPISessionDoesNotReuseCookiesCredentialsOrCachedResponses() {
        let config = API.session.configuration
        XCTAssertNil(config.urlCache)
        XCTAssertNil(config.urlCredentialStorage)
        XCTAssertNil(config.httpCookieStorage)
        XCTAssertFalse(config.httpShouldSetCookies)
        XCTAssertEqual(config.tlsMinimumSupportedProtocolVersion, .TLSv12)
    }
}
