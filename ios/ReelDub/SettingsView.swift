import SwiftUI

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
                    Button("ReelDub help") { help = true }
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
                .sheet(isPresented: $help) { ReelDubHelp() }
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
