import AuthenticationServices
import CryptoKit
import SwiftUI

struct SettingsView: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss
    @State private var server = ""
    @State private var key = ""
    @State private var nonce: String?
    @State private var message: String?
    @State private var busy = false
    var body: some View {
        NavigationStack {
            Form {
                if let account = studio.account {
                    Section("Account") {
                        LabeledContent("Access", value: account.tier.capitalized)
                        LabeledContent("Translations today", value: account.limit.map { "\(account.used) of \($0)" } ?? "Unlimited")
                    }
                }
                Section {
                    TextField("https://your-server.example", text: $server).keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                } header: { Text("Server address") } footer: { Text("Use your own ReelDub server or the address supplied by your host.") }
                Section("Sign in with Apple") {
                    SignInWithAppleButton(.signIn) { req in
                        guard let nonce else { return }
                        req.nonce = SHA256.hash(data: Data(nonce.utf8)).map { String(format: "%02x", $0) }.joined()
                    } onCompletion: { result in
                        switch result {
                        case .success(let auth):
                            guard let credential = auth.credential as? ASAuthorizationAppleIDCredential,
                                  let identity = credential.identityToken, let token = String(data: identity, encoding: .utf8), let nonce else { return }
                            Task { await perform {
                                let base = try Connection.server(server)
                                let api = API(connection: Connection(base: base, token: ""))
                                let data = try await api.data("v1/apple/session", method: "POST", body: ["identity_token":token,"challenge":nonce])
                                let session = try JSONDecoder().decode(SessionKey.self, from: data)
                                try await studio.connect(Connection(base: base, token: session.token))
                            } }
                        case .failure(let error): message = error.localizedDescription
                        }
                    }.frame(height: 46).disabled(nonce == nil || busy)
                }
                Section("Self-hosted connection") {
                    SecureField("Connection key", text: $key).textContentType(.password).autocorrectionDisabled().textInputAutocapitalization(.never)
                    Button("Connect with key") { Task { await perform {
                        try await studio.connect(Connection(base: try Connection.server(server), token: key.trimmingCharacters(in: .whitespacesAndNewlines)))
                    } } }.disabled(key.isEmpty || busy)
                }
                if let message { Text(message).foregroundStyle(.red).font(.footnote) }
                if studio.connection != nil { Button("Disconnect", role: .destructive) { Task { await studio.disconnect(); dismiss() } } }
            }.navigationTitle("Server & account")
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            .onAppear {
                if let base = studio.connection?.base { server = base.absoluteString }
                #if targetEnvironment(simulator)
                if server.isEmpty { server = "http://127.0.0.1:8300" }
                #endif
            }
            .task(id: server) {
                nonce = nil
                guard let base = try? Connection.server(server) else { return }
                do {
                    try await Task.sleep(for: .milliseconds(450))
                    let api = API(connection: Connection(base: base, token: ""))
                    let data = try await api.data("v1/apple/challenge", method: "POST")
                    if !Task.isCancelled { nonce = try JSONDecoder().decode(Challenge.self, from: data).challenge }
                } catch {}
            }
        }
    }
    private func perform(_ action: () async throws -> Void) async {
        busy = true; message = nil
        defer { busy = false }
        do { try await action(); dismiss() } catch { message = error.localizedDescription }
    }
}
