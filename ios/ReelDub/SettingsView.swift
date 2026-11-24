import StoreKit
import SwiftUI

struct VideoSuggestion: Decodable, Equatable {
    let topic: String
    let note: String
    static let choices = [
        ("longer", "Longer videos"), ("other_apps", "Videos from other apps or websites"),
        ("my_videos", "Videos I record"), ("other", "Something else")
    ]
    var topicName: String { Self.choices.first { $0.0 == topic }?.1 ?? topic }
}

struct VideoSuggestionForm: View {
    let api: API
    @State private var topic = ""
    @State private var note = ""
    @State private var previous: VideoSuggestion?
    @State private var loaded = false
    @State private var sending = false
    @State private var received = false
    @State private var issue: String?

    private var draft: VideoSuggestion {
        VideoSuggestion(topic: topic, note: note.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    var body: some View {
        Form {
            if loaded {
                Section("What would you most like to translate?") {
                    ForEach(VideoSuggestion.choices, id: \.0) { code, title in
                        Button {
                            topic = code
                        } label: {
                            HStack {
                                Text(title).foregroundStyle(.primary)
                                Spacer()
                                if topic == code { Image(systemName: "checkmark").accessibilityHidden(true) }
                            }
                        }.accessibilityAddTraits(topic == code ? .isSelected : [])
                    }
                }
                Section {
                    TextField(topic == "other" ? "What kind of video?" : "An example (optional)", text: $note, axis: .vertical)
                        .lineLimit(2...5)
                    if note.unicodeScalars.count > 400 { Text("Please use 400 characters or fewer.").foregroundStyle(.red) }
                } footer: {
                    Text("Your answer goes to the ReelDub team with your Cloud account. You can update it here. Please leave out personal details.")
                }
                Button(sending ? "Sending…" : previous == nil ? "Send suggestion" : "Update suggestion") {
                    Task { await send() }
                }.disabled(sending || topic.isEmpty || (topic == "other" && draft.note.isEmpty) || note.unicodeScalars.count > 400 || draft == previous)
                if received && draft == previous {
                    Label("Thanks, your suggestion was saved.", systemImage: "checkmark.circle")
                }
            } else if issue == nil {
                ProgressView("Loading suggestion…")
            }
            if let issue {
                Text(issue).foregroundStyle(.red)
                if !loaded { Button("Try again") { Task { await load() } } }
            }
        }
        .disabled(sending)
        .navigationTitle("Video suggestions").navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        guard !loaded else { return }
        issue = nil
        struct Response: Decodable { let suggestion: VideoSuggestion? }
        do {
            let data = try await api.data("v1/me/video-suggestion")
            let saved = try JSONDecoder().decode(Response.self, from: data).suggestion
            previous = saved
            topic = saved?.topic ?? ""
            note = saved?.note ?? ""
            loaded = true
        } catch { if !Task.isCancelled { issue = error.localizedDescription } }
    }

    private func send() async {
        guard loaded, !sending else { return }
        let suggestion = draft
        sending = true
        defer { sending = false }
        issue = nil
        do {
            _ = try await api.data("v1/me/video-suggestion", method: "PUT", body: ["topic": suggestion.topic, "note": suggestion.note])
            previous = suggestion
            received = true
        } catch { issue = error.localizedDescription }
    }
}

struct SettingsView: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss
    @State private var selfHost = false
    @State private var deletingAccount = false
    @State private var help = false
    var body: some View {
        NavigationStack {
            List {
                if let connection = studio.connection {
                    Section("Connected to") {
                        Label(connection.isHosted ? "ReelDub Cloud" : "Self-hosted", systemImage: connection.isHosted ? "cloud" : "externaldrive")
                        Text(connection.base.host() ?? connection.base.absoluteString).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if let account = studio.account {
                    Section("Account") {
                        if let email = account.email { Text(email) }
                        LabeledContent("Plan", value: account.tier == "paid" ? (account.creator_minutes == true ? "Creator" : "Premium") : account.tier.capitalized)
                        LabeledContent("Daily dubs", value: account.limit.map { "\(account.used) of \($0) used" } ?? "Unlimited")
                    }
                }
                if studio.connection?.isHosted == true, studio.account?.admin_console == true {
                    Section { NavigationLink { AdminConsole(studio:studio).id(studio.connection?.token) } label: {
                        Label("Admin console", systemImage:"gauge.with.dots.needle.67percent")
                    } }
                }
                if studio.connection?.isHosted == true {
                    Section {
                        NavigationLink("Plans & minutes") { CloudStore(studio: studio) }
                    }
                }
                Section("Help and policies") {
                    Button("Help & FAQ") { help = true }
                    PolicyLinks()
                }
                Section {
                    Button("Use my own server") { selfHost = true }
                    if studio.connection?.isHosted == true {
                        Button("Delete account and data", role: .destructive) { deletingAccount = true }
                            .accessibilityIdentifier("delete-cloud-account")
                    }
                    Button("Sign out", role: .destructive) { Task { await studio.disconnect() } }
                }
            }.navigationTitle("Account")
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .sheet(isPresented: $help) {
                    ReelDubHelp(suggestionAPI: studio.connection?.isHosted == true && studio.account?.video_suggestions == true ? studio.api : nil)
                }
                .sheet(isPresented: $deletingAccount) { DeleteCloudAccount(studio: studio) }
                .sheet(isPresented: $selfHost) { SelfHost(studio: studio) }
        }
    }
}

private struct DeleteCloudAccount: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss
    @State private var confirmed = false
    @State private var busy = false
    @State private var issue: String?
    var body: some View {
        NavigationStack {
            List {
                Section("Your account and dubs") {
                    Text("ReelDub will delete your profile, uploads, dubs, reports, and saved videos for this account on this device. This is permanent.")
                    Text("Copies you exported or downloaded elsewhere can remain. Backup copies of account records expire within seven days.")
                }
                Section("App Store purchases") {
                    Text("Cancel any active subscription with Apple to stop renewal. ReelDub keeps a limited purchase and credit record so you can restore your balance.")
                    Link("Manage Apple subscriptions", destination: URL(string: "https://apps.apple.com/account/subscriptions")!)
                }
                if confirmed {
                    Section("Confirm with the Apple Account that owns these dubs") {
                        AppleButton(studio: studio, white: false, busy: $busy, issue: $issue) { identity, challenge, code in
                            try await studio.deleteAccount(identity: identity, challenge: challenge, code: code)
                        }.frame(height: 46)
                        if let issue { Text(issue).foregroundStyle(.red) }
                    }
                } else {
                    Button("Delete my account", role: .destructive) { confirmed = true }
                }
            }
            .navigationTitle("Delete account").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(busy) } }
            .interactiveDismissDisabled(busy)
        }
    }
}

