import Foundation
import Security

struct Connection: Codable {
    let base: URL
    let token: String

    static func server(_ text: String) throws -> URL {
        guard let parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = parts.host, parts.user == nil, parts.password == nil, parts.query == nil,
              parts.fragment == nil, parts.path == "" || parts.path == "/",
              parts.scheme == "https" || (parts.scheme == "http" && ["localhost","127.0.0.1","::1"].contains(host)),
              let url = parts.url else { throw Problem("Use an HTTPS server address or HTTP on localhost.") }
        return url
    }

    static var vault: [String: Any] {
        var value: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "ReelDub.Connection", kSecAttrAccount as String: "active"]
        if let group = Bundle.main.object(forInfoDictionaryKey: "ReelDubKeychainGroup") as? String,
           !group.hasPrefix("$"), group.contains(".") { value[kSecAttrAccessGroup as String] = group }
        return value
    }

    static func read() -> Connection? {
        var query = vault
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(Self.self, from: data)
    }

    func save() throws {
        let data = try JSONEncoder().encode(self)
        let changes = [kSecValueData as String: data]
        var status = SecItemUpdate(Self.vault as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var query = Self.vault
            query[kSecValueData as String] = data
            query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(query as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Problem("Keychain could not save this connection (\(status)).") }
    }

    static func clear() { SecItemDelete(vault as CFDictionary) }
}

struct Problem: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}
struct Job: Codable, Identifiable, Hashable {
    let id, url, state, stage, created: String
    let started, finished, error: String?
    let ahead: Int?
    let reported: Bool
    let media_seconds: Double?
    var code: String { URL(string: url)?.lastPathComponent ?? "Reel" }
    static func date(_ value: String?) -> Date? {
        guard let value else { return nil }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }
    func timing(at now: Date) -> String {
        guard let created = Self.date(created) else { return stage }
        let start = Self.date(started)
        let end = Self.date(finished)
        let elapsed = max(0, Int((end ?? now).timeIntervalSince(start ?? created)))
        let time = Self.duration(elapsed)
        if state == "queued" { return "\(ahead ?? 0) ahead · \(time) waiting" }
        if state == "running" { return "\(stage) · \(time) elapsed" }
        return "\(time) processing · \(Self.duration(max(0, Int((start ?? created).timeIntervalSince(created))))) queued"
    }
    static func duration(_ seconds: Int) -> String {
        seconds < 60 ? "\(seconds)s" : "\(seconds / 60)m \(seconds % 60)s"
    }
}
struct Account: Codable { let tier: String; let used: Int; let limit: Int? }
struct Challenge: Decodable { let challenge: String }
struct SessionKey: Decodable { let token: String }

private final class NoRedirect: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
struct API {
    let connection: Connection
    static let session = URLSession(configuration: .ephemeral, delegate: NoRedirect(), delegateQueue: nil)
    func request(_ path: String, method: String = "GET", body: [String: String]? = nil) throws -> URLRequest {
        var req = URLRequest(url: connection.base.appending(path: path))
        req.httpMethod = method
        req.timeoutInterval = 30
        if !connection.token.isEmpty { req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization") }
        if let body {
            req.httpBody = try JSONEncoder().encode(body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }
    func data(_ path: String, method: String = "GET", body: [String:String]? = nil) async throws -> Data {
        let (data, response) = try await Self.session.data(for: request(path, method: method, body: body))
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let error = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw Problem(error?["detail"] as? String ?? "The server could not complete the request.")
        }
        return data
    }
    func submit(_ value: String, id: UUID) async throws -> Job {
        try JSONDecoder().decode(Job.self, from: await data("v1/jobs", method: "POST",
                                    body: ["url":value,"request_id":id.uuidString]))
    }
    func movie(_ id: String) async throws -> URL {
        let (tmp, result) = try await Self.session.download(for: request("v1/jobs/\(id)/video"))
        guard let r = result as? HTTPURLResponse, r.statusCode == 200, r.mimeType == "video/mp4"
        else { throw Problem("The video could not be downloaded.") }
        let path = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString + ".mp4")
        try FileManager.default.moveItem(at: tmp, to: path)
        return path
    }
}
