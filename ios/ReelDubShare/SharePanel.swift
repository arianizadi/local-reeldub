import SwiftUI

struct SharePanel: View {
    @ObservedObject var attachment: SharedMovieRequest
    let close: () -> Void
    @State private var connection = Connection.read()
    @State private var account: Account?
    @State private var profileAttempt = 0
    @State private var selfHostLocked = false
    @State private var results: [Job] = []
    @State private var message: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Label("ReelDub", systemImage: "waveform").font(.headline)
                    Spacer()
                    if results.isEmpty { Button("Cancel", action: close) }
                }
                if !results.isEmpty {
                    ShareConfirmation(jobs: results, close: close, balanceAPI: connection.map { API(connection: $0) })
                } else if attachment.provider == nil {
                    Text("Share one video").font(.title.bold())
                    Text("Choose a video in Photos or Files, then share it to ReelDub.").foregroundStyle(.secondary)
                    Button("Close", action: close)
                } else if let connection {
                    if selfHostLocked {
                        Text("Self-host unlock needed").font(.title.bold())
                        Text("Open ReelDub to unlock or restore your private-server connection, then share this video again.")
                            .foregroundStyle(.secondary)
                    } else if let account {
                        if account.video_uploads == true, !(account.upload_voice_modes ?? [:]).isEmpty {
                            Text("Translate this video").font(.system(.largeTitle, design: .serif, weight: .semibold))
                            if let name = attachment.provider?.suggestedName, !name.isEmpty {
                                Label((name as NSString).lastPathComponent, systemImage: "film")
                                    .font(.subheadline.weight(.medium)).lineLimit(2)
                            }
                            Text("Upload to " + (connection.isHosted ? "ReelDub Cloud" : (connection.base.host() ?? "your server")) + " to choose a language and voice.")
                                .foregroundStyle(.secondary)
                            if attachment.loading {
                                ProgressView("Preparing video…")
                            } else {
                                Button(action: attachment.prepare) {
                                    Text("Upload and review").font(.headline).frame(maxWidth: .infinity, minHeight: 45)
                                }
                                .buttonStyle(.borderedProminent).foregroundStyle(Palette.onAction)
                            }
                            if let issue = attachment.error { Text(issue).font(.footnote).foregroundStyle(.red) }
                        } else {
                            Text("Video uploads unavailable").font(.title.bold())
                            Text("This server needs support for video uploads and voice choices.").foregroundStyle(.secondary)
                        }
                    } else if let message {
                        Text(message).foregroundStyle(.secondary)
                        Button("Check account again") { profileAttempt += 1 }
                    } else {
                        ProgressView("Checking account…")
                    }
                } else {
                    Text("Sign in to ReelDub first").font(.title.bold())
                    Text(message ?? "Open ReelDub to sign in or connect your own server, then share this video again.")
                        .foregroundStyle(.secondary)
                    Button("Close", action: close)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(24)
        }
        .tint(Palette.action)
        .sheet(item: $attachment.project, onDismiss: attachment.discard) { input in
            if let connection, let account {
                QuoteSheet(api: API(connection: connection), input: input, voiceModes: account.upload_voice_modes ?? [:],
                           kittenLanguages: account.kitten_narrator_languages ?? [],
                           target: OutputLanguage.read(), creatorMinutes: account.creator_minutes == true,
                           liveOffers: account.purchase_preflight == true) { jobs in
                    results = jobs
                }
            }
        }
        .task(id: profileAttempt) {
            guard attachment.provider != nil, let connection else { return }
            if !connection.isHosted, !(await SelfHostEntitlement.current()) {
                selfHostLocked = true
                return
            }
            message = nil
            do {
                let data = try await API(connection: connection).data("v1/me")
                account = try JSONDecoder().decode(Account.self, from: data)
            } catch is SessionError {
                self.connection = nil
                message = "Your session ended. Open ReelDub to sign in again."
            } catch {
                if !Task.isCancelled { message = "Couldn’t check your account. Try again when connected." }
            }
        }
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