struct SelfHost: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss
    @State private var server = ""
    @State private var key = ""
    @State private var tested: Connection?
    @State private var issue: String?
    @State private var notice: String?
    @State private var operation: Operation?
    @State private var loadingPrice = false
    @FocusState private var field: Field?

    private enum Operation { case test, connect, purchase, restore }
    private enum Field { case address, key }
    private var busy: Bool { operation != nil }
    private var access: SelfHostStore { studio.selfHostAccess }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://your-server.example", text: $server)
                        .keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .focused($field, equals: .address).submitLabel(.next)
                        .onSubmit { field = .key }.accessibilityLabel("Server address")
                    SecureField("Connection key", text: $key).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .focused($field, equals: .key)
                    if let tested {
                        Label("Connection verified" + (tested.base.host().map { " · " + $0 } ?? ""), systemImage: "checkmark.circle")
                            .accessibilityIdentifier("self-host-verified")
                    } else {
                        Button("Test connection") { Task { await testConnection() } }
                            .accessibilityIdentifier("self-host-test")
                            .disabled(server.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                } header: { Text("Your ReelDub server") }
                footer: { Text("Test your server address and key for free. Your server handles the video and audio.") }

                Section {
                    if access.owned {
                        Label("Self-hosting unlocked", systemImage: "checkmark.seal")
                        if let tested {
                            Button("Connect with key") { Task { await connect(tested) } }
                                .accessibilityIdentifier("self-host-connect")
                        }
                    } else {
                        if let product = access.product {
                            Button("Unlock for " + product.displayPrice) { Task { await purchase() } }
                                .accessibilityIdentifier("self-host-buy")
                                .disabled(tested == nil || !access.checked)
                        } else if loadingPrice || !access.checked {
                            ProgressView("Loading App Store price…")
                        } else {
                            Text("The App Store price is unavailable.").foregroundStyle(.secondary)
                            Button("Retry price") { Task { await loadPrice() } }
                        }
                    }
                    Button("Restore Purchases") { Task { await restore() } }
                        .accessibilityIdentifier("self-host-restore")
                } header: { Text("One-time app unlock") }
                footer: { Text("Connect your own servers with one purchase. On a new iPhone, restore with the same Apple Account you use for App Store purchases. ReelDub Cloud uses a separate allowance.") }

                if let operation {
                    switch operation {
                    case .test: ProgressView("Testing connection…")
                    case .connect: ProgressView("Connecting…")
                    case .purchase: ProgressView("Completing purchase…")
                    case .restore: ProgressView("Restoring purchase…")
                    }
                }
                if let issue { Text(issue).foregroundStyle(.red) }
                if let notice { Text(notice).foregroundStyle(.secondary) }
            }.navigationTitle("Your server").navigationBarTitleDisplayMode(.inline)
                .disabled(busy)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(busy) } }
                .interactiveDismissDisabled(busy)
                .onChange(of: server) { _, _ in clearTest() }
                .onChange(of: key) { _, _ in clearTest() }
                .task {
                    if let current = studio.connection, !current.isHosted {
                        server = current.base.absoluteString
                        key = current.token
                    }
                    await access.refresh()
                    if !access.owned { await loadPrice() }
                }
        }
    }

    private func clearTest() {
        tested = nil
        issue = nil
        notice = nil
    }

    private func loadPrice() async {
        guard !loadingPrice else { return }
        loadingPrice = true
        defer { loadingPrice = false }
        // A missing storefront product cannot prevent a connection test or restoration.
        try? await access.loadOffer()
    }

    private func testConnection() async {
        guard !busy else { return }
        field = nil
        clearTest()
        operation = .test
        defer { operation = nil }
        do {
            let url = try Connection.server(server)
            let secret = key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !secret.isEmpty else { throw Problem("Enter the connection key from your server.") }
            let candidate = Connection(base: url, token: secret)
            try await studio.testSelfHost(candidate)
            tested = candidate
        } catch is SessionError {
            issue = "Your server did not recognize that connection key. Check the key and try again."
        } catch is DecodingError {
            issue = "That address responded, but it did not return a ReelDub account. Check the server address."
        } catch { issue = error.localizedDescription }
    }

    private func connect(_ candidate: Connection) async {
        guard !busy else { return }
        issue = nil
        notice = nil
        operation = .connect
        defer { operation = nil }
        do {
            try await studio.connect(candidate)
            dismiss()
        } catch { issue = error.localizedDescription }
    }

    private func purchase() async {
        guard !busy, let tested, access.product != nil, access.checked, !access.owned else { return }
        issue = nil
        notice = nil
        operation = .purchase
        defer { operation = nil }
        do {
            guard try await studio.purchaseSelfHost(for: tested) else {
                notice = "Purchase canceled. Your server connection is verified."
                return
            }
            operation = .connect
            try await studio.connect(tested)
            dismiss()
        } catch { issue = error.localizedDescription }
    }

    private func restore() async {
        guard !busy else { return }
        field = nil
        issue = nil
        notice = nil
        operation = .restore
        defer { operation = nil }
        do {
            if try await access.restore() {
                notice = tested == nil ? "Purchase restored. Test your server connection to continue." : "Purchase restored. You can connect to your server."
            } else {
                notice = "No self-host purchase was found. Check the Apple Account used for Media & Purchases."
            }
        } catch { issue = error.localizedDescription }
    }
}
