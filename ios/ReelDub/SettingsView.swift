import SwiftUI

struct SettingsView: View {
    let studio: Studio
    @Environment(\.dismiss) private var dismiss
    @State private var changeServer = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Connected server") {
                    if let base = studio.connection?.base {
                        Text(base.absoluteString).textSelection(.enabled)
                    }
                    Button("Change server or key") { changeServer.toggle() }
                    if changeServer { ConnectionForm(studio: studio) { changeServer = false } }
                    Button("Disconnect", role: .destructive) { dismiss(); studio.disconnect() }
                }
                Section("Using ReelDub") {
                    Text("Share a video link and choose ReelDub. Pick a voice matching mode and tap Dub video. ReelDub confirms submission and returns you to the app you shared from. Open Dubs to watch.")
                    Text("Use videos and voices you have permission to translate. AI translations can miss words or change their meaning.")
                }
                Section("About") {
                    Text("Fast voice matching is powered by KittenTTS 2. Powered by KittenML.")
                    Link("ReelDub on GitHub", destination: URL(string: "https://github.com/arianizadi/local-reeldub")!)
                }
            }.navigationTitle("Settings").navigationBarTitleDisplayMode(.inline)
                .toolbar { Button("Done") { dismiss() } }
        }
    }
}
