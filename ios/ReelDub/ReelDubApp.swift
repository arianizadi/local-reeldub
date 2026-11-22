import SwiftUI

@main
struct ReelDubApp: App {
    @State private var studio = Studio()
    var body: some Scene {
        WindowGroup {
            Group {
                if studio.connection == nil { Welcome(studio: studio) }
                else { Home(studio: studio) }
            }.tint(Palette.action)
                .task(id: studio.account?.store_token) { await studio.observePurchases() }
        }
    }
}

enum TransferRetry: Equatable {
    case waiting(until: Date, seconds: Int)
    case stopped(lastRequest: String?)

    var deadline: Date? {
        if case let .waiting(until, _) = self { return until }
        return nil
    }

    func permits(_ job: Job, at date: Date) -> Bool {
        switch self {
        case let .waiting(until, _): return date >= until
        case let .stopped(request): return job.last_requested != request
        }
    }

    static func next(for job: Job, error: Error, previous: Self?, at date: Date) -> Self {
        if let error = error as? ServiceError, [404, 409, 410].contains(error.status) {
            return .stopped(lastRequest: job.last_requested)
        }
        var seconds = 60
        if case let .waiting(_, last) = previous { seconds = min(last * 2, 3600) }
        return .waiting(until: date.addingTimeInterval(Double(seconds)), seconds: seconds)
    }
}

@Observable @MainActor
final class Studio {
    var connection: Connection?
    private var library: DeviceLibrary?
    private(set) var saved: [String: Job] = [:]
    private var retainedJobs: [String: Job] = [:]
    private var unrecordedMarkers = Set<String>()
    private(set) var transferring: String?
    private(set) var transferErrors: [String: String] = [:]
    private var transferRetries: [String: TransferRetry] = [:]
    private var transferTask: Task<Void, Never>?
    private var deleting: Set<String> = []
    private var refreshGeneration = 0
    private var fullRefreshes = 0
    @ObservationIgnored var unfinishedPurchases: AsyncStream<Void>.Continuation?
    private let transport: URLSession

    init(connection: Connection? = Connection.read(), transport: URLSession = API.session) {
        self.connection = connection
        self.transport = transport
        restoreLibrary()
    }

