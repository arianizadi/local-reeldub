import SwiftUI

struct SharePanel: View {
    let link: String?
    let close: () -> Void
    @State private var requestID = UUID()
    @State private var connection = Connection.read()
    @State private var busy = false
    @State private var preferredLanguage = OutputLanguage.read()
    @State private var account: Account?
    @State private var profileAttempt = 0
    @State private var showBalance = false
    private var creator: Bool { account?.creator_minutes == true }
    @State private var project: ProjectInput?
    private var languages: [String] { account?.output_languages ?? connection?.outputLanguages ?? ["en"] }
    private var target: String { OutputLanguage.selected(preferredLanguage, supported: languages) }
    @State private var results: [Job] = []
    private var success: Bool { !results.isEmpty }
    @State private var message: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var validLink: String? { link.flatMap { Job.isReel($0) ? $0 : nil } }

    var body: some View {
        ScrollView {
          VStack(alignment: .leading, spacing: 19) {
            HStack {
                Label("ReelDub", systemImage: "waveform").font(.headline)
                Spacer()
                if !success { Button("Cancel", action: close).disabled(busy) }
            }
            if success {
                ShareConfirmation(jobs: results, close: close, balanceAPI: connection.map { API(connection: $0) })
            } else if let validLink, let connection {
                Text("Translate this Reel?").font(.system(.largeTitle, design: .serif, weight: .semibold))
                Text(validLink).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                Label(connection.isHosted ? "ReelDub Cloud" : (connection.base.host() ?? "Your server"), systemImage: "cloud")
                    .font(.subheadline.weight(.medium))
                if languages.count > 1 { OutputLanguagePicker(supported: languages, preference: $preferredLanguage).disabled(busy) }
                if account == nil && message != nil {
                    Button("Check account again") { profileAttempt += 1 }
                }
                if showBalance && connection.isHosted {
                    BalanceOptions(live: account?.purchase_preflight == true)
                }
                Button {
                    guard account != nil, !busy else { return }
                    if creator { project = .reel(validLink); return }
                    Task {
                        busy = true
                        message = nil
                        defer { busy = false }
                        do {
                            results = [try await API(connection: connection).submit(validLink, id: requestID, target: target)]
                        } catch is SessionError {
                            self.connection = nil
                            message = "Your session ended. Sign in again in ReelDub."
                        } catch let error as ServiceError where error.status == 428 {
                            project = .reel(validLink)
                        } catch {
                            showBalance = (error as? ServiceError)?.needsBalance == true
                            message = error.localizedDescription
                        }
                    }
                } label: {
                    Text(account == nil ? (message == nil ? "Checking account…" : "Account unavailable") : busy ? "Queuing…" : (creator ? "Review translation" : "Translate to " + OutputLanguage.name(target))).font(.headline).foregroundStyle(Palette.onAction).frame(maxWidth: .infinity, minHeight: 45)
                }.buttonStyle(.borderedProminent).disabled(busy || account == nil)
            } else {
                Text(connection == nil ? "Sign in to ReelDub first" : "Share a Reel link").font(.title.bold())
                Text(connection == nil ? "Open ReelDub to sign in or connect your own server, then share this Reel again." : "Choose Share on an individual Instagram Reel.").foregroundStyle(.secondary)
                Button("Close", action: close)
            }
            if let message { Text(message).font(.footnote).foregroundStyle(.red) }
            Spacer(minLength: 0)
          }.padding(24)
        }.tint(Palette.action)
            .sheet(item:$project) { input in
                if let connection { QuoteSheet(api:API(connection:connection),input:input,languages:languages,target:target,liveOffers:account?.purchase_preflight == true) { jobs in results=jobs } }
            }
            .onChange(of: target) { _, _ in requestID = UUID() }
            .task(id: profileAttempt) {
                guard let connection else { return }
                message = nil
                do {
                    let data = try await API(connection: connection).data("v1/me")
                    account = try JSONDecoder().decode(Account.self, from: data)
                } catch is SessionError {
                    self.connection = nil
                    message = "Your session ended. Sign in again in ReelDub."
                } catch {
                    if !Task.isCancelled { message = "Couldn’t check your account. Try again when connected." }
                }
            }
            .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: success)
    }
}

struct ShareConfirmation: View {
    let jobs: [Job]
    let close: () -> Void
    var balanceAPI: API? = nil
    @State private var balance: Account?
    @State private var loading = false
    @State private var balanceIssue: String?
    var body: some View {
        Image(systemName: "checkmark.circle.fill").font(.largeTitle).foregroundStyle(.green).accessibilityHidden(true)
        Text(jobs.count == 1 ? "Dub confirmed" : "\(jobs.count) dubs confirmed").font(.title.bold())
        ForEach(jobs) { job in
            DubValueRow(job.targetName, value: status(of: job))
        }
        Text("Open Dubs in ReelDub to watch or check progress.").foregroundStyle(.secondary)
        if let balance {
            DubValueRow("Daily dubs left", value: balance.dailyRemaining)
            if balance.creator_minutes == true, balance.tier != "admin" {
                DubValueRow("Monthly time", value: Account.minuteBalance(balance.monthly_seconds))
                DubValueRow("Purchased time", value: Account.minuteBalance(balance.pack_seconds))
            }
        } else if let balanceAPI {
            Button(loading ? "Checking balance…" : "Check remaining balance") {
                Task {
                    guard !loading else { return }
                    loading = true
                    defer { loading = false }
                    do {
                        let data = try await balanceAPI.data("v1/me")
                        balance = try JSONDecoder().decode(Account.self, from: data)
                        balanceIssue = nil
                    } catch { balanceIssue = error.localizedDescription }
                }
            }.disabled(loading)
            if let balanceIssue { Text(balanceIssue).font(.footnote).foregroundStyle(.secondary) }
        }
        Button(action: close) {
            Text("Done").foregroundStyle(Palette.onAction).frame(maxWidth: .infinity, minHeight: 44)
        }.buttonStyle(.borderedProminent)
    }

    private func status(of job: Job) -> String {
        switch job.state {
        case "done": return "Ready"
        case "queued": return "Queued"
        case "running": return "Processing"
        case "failed": return "Failed"
        default: return job.stage
        }
    }
}
