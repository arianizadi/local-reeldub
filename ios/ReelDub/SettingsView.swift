import SwiftUI

struct SettingsView: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss
    @State private var selfHost = false
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
                        LabeledContent("Plan", value: account.tier == "paid" ? "Premium" : account.tier.capitalized)
                        LabeledContent("Daily translations", value: account.limit.map { "\(account.used) of \($0) used" } ?? "Unlimited")
                    }
                }
                Section {
                    Button("Use my own server") { selfHost = true }
                    Button("Sign out", role: .destructive) { Task { await studio.disconnect() } }
                }
            }.navigationTitle("Account")
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .sheet(isPresented: $selfHost) { SelfHost(studio: studio) }
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
                if let url = try? Connection.server(server) {
                    Section("Apple account on this server") {
                        AppleButton(studio: studio, base: url, white: scheme == .dark, busy: $busy, issue: $issue)
                            .frame(height: 48).id(url).disabled(busy)
                    }
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
