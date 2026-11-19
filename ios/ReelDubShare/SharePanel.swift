import SwiftUI

struct SharePanel: View {
    let link: String?
    let close: () -> Void
    @State private var requestID = UUID()
    @State private var connection = Connection.read()
    @State private var busy = false
    @State private var success = false
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
            if success {
                Image(systemName: "checkmark.circle.fill").font(.largeTitle).foregroundStyle(.green)
                Text("Reel queued").font(.title.bold())
                Text("Follow its progress in ReelDub. You can return to Instagram now.").foregroundStyle(.secondary)
                Button("Done", action: close).buttonStyle(.borderedProminent)
            } else if let validLink, let connection {
                Text("Dub in English?").font(.system(.largeTitle, design: .serif, weight: .semibold))
                Text(validLink).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                Label(connection.isHosted ? "ReelDub Cloud" : (connection.base.host() ?? "Your server"), systemImage: "cloud")
                    .font(.subheadline.weight(.medium))
                Button {
                    Task {
                        busy = true
                        message = nil
                        defer { busy = false }
                        do {
                            _ = try await API(connection: connection).submit(validLink, id: requestID)
                            success = true
                        } catch is SessionError {
                            self.connection = nil
                            message = "Your session ended. Sign in again in ReelDub."
                        } catch { message = error.localizedDescription }
                    }
                } label: {
                    Text(busy ? "Queuing…" : "Translate to English").font(.headline).frame(maxWidth: .infinity, minHeight: 45)
                }.buttonStyle(.borderedProminent).disabled(busy)
            } else {
                Text(connection == nil ? "Sign in to ReelDub first" : "Share a Reel link").font(.title.bold())
                Text(connection == nil ? "Open ReelDub and continue with Apple, then share this Reel again." : "Choose Share on an individual Instagram Reel.").foregroundStyle(.secondary)
                Button("Close", action: close)
            }
            if let message { Text(message).font(.footnote).foregroundStyle(.red) }
            Spacer(minLength: 0)
        }.tint(Color(red: 0.73, green: 0.245, blue: 0.13)).padding(24)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: success)
    }
}
