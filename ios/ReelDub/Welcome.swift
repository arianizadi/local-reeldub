import SwiftUI

struct Welcome: View {
    let studio: Studio
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Image("brand-mark").resizable().scaledToFit().frame(width: 58, height: 58)
                        .accessibilityHidden(true)
                    Text("Connect your server").font(.largeTitle.bold())
                    Text("Your server translates the dialogue and matches the speaker’s voice. Once connected, share a video link straight from another app.")
                        .foregroundStyle(.secondary)
                    ConnectionForm(studio: studio)
                }.padding(24).frame(maxWidth: 560)
            }.navigationTitle("ReelDub").navigationBarTitleDisplayMode(.inline)
        }
    }
}

struct ConnectionForm: View {
    let studio: Studio
    var onConnect: () -> Void = {}
    @State private var address: String
    @State private var key = ""
    @State private var working = false
    @State private var issue: String?

    init(studio: Studio, onConnect: @escaping () -> Void = {}) {
        self.studio = studio
        self.onConnect = onConnect
        _address = State(initialValue: studio.connection?.base.absoluteString ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Server address").font(.subheadline.weight(.medium))
            TextField("https://dub.example.com", text: $address)
                .textContentType(.URL).keyboardType(.URL).accessibilityIdentifier("server-address")
            Text("Connection key").font(.subheadline.weight(.medium))
            SecureField("Server key", text: $key).textContentType(.password).accessibilityIdentifier("server-key")
            Text("Use the address and key from your server setup.").font(.footnote).foregroundStyle(.secondary)
            if let issue { Text(issue).foregroundStyle(.red) }
            Button(action: { Task { await connect() } }) {
                Text(working ? "Connecting…" : "Connect").font(.headline).frame(maxWidth: .infinity, minHeight: 40)
            }.buttonStyle(.borderedProminent).foregroundStyle(Palette.onAction)
                .accessibilityIdentifier("self-host-connect")
                .disabled(working || address.isEmpty || key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .textInputAutocapitalization(.never).autocorrectionDisabled().textFieldStyle(.roundedBorder)
    }

    private func connect() async {
        guard !working else { return }
        working = true
        issue = nil
        defer { working = false }
        do {
            let server = try Connection.server(address)
            try await studio.connect(Connection(base: server, token: key.trimmingCharacters(in: .whitespacesAndNewlines)))
            key = ""
            onConnect()
        } catch { issue = error.localizedDescription }
    }
}