    private func restoreLibrary() {
        library = connection.flatMap(DeviceLibrary.init)
        saved = library?.inventory() ?? [:]
        retainedJobs = library?.retainedJobs() ?? [:]
        unrecordedMarkers = library?.unrecordedDownloadMarkers() ?? []
        mergeJobs([])
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
        if case .stopped = transferRetries[job.id] { return "Download unavailable" }
        if transferring == job.id { return "Saving to this device…" }
        if transferErrors[job.id] != nil { return "Download interrupted · will retry" }
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
    var api: API? { connection.map { API(connection: $0, transport: transport) } }

    func progressDelay(failures: Int) -> Int? {
        guard jobs.contains(where: { $0.rdPhase.isActive }) else { return nil }
        if failures > 0 { return failures == 1 ? 15 : (failures == 2 ? 30 : 60) }
        return jobs.contains(where: { $0.rdPhase == .running }) ? 5 : 10
    }

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
            if progressOnly, account?.batch_progress == true {
                let active = jobs.filter { $0.rdPhase.isActive }.map(\.id)
                guard !active.isEmpty else { return }
                if active.count <= 100 {
                    let data = try await api.post("v1/library/progress", ["ids": active])
                    guard current() else { return }
                    let updates = try JSONDecoder().decode([Job].self, from: data)
                    if applyActiveProgress(updates, expected: Set(active)) {
                        failure = nil
                        return
                    }
                }
                // A completion or deletion needs a full reconciliation, including the balance and downloads.
            }
            let legacyProgress = progressOnly && account != nil && account?.batch_progress != true
            async let concurrentProfile: Data? = legacyProgress ? nil : api.data("v1/me")
            let payload: Data
            do { payload = try await api.data("v1/jobs") }
            catch let denial as ServiceError where denial.status == 403 {
                let data: Data
                if let profile = try await concurrentProfile { data = profile }
                else { data = try await api.data("v1/me") }
                let profile = try JSONDecoder().decode(Account.self, from: data)
                if current(), profile.tier == "suspended" { account = profile }
                throw denial
            }
            guard current() else { return }
            let remote = try JSONDecoder().decode([Job].self, from: payload)
            if legacyProgress, remote.contains(where: { $0.rdPhase.isActive }) {
                mergeJobs(remote)
                failure = nil
                startTransfer()
                return
            }
            let profileData: Data
            if let prefetched = try await concurrentProfile { profileData = prefetched }
            else { profileData = try await api.data("v1/me") }
            let profile = try JSONDecoder().decode(Account.self, from: profileData)
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
            let missing = Array(Set(saved.keys).union(retainedJobs.keys).subtracting(remoteIDs))
            // The feed contains at most 100 jobs. Only remove an older local item after the owner’s server confirms deletion.
            for start in stride(from: 0, to: missing.count, by: 100) {
                let ids = Array(missing[start ..< min(start + 100, missing.count)])
                do {
                    let data = try await api.post("v1/library/deletions", ["ids": ids])
                    guard current(), !Task.isCancelled else { return }
                    for id in try JSONDecoder().decode([String].self, from: data) where ids.contains(id) {
                        try library?.remove(id)
                        saved[id] = nil
                        retainedJobs[id] = nil
                        RDPosterStore.shared.remove(id, api: api)
                    }
                } catch is SessionError {
                    throw SessionError.expired
                } catch let denial as ServiceError where denial.status == 403 {
                    throw denial
                } catch {
                    // An older or temporarily unreachable cleanup endpoint can be retried on the next full refresh.
                    break
                }
            }
            guard current(), !Task.isCancelled else { return }
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
    func applyActiveProgress(_ updates: [Job], expected: Set<String>) -> Bool {
        guard updates.allSatisfy({ $0.rdPhase.isActive }), Set(updates.map(\.id)) == expected else { return false }
        let latest = Dictionary(updates.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        jobs = jobs.map { latest[$0.id] ?? $0 }
        return true
    }
    private func mergeJobs(_ remote: [Job]) {
        if !unrecordedMarkers.isEmpty, !remote.isEmpty, let library {
            for job in library.recoverReferences(from: remote, pending: &unrecordedMarkers) {
                retainedJobs[job.id] = job
            }
        }
        var latest = retainedJobs
        latest.merge(saved) { _, saved in saved }
        for job in remote { latest[job.id] = job }
        jobs = latest.values.sorted { $0.activity == $1.activity ? $0.id > $1.id : $0.activity > $1.activity }
    }

    var nextTransferRetry: Date? {
        guard transferring == nil else { return nil }
        return jobs.compactMap { $0.available_on_server == true ? transferRetries[$0.id]?.deadline : nil }.min()
    }

    func retryTransfers() {
        startTransfer()
    }
    func connect(_ new: Connection) async throws {
        let me = try await API(connection: new, transport: transport).data("v1/me")
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
        deleting = []; transferErrors = [:]; transferRetries = [:]
        saved = [:]; retainedJobs = [:]; unrecordedMarkers = []; library = nil
    }

    private func clearSession() {
        resetTransfers()
        unfinishedPurchases = nil
        Connection.clear(); connection = nil; jobs = []; account = nil; failure = nil; pending = nil
    }
    private func startTransfer() {
        guard transferTask == nil, let api, let library,
              let job = jobs.first(where: { job in
                  job.state == "done" && job.available_on_server == true && !deleting.contains(job.id) && !library.isManualDownload(job.id)
                    && (transferRetries[job.id]?.permits(job, at: Date()) ?? true)
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
                    retainedJobs[job.id] = nil
                }
                let bytes = try library.movie(job.id).resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                _ = try await api.post("v1/jobs/" + job.id + "/received", ["bytes": bytes])
                try Task.checkCancellation()
                guard matches(api), !deleting.contains(job.id), !library.isManualDownload(job.id) else { return }
                try library.markReceived(job.id)
                transferErrors[job.id] = nil; transferRetries[job.id] = nil
            } catch is SessionError {
                guard !Task.isCancelled, matches(api) else { return }
                clearSession()
                welcomeMessage = "Your session ended. Sign in again to see your dubs."
            } catch {
                guard !Task.isCancelled, matches(api) else { return }
                if let error = error as? ServiceError, [404, 410].contains(error.status),
                   saved[job.id] == nil, let index = jobs.firstIndex(where: { $0.id == job.id }) {
                    jobs[index].available_on_server = false
                    if retainedJobs[job.id] != nil {
                        retainedJobs[job.id] = jobs[index]
                        try? library.retain(jobs[index])
                    }
                }
                transferErrors[job.id] = error.localizedDescription
                let retry = TransferRetry.next(for: job, error: error, previous: transferRetries[job.id], at: Date())
                transferRetries[job.id] = retry
                if case .stopped = retry { await refresh() }
            }
        }
    }

    func removeDownload(_ id: String) throws {
        guard let library, let job = saved[id] else { return }
        try library.removeDownload(job)
        retainedJobs[id] = job
        libraryRevision += 1
        if transferring == id {
            transferTask?.cancel()
            transferTask = nil
            transferring = nil
        }
        saved[id] = nil
        transferErrors[id] = nil
        transferRetries[id] = nil
        if let api { RDPosterStore.shared.remove(id, api: api) }
        startTransfer()
    }

    func canDownloadAgain(_ id: String) -> Bool {
        guard saved[id] == nil, jobs.first(where: { $0.id == id })?.available_on_server != false else { return false }
        if case .stopped = transferRetries[id] { return true }
        return library?.isManualDownload(id) == true
    }

    func downloadAgain(_ id: String) throws {
        guard let library else { throw Problem("Open your Dubs library first.") }
        guard canDownloadAgain(id) else { throw Problem("This dub is unavailable to download. Refresh your library to check again.") }
        try library.allowDownload(id)
        transferRetries[id] = nil
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
        saved[id] = nil; retainedJobs[id] = nil; transferErrors[id] = nil; transferRetries[id] = nil
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
