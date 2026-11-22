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
                        Button("Delete Cloud account", role: .destructive) { deletingAccount = true }
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
                Section {
                    Text("Deleting your account removes your profile, reports, dubs and private server cache. It also clears the saved dubs on this iPhone. Exported copies and downloads on other devices can remain.")
                    Text("Apple subscriptions keep renewing until you cancel them with Apple. Your purchase and credit records are retained so you can recover paid access with the same Apple Account.")
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
    @Environment(\.colorScheme) private var scheme
    @State private var server = ""
    @State private var key = ""
    @State private var issue: String?
    @State private var busy = false
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://your-server.example", text: $server)
                        .keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityLabel("Server address")
                } header: { Text("Server address") } footer: { Text("Enter the address of your own ReelDub installation.") }
                Section("Connection key") {
                    SecureField("Connection key", text: $key).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("Connect with key") { Task { await connect() } }.disabled(busy)
                }
                if busy { ProgressView("Connecting…") }
                if let issue { Text(issue).foregroundStyle(.red) }
            }.navigationTitle("Your server").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
                .onChange(of: studio.connection?.token) { _, token in if token != nil { dismiss() } }
                .onAppear {
                    if let current = studio.connection, !current.isHosted { server = current.base.absoluteString }
                }
        }
    }
    private func connect() async {
        issue = nil
        busy = true
        defer { busy = false }
        do {
            let url = try Connection.server(server)
            let secret = key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !secret.isEmpty else { throw Problem("Enter the connection key from your server.") }
            try await studio.connect(Connection(base: url, token: secret))
            dismiss()
        } catch { issue = error.localizedDescription }
    }
}
