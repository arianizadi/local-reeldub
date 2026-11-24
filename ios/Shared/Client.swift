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
    var isHosted: Bool {
        let cloud = Self.hosted
        guard base.scheme?.lowercased() == "https", (base.port ?? 443) == (cloud.port ?? 443),
              let hostname = base.host()?.lowercased(), let cloudName = cloud.host()?.lowercased() else { return false }
        return hostname == cloudName || hostname == cloudName + "."
    }
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
    var voice_mode: String? = nil
    var narrator_engine: String? = nil
    var match_engine: String? = nil
    var dubVoiceLabel: String? {
        switch voice_mode {
        case "narrator": narrator_engine == VoiceEngine.kittenNarratorID ? "KittenTTS 2 narrator" : "Original narrator"
        case "match": match_engine == VoiceEngine.kittenMatchID ? "Matched speaker · KittenTTS 2" : "Matched speaker · Original"
        default: nil
        }
    }
    var recovery_count: Int? = nil
    var recovery_switched_server: Bool? = nil
    var recoveryCue: String? {
        if state == "queued", stage.caseInsensitiveCompare("Waiting for a render server") == .orderedSame {
            return "Waiting for a render server"
        }
        guard (recovery_count ?? 0) > 0 else {
            return state == "queued" && stage.caseInsensitiveCompare("Recovering render") == .orderedSame ? "Recovering render" : nil
        }
        switch state {
        case "queued": return "Recovering render"
        case "running": return recovery_switched_server == true ? "Continuing on another server" : "Rendering again"
        default: return nil
        }
    }
    var recoveryNotice: String? {
        if state == "queued", (recovery_count ?? 0) > 0 {
            let priority = ahead == 0 ? "Your dub is at the front of the recovery queue." : "Your dub has priority over new requests."
            return "Sorry, your render was interrupted. " + priority + " We’ll retry when a render server is available."
        }
        if state == "running", (recovery_count ?? 0) > 0 {
            return recovery_switched_server == true ? "Your dub is continuing on another server after an interruption." : "Your dub is rendering again after an interruption."
        }
        if state == "done", (recovery_count ?? 0) > 0 { return "Your dub finished after recovering from an interruption." }
        return nil
    }
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
    var lab_mode: String? = nil
    var render_quality: String? = nil
    var lab_runtime: String? = nil
    var voice_batch: Int? = nil
    var target_language: String? = nil
    var target: String { target_language ?? "en" }
    var targetName: String { OutputLanguage.name(target) }
    var activity: String { last_requested ?? created }
    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        if let author, !author.isEmpty { return "Reel by " + author }
        return (Self.isReel(url) ? "Reel" : "Video") + (Self.date(created).map { " · " + $0.formatted(.dateTime.month(.abbreviated).day().hour().minute()) } ?? "")
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
        if state == "queued" {
            let priority = (recovery_count ?? 0) > 0 ? "Recovery priority · " : (lane == "premium" ? "Priority · " : "")
            let position = ahead.map { "\($0) ahead · " } ?? ""
            return priority + position + time + " waiting"
        }
        if state == "running" { return "\(stage) · \(time) elapsed" }
        return "\(time) processing · \(Self.duration(max(0, Int((start ?? created).timeIntervalSince(created))))) queued"
    }
    static func duration(_ seconds: Int) -> String {
        seconds < 60 ? "\(seconds)s" : "\(seconds / 60)m \(seconds % 60)s"
    }
}
struct Account: Codable {
    var dailyRemaining: String { limit.map { String(max(0, $0 - used)) } ?? "Unlimited" }
    static func minuteBalance(_ seconds: Int?) -> String {
        let seconds = max(0, seconds ?? 0)
        if seconds % 60 == 0 { return "\(seconds / 60) min" }
        return Duration.seconds(seconds).formatted(.units(allowed: [.minutes, .seconds], width: .abbreviated))
    }
    static var dailyResetNotice: String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let reset = calendar.startOfDay(for: Date()).addingTimeInterval(86_400)
        return "Daily dubs reset at " + reset.formatted(date: .omitted, time: .shortened) + " local time."
    }

    let tier: String
    let used: Int
    let limit: Int?
    let email, library_id: String?
    let output_languages: [String]?
    var lab_runtimes: [String]? = nil
    var admin_console: Bool? = nil
    var video_uploads: Bool? = nil
    var upload_voice_modes: [String: [String]]? = nil
    var kitten_narrator_languages: [String]? = nil
    var kitten_match_languages: [String]? = nil
    var creator_minutes: Bool? = nil
    var batch_progress: Bool? = nil
    var video_suggestions: Bool? = nil
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
    let adminGrant: String?
    let adminExpired: (@MainActor @Sendable () -> Void)?
    let selfHostAuthorized: () -> Bool
    init(connection: Connection, transport: URLSession = API.session, selfHostAuthorized: @escaping () -> Bool = { SelfHostEntitlement.isCached }, adminGrant: String? = nil,
         adminExpired: (@MainActor @Sendable () -> Void)? = nil) {
        self.adminExpired = adminExpired
        self.adminGrant = adminGrant
        self.connection = connection
        self.transport = transport
        self.selfHostAuthorized = selfHostAuthorized
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
        // A free compatibility test can only read the profile. Every other private
        // server request, including job submission from the extension, needs the unlock.
        if !connection.isHosted && !(path == "v1/me" && method == "GET" && body == nil) && !selfHostAuthorized() {
            throw Problem("Open ReelDub to unlock or restore your self-hosted connection.")
        }
        var req = URLRequest(url: base.appending(path: path))
        req.httpMethod = method
        if path.hasPrefix("v1/admin/") {
            guard connection.isHosted else { throw Problem("Administrator access requires ReelDub Cloud.") }
            if let adminGrant { req.setValue(adminGrant, forHTTPHeaderField: "X-ReelDub-Admin") }
        }
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
    func removeLibraryEntry(_ id: String, revision: String) async throws {
        var req = try request("v1/jobs/" + id, method: "DELETE")
        req.setValue(revision, forHTTPHeaderField: "X-ReelDub-Library-Revision")
        _ = try await response(for: req)
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
            if (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "X-ReelDub-Reason") == "admin_verification_required",
               let adminExpired { Task { @MainActor in adminExpired() } }
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

enum RDLink {
    /// Accepts a bare Reel URL or text containing one (e.g. a share caption).
    static func reel(in text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if Job.isReel(trimmed) { return trimmed }
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return nil }
        let range = NSRange(trimmed.startIndex..., in: trimmed)
        for match in detector.matches(in: trimmed, range: range) {
            if let url = match.url?.absoluteString, Job.isReel(url) { return url }
        }
        return nil
    }
}
