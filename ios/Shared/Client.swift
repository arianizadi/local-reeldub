import Foundation
import Security

struct Connection: Codable {
    let base: URL
    let token: String
    var libraryID: String? = nil
    var outputLanguages: [String]? = nil

    static var hosted: URL {
        guard let value = Bundle.main.object(forInfoDictionaryKey: "ReelDubHostedURL") as? String,
              let url = try? server(value), url.scheme == "https" else {
            preconditionFailure("ReelDubHostedURL must be an HTTPS origin")
        }
        return url
    }
    var isHosted: Bool { base.scheme?.lowercased() == "https" && base.host()?.lowercased() == Self.hosted.host()?.lowercased() && base.port == Self.hosted.port }
    static func server(_ text: String) throws -> URL {
        guard let parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil, parts.query == nil,
              parts.fragment == nil, parts.path == "" || parts.path == "/",
              let url = parts.url else { throw Problem("Enter an HTTPS server address, without a path or credentials.") }
        if parts.scheme?.lowercased() == "https" { return url }
        #if DEBUG && targetEnvironment(simulator)
        if parts.scheme?.lowercased() == "http", ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host.lowercased()) { return url }
        #endif
        throw Problem("ReelDub needs an HTTPS server address to protect your connection.")
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
        query[kSecReturnAttributes as String] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let attributes = result as? [String: Any],
              let data = attributes[kSecValueData as String] as? Data else { return nil }
        if attributes[kSecAttrAccessible as String] as? String != kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String {
            let update = [kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
            guard SecItemUpdate(vault as CFDictionary, update as CFDictionary) == errSecSuccess else { return nil }
        }
        return try? JSONDecoder().decode(Self.self, from: data)
    }

    func save() throws {
        let data = try JSONEncoder().encode(self)
        let changes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var status = SecItemUpdate(Self.vault as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var query = Self.vault
            query[kSecValueData as String] = data
            query[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            status = SecItemAdd(query as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Problem("Keychain could not save this connection (\(status)).") }
    }

    static func clear() { SecItemDelete(vault as CFDictionary) }
}

enum SessionError: LocalizedError {
    case expired
    var errorDescription: String? { "Your session ended. Sign in again." }
}

struct ServiceError: LocalizedError {
    let status: Int
    let errorDescription: String?
    var reason: String? = nil
    var needsBalance: Bool { status == 402 || reason == "daily_allowance_used" }
}

struct Problem: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}
struct Job: Codable, Identifiable, Hashable {
    let id, url, state, stage, created: String
    let started, finished, error: String?
    let ahead: Int?
    var progress: Double? = nil
    var estimated_finish: String? = nil
    func remaining(at date: Date) -> String {
        guard let finish = Self.date(estimated_finish) else { return "Estimating time remaining…" }
        let seconds = Int(ceil(finish.timeIntervalSince(date)))
        guard seconds > 0 else { return "Updating estimate…" }
        if seconds < 10 { return "About 10s left" }
        return "About " + Self.duration(Int(ceil(Double(seconds) / 5)) * 5) + " left"
    }
    var lane: String? = nil
    let reported: Bool
    let media_seconds: Double?
    let title, author: String?
    let has_poster: Bool?
    var available_on_server: Bool? = nil
    var cache_expires: String? = nil
    var last_requested: String? = nil
    var source_id: String? = nil
    var trimmed: Bool? = nil
    var target_language: String? = nil
    var target: String { target_language ?? "en" }
    var targetName: String { OutputLanguage.name(target) }
    var activity: String { last_requested ?? created }
    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        if let author, !author.isEmpty { return "Reel by " + author }
        return "Reel · " + (Self.date(created)?.formatted(.dateTime.month(.abbreviated).day().hour().minute()) ?? "Instagram")
    }
    static func isReel(_ value: String) -> Bool {
        value.range(of: #"(?i)^https://(?:(?:www|m)\.)?instagram\.com/reels?/[A-Za-z0-9_-]{5,32}/?(?:[?#][^\s]*)?$"#, options: .regularExpression) != nil
    }
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
        if state == "queued" { return (lane == "premium" ? "Priority · " : "") + "\(ahead ?? 0) ahead · \(time) waiting" }
        if state == "running" { return "\(stage) · \(time) elapsed" }
        return "\(time) processing · \(Self.duration(max(0, Int((start ?? created).timeIntervalSince(created))))) queued"
    }
    static func duration(_ seconds: Int) -> String {
        seconds < 60 ? "\(seconds)s" : "\(seconds / 60)m \(seconds % 60)s"
    }
}
struct Account: Codable {
    let tier: String
    let used: Int
    let limit: Int?
    let email, library_id: String?
    let output_languages: [String]?
    var creator_minutes: Bool? = nil
    var batch_progress: Bool? = nil
    var monthly_seconds: Int? = nil
    var pack_seconds: Int? = nil
    let store_token: UUID?
    let extra_translations, queue_skips: Int?
    let purchase_preflight: Bool?
}
struct Challenge: Decodable { let challenge: String }
struct SessionKey: Decodable { let token: String }

