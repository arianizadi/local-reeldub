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
    @State private var result: Job?
    private var success: Bool { result != nil }
    @State private var message: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var validLink: String? { link.flatMap { Job.isReel($0) ? $0 : nil } }

    var body: some View {
        VStack(alignment: .leading, spacing: 19) {
            Capsule().fill(.secondary.opacity(0.4)).frame(width: 36, height: 5).frame(maxWidth: .infinity)
            HStack {
                Label("ReelDub", systemImage: "waveform").font(.headline)
                Spacer()
                if !success { Button("Cancel", action: close).disabled(busy) }
            }
            if let result {
                Image(systemName: "checkmark.circle.fill").font(.largeTitle).foregroundStyle(.green)
                Text(result.state == "done" ? "Your dub is ready" : "Reel queued").font(.title.bold())
                Text(result.state == "done" ? "Open Dubs in ReelDub to watch or save it." : "Follow its progress in ReelDub. You can return to Instagram now.").foregroundStyle(.secondary)
                Button("Done", action: close).buttonStyle(.borderedProminent)
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
                            result = try await API(connection: connection).submit(validLink, id: requestID, target: target)
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
                    Text(account == nil ? (message == nil ? "Checking account…" : "Account unavailable") : busy ? "Queuing…" : (creator ? "Review translation" : "Translate to " + OutputLanguage.name(target))).font(.headline).frame(maxWidth: .infinity, minHeight: 45)
                }.buttonStyle(.borderedProminent).disabled(busy || account == nil)
            } else {
                Text(connection == nil ? "Sign in to ReelDub first" : "Share a Reel link").font(.title.bold())
                Text(connection == nil ? "Open ReelDub and continue with Apple, then share this Reel again." : "Choose Share on an individual Instagram Reel.").foregroundStyle(.secondary)
                Button("Close", action: close)
            }
            if let message { Text(message).font(.footnote).foregroundStyle(.red) }
            Spacer(minLength: 0)
        }.tint(Color(red: 0.73, green: 0.245, blue: 0.13)).padding(24)
            .sheet(item:$project) { input in
                if let connection { QuoteSheet(api:API(connection:connection),input:input,languages:languages,target:target,liveOffers:account?.purchase_preflight == true) { jobs in result=jobs.first } }
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
