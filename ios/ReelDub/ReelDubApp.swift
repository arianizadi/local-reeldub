import SwiftUI

@main
struct ReelDubApp: App {
    @State private var studio = Studio()
    var body: some Scene {
        WindowGroup {
            Group {
                if studio.connection == nil { Welcome(studio: studio) }
                else { Home(studio: studio) }
            }.tint(Palette.ember)
                .task(id: studio.account?.store_token) { await studio.observePurchases() }
        }
    }
}

@Observable @MainActor
final class Studio {
    var connection: Connection?
    private var library: DeviceLibrary?
    private(set) var saved: [String: Job] = [:]
    private(set) var transferring: String?
    private(set) var transferErrors: [String: String] = [:]
    private var retryAfter: [String: Date] = [:]
    private var transferTask: Task<Void, Never>?
    private var deleting: Set<String> = []
    private var refreshGeneration = 0
    private var fullRefreshes = 0

    init(connection: Connection? = Connection.read()) {
        self.connection = connection
        restoreLibrary()
    }

    private func restoreLibrary() {
        library = connection.flatMap(DeviceLibrary.init)
        saved = library?.inventory() ?? [:]
        jobs = saved.values.sorted { $0.activity > $1.activity }
    }
    private func matches(_ api: API) -> Bool {
        connection?.base == api.connection.base && connection?.token == api.connection.token
    }
    func localMovie(_ id: String) -> URL? { saved[id] == nil ? nil : library?.movie(id) }

    func isExpired(_ job: Job) -> Bool {
        job.state == "done" && job.available_on_server == false && saved[job.id] == nil
    }

    func storageLabel(_ job: Job) -> String? {
        guard job.state == "done" else { return nil }
        if saved[job.id] != nil { return "Saved on this device" }
        if job.available_on_server == false { return "Server copy expired" }
        if library?.isManualDownload(job.id) == true { return "Removed from this device" }
        if transferErrors[job.id] != nil { return "Download interrupted · will retry" }
        if transferring == job.id { return "Saving to this device…" }
        return nil
    }

    func shareMovie(_ id: String) async throws -> URL {
        if saved[id] != nil, let library { return try library.export(id) }
        guard let api else { throw Problem("Connect to a server first.") }
        return try await api.movie(id)
    }
    var jobs = [Job]()
    var account: Account?
    var failure: String?
    var welcomeMessage: String?
    var sending = false
    private var libraryRevision = 0
    private var pending: (url: String, target: String, id: UUID)?
    var api: API? { connection.map { API(connection: $0) } }

    func refresh(progressOnly: Bool = false) async {
        guard let api else { return }
        if progressOnly && fullRefreshes > 0 { return }
        if !progressOnly { fullRefreshes += 1 }
        defer { if !progressOnly { fullRefreshes -= 1 } }
        refreshGeneration += 1
        let generation = refreshGeneration
        var revision = libraryRevision
        func current() -> Bool { matches(api) && revision == libraryRevision && generation == refreshGeneration }
        do {
            let payload = try await api.data("v1/jobs")
            guard current() else { return }
            let remote = try JSONDecoder().decode([Job].self, from: payload)
            if progressOnly, account != nil, remote.contains(where: { $0.rdPhase.isActive }) {
                mergeJobs(remote)
                failure = nil
                startTransfer()
                return
            }
            let profile = try JSONDecoder().decode(Account.self, from: await api.data("v1/me"))
            guard current() else { return }
            if connection?.libraryID != profile.library_id || connection?.outputLanguages != profile.output_languages {
                let changedLibrary = connection?.libraryID != profile.library_id
                var upgraded = api.connection
                upgraded.libraryID = profile.library_id
                upgraded.outputLanguages = profile.output_languages
                try upgraded.save()
                connection = upgraded
                if changedLibrary {
                    resetTransfers()
                    revision = libraryRevision
                    restoreLibrary()
                }
            }
            let remoteIDs = Set(remote.map(\.id))
            let missing = saved.keys.filter { !remoteIDs.contains($0) }
            // The feed is paged. Older offline copies survive unless the owner explicitly deleted them.
            for start in stride(from: 0, to: missing.count, by: 100) {
                let ids = Array(missing[start ..< min(start + 100, missing.count)])
                let data = try await api.post("v1/library/deletions", ["ids": ids])
                guard current() else { return }
                for id in try JSONDecoder().decode([String].self, from: data) {
                    try library?.remove(id)
                    saved[id] = nil
                    RDPosterStore.shared.remove(id, api: api)
                }
            }
            mergeJobs(remote)
            account = profile
            failure = nil
            startTransfer()
        } catch is SessionError {
            guard current() else { return }
            clearSession()
            welcomeMessage = "Your session ended. Sign in again to see your dubs."
        } catch {
            if current(), !Task.isCancelled { failure = error.localizedDescription }
        }
    }
    private func mergeJobs(_ remote: [Job]) {
        let ids = Set(remote.map(\.id))
        jobs = (remote + saved.values.filter { !ids.contains($0.id) }).sorted { $0.activity > $1.activity }
    }

    var nextTransferRetry: Date? {
        guard transferring == nil else { return nil }
        return jobs.compactMap { $0.available_on_server == true ? retryAfter[$0.id] : nil }.min()
    }