private final class NoRedirect: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
struct API {
    let connection: Connection
    let transport: URLSession
    init(connection: Connection, transport: URLSession = API.session) {
        self.connection = connection
        self.transport = transport
    }
    static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.tlsMinimumSupportedProtocolVersion = .TLSv12
        config.urlCache = nil
        config.urlCredentialStorage = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        return URLSession(configuration: config, delegate: NoRedirect(), delegateQueue: nil)
    }()
    func request(_ path: String, method: String = "GET", body: [String: String]? = nil) throws -> URLRequest {
        // Restored Keychain connections also pass this check before sending credentials.
        let base = try Connection.server(connection.base.absoluteString)
        var req = URLRequest(url: base.appending(path: path))
        req.httpMethod = method
        if path == "v1/me" { req.setValue("1", forHTTPHeaderField: "X-ReelDub-Checkout") }
        req.timeoutInterval = 30
        if !connection.token.isEmpty { req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization") }
        if let body {
            req.httpBody = try JSONEncoder().encode(body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }
    func data(_ path: String, method: String = "GET", body: [String:String]? = nil) async throws -> Data {
        try await response(for: request(path, method: method, body: body))
    }
    func post<T: Encodable>(_ path: String, _ value: T) async throws -> Data {
        var req = try request(path, method: "POST")
        req.httpBody = try JSONEncoder().encode(value)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return try await response(for: req)
    }
    private func response(for req: URLRequest) async throws -> Data {
        let (data, response) = try await transport.data(for: req)
        return try checked(data, response)
    }
    func checked(_ data: Data, _ response: URLResponse) throws -> Data {
        if (response as? HTTPURLResponse)?.statusCode == 401 && !connection.token.isEmpty { throw SessionError.expired }
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let error = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw ServiceError(status: (response as? HTTPURLResponse)?.statusCode ?? 0, errorDescription: error?["detail"] as? String ?? "The server could not complete the request.",
                               reason: (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "X-ReelDub-Reason"))
        }
        return data
    }
    func submit(_ value: String, id: UUID, target: String = "en") async throws -> Job {
        var body = ["url": value, "request_id": id.uuidString]
        if target != "en" { body["target_language"] = target }
        return try JSONDecoder().decode(Job.self, from: await data("v1/jobs", method: "POST", body: body))
    }
    func playback(_ id: String) async throws -> URL {
        struct Ticket: Decodable { let ticket: String }
        let payload: Data
        do { payload = try await data("v1/jobs/\(id)/playback", method: "POST") }
        catch let error as ServiceError where [404, 405].contains(error.status) { return try await movie(id) }
        let grant = try JSONDecoder().decode(Ticket.self, from: payload)
        return connection.base.appending(path: "v1/play/\(id)/\(grant.ticket)")
    }
    func movie(_ id: String) async throws -> URL {
        let (tmp, result) = try await transport.download(for: request("v1/jobs/\(id)/video"))
        if let http = result as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            _ = try checked(Data(), http)
        }
        guard let r = result as? HTTPURLResponse, r.statusCode == 200, r.mimeType == "video/mp4"
        else { throw Problem("The video could not be downloaded.") }
        let length = try tmp.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard length > 0, r.expectedContentLength < 0 || Int64(length) == r.expectedContentLength else {
            throw Problem("The video download was incomplete.")
        }
        let path = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString + ".mp4")
        #if os(iOS) && !targetEnvironment(macCatalyst)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: tmp.path)
        #endif
        try FileManager.default.moveItem(at: tmp, to: path)
        return path
    }
}
