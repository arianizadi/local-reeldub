import XCTest
@testable import ReelDub

final class TransportPolicyTests: XCTestCase {
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

    func testConsoleProofAndGrantAreConfinedToTheHostedAdminAPI() throws {
        let cloud = API(connection: Connection(base: Connection.hosted, token: "fixture"), adminGrant: "grant")
        XCTAssertEqual(try cloud.request("v1/admin/overview").value(forHTTPHeaderField: "X-ReelDub-Admin"), "grant")
        XCTAssertNil(try cloud.request("v1/me").value(forHTTPHeaderField: "X-ReelDub-Admin"))
        let privateServer = try Connection.server("https://private.example")
        let own = API(connection: Connection(base: privateServer, token: "fixture"), adminGrant: "grant")
        XCTAssertThrowsError(try own.request("v1/admin/unlock", method: "POST", body: ["identity_token": "proof"]))
        XCTAssertThrowsError(try own.request("v1/admin/accounts", method: "POST"))
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
