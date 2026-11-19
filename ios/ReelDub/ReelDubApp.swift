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
        }
    }
}

@Observable @MainActor
final class Studio {
    var connection = Connection.read()
    var jobs = [Job]()
    var account: Account?
    var failure: String?
    var welcomeMessage: String?
    var sending = false
    private var pending: (String, UUID)?
    var api: API? { connection.map { API(connection: $0) } }

    func refresh() async {
        guard let api else { return }
        do {
            async let j = api.data("v1/jobs")
            async let a = api.data("v1/me")
            let (payload, me) = try await (j, a)
            guard connection?.base == api.connection.base, connection?.token == api.connection.token else { return }
            jobs = try JSONDecoder().decode([Job].self, from: payload)
            account = try JSONDecoder().decode(Account.self, from: me)
            failure = nil
        } catch is SessionError {
            guard connection?.token == api.connection.token, connection?.base == api.connection.base else { return }
            clearSession()
            welcomeMessage = "Your session ended. Sign in again to see your dubs."
        } catch {
            if !Task.isCancelled { failure = error.localizedDescription }
        }
    }
    func connect(_ new: Connection) async throws {
        let me = try await API(connection: new).data("v1/me")
        let profile = try JSONDecoder().decode(Account.self, from: me)
        try new.save()
        jobs = []; pending = nil; connection = new; account = profile; welcomeMessage = nil
        await refresh()
    }
    func disconnect() async {
        let previous = api
        clearSession()
        if let previous { _ = try? await previous.data("v1/session", method: "DELETE") }
    }
    private func clearSession() {
        Connection.clear(); connection = nil; jobs = []; account = nil; failure = nil; pending = nil
    }
    func submit(_ url: String) async throws {
        guard let api, !sending else { throw Problem("Connect to a server first.") }
        sending = true
        defer { sending = false }
        if pending?.0 != url { pending = (url, UUID()) }
        let job = try await api.submit(url, id: pending!.1)
        pending = nil
        jobs.removeAll { $0.id == job.id }; jobs.insert(job, at: 0)
        await refresh()
    }
}