    func retryTransfers() {
        retryAfter = retryAfter.filter { $0.value > Date() }
        startTransfer()
    }
    func connect(_ new: Connection) async throws {
        let me = try await API(connection: new).data("v1/me")
        let profile = try JSONDecoder().decode(Account.self, from: me)
        var verified = new
        verified.libraryID = profile.library_id
        verified.outputLanguages = profile.output_languages
        try verified.save()
        resetTransfers()
        pending = nil; connection = verified; restoreLibrary(); account = profile; welcomeMessage = nil
        await refresh()
    }
    func deleteAccount(identity: String, challenge: String, code: String) async throws {
        guard let api, api.connection.isHosted else { throw Problem("Sign in to ReelDub Cloud first.") }
        let local = library
        struct Receipt: Decodable { let apple_revoked: Bool }
        let response = try await api.data("v1/account", method: "DELETE", body: [
            "identity_token": identity, "challenge": challenge, "authorization_code": code])
        let receipt = try JSONDecoder().decode(Receipt.self, from: response)
        guard matches(api) else { return }
        clearSession()
        welcomeMessage = receipt.apple_revoked ? "Your ReelDub account was deleted." :
            "Your ReelDub account was deleted. To disconnect Apple, open Settings → your name → Sign in with Apple → ReelDub."
        do { try local?.erase() }
        catch { welcomeMessage = "Your Cloud account was deleted. Remove ReelDub from this device to clear the remaining local files." }
    }
    func disconnect() async {
        let previous = api
        clearSession()
        if let previous { _ = try? await previous.data("v1/session", method: "DELETE") }
    }
    private func resetTransfers() {
        libraryRevision += 1
        transferTask?.cancel()
        transferTask = nil; transferring = nil
        deleting = []; transferErrors = [:]; retryAfter = [:]
        saved = [:]; library = nil
    }

    private func clearSession() {
        resetTransfers()
        Connection.clear(); connection = nil; jobs = []; account = nil; failure = nil; pending = nil
    }
    private func startTransfer() {
        guard transferTask == nil, let api, let library,
              let job = jobs.first(where: { job in
                  job.state == "done" && job.available_on_server == true && !deleting.contains(job.id) && !library.isManualDownload(job.id)
                    && (retryAfter[job.id] ?? .distantPast) <= Date()
                    && (saved[job.id] == nil || library.needsReceipt(job.id))
              }) else { return }
        transferring = job.id
        transferTask = Task {
            defer {
                if !Task.isCancelled, matches(api) {
                    transferTask = nil; transferring = nil
                    startTransfer()
                }
            }
            do {
                if saved[job.id] == nil {
                    let temp = try await api.movie(job.id)
                    defer { try? FileManager.default.removeItem(at: temp) }
                    try await DeviceLibrary.validateMovie(temp)
                    try Task.checkCancellation()
                    guard matches(api), !deleting.contains(job.id), !library.isManualDownload(job.id), jobs.contains(where: { $0.id == job.id }) else { return }
                    try library.save(temp, job: job)
                    saved[job.id] = job
                }
                let bytes = try library.movie(job.id).resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                _ = try await api.post("v1/jobs/" + job.id + "/received", ["bytes": bytes])
                try Task.checkCancellation()
                guard matches(api), !deleting.contains(job.id), !library.isManualDownload(job.id) else { return }
                try library.markReceived(job.id)
                transferErrors[job.id] = nil; retryAfter[job.id] = nil
            } catch {
                guard !Task.isCancelled, matches(api) else { return }
                transferErrors[job.id] = error.localizedDescription
                retryAfter[job.id] = Date().addingTimeInterval(60)
            }
        }
    }

    func removeDownload(_ id: String) throws {
        guard let library, saved[id] != nil else { return }
        try library.removeDownload(id)
        libraryRevision += 1
        if transferring == id {
            transferTask?.cancel()
            transferTask = nil
            transferring = nil
        }
        saved[id] = nil
        transferErrors[id] = nil
        retryAfter[id] = nil
        if let api { RDPosterStore.shared.remove(id, api: api) }
    }

    func canDownloadAgain(_ id: String) -> Bool {
        saved[id] == nil && library?.isManualDownload(id) == true
    }

    func downloadAgain(_ id: String) throws {
        guard let library else { throw Problem("Open your Dubs library first.") }
        try library.allowDownload(id)
        startTransfer()
    }

    func deleteDub(_ id: String) async throws {
        guard let api else { throw Problem("Connect to a server first.") }
        deleting.insert(id)
        defer { if matches(api) { deleting.remove(id) } }
        _ = try await api.data("v1/jobs/" + id, method: "DELETE")
        guard matches(api) else { throw Problem("Your connection changed.") }
        libraryRevision += 1
        try library?.remove(id)
        saved[id] = nil; transferErrors[id] = nil; retryAfter[id] = nil
        RDPosterStore.shared.remove(id, api: api)
        jobs.removeAll { $0.id == id }
    }

    @discardableResult func submit(_ url: String, target: String = "en") async throws -> Job {
        guard let api, !sending else { throw Problem("Connect to a server first.") }
        sending = true
        defer { sending = false }
        if pending?.url != url || pending?.target != target { pending = (url, target, UUID()) }
        let job = try await api.submit(url, id: pending!.id, target: target)
        guard matches(api) else { throw Problem("Your connection changed.") }
        pending = nil
        jobs.removeAll { $0.id == job.id }; jobs.insert(job, at: 0)
        await refresh()
        return job
    }
}
