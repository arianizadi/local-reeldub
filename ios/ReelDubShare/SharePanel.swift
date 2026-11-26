import SwiftUI

struct SharePanel: View {
    let items: [NSExtensionItem]
    let close: () -> Void
    @State private var input: DubInput?
    @State private var connection = Connection.read()
    @State private var languages: [String: [String]] = [:]
    @State private var submitted = false
    @State private var closed = false
    @State private var message: String?
    @State private var retry = 0

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Label("ReelDub", systemImage: "waveform").font(.headline)
                    Spacer()
                    Button(submitted ? "Done" : "Cancel", action: finish)
                }
                if submitted {
                    Label("Submitted", systemImage: "checkmark.circle.fill").font(.title.bold())
                        .accessibilityIdentifier("dub-submitted")
                    Text("Find your video in Dubs.").foregroundStyle(.secondary)
                } else if connection == nil {
                    Text("Connect your server").font(.title.bold())
                    Text("Open ReelDub once to enter your server address and key, then share again.").foregroundStyle(.secondary)
                } else if let input, let connection {
                    switch input {
                    case .reel: Text("Dub this Reel").font(.title.bold())
                    case .file(let movie): Text(movie.title).font(.title.bold())
                    }
                    DubForm(api: API(connection: connection), input: input, languages: languages) { _ in
                        submitted = true
                        AccessibilityNotification.Announcement("Submitted").post()
                    }
                } else if message == nil {
                    Text("Reading shared video…").foregroundStyle(.secondary)
                }
                if let message, !submitted {
                    Text(message).foregroundStyle(.secondary)
                    Button("Try again") { retry += 1 }
                }
            }.frame(maxWidth: 550, alignment: .leading).frame(maxWidth: .infinity).padding(22)
        }
        .tint(Palette.action)
        .task(id: retry) {
            message = nil
            guard let connection else { return }
            do {
                if input == nil {
                    let shared = try await SharedReel.extract(from: items)
                    if Task.isCancelled {
                        if case .file(let movie) = shared { movie.remove() }
                        return
                    }
                    input = shared
                }
                if let cached = connection.qualityLanguages, !cached.isEmpty { languages = cached }
                else {
                    languages = try JSONDecoder().decode(Account.self, from: await API(connection: connection).data("v1/me")).quality_languages ?? [:]
                }
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
        .task(id: submitted) {
            guard submitted else { return }
            do { try await Task.sleep(for: .milliseconds(650)) } catch { return }
            finish()
        }
        .onDisappear { if case .file(let movie) = input { movie.remove() } }
    }

    private func finish() {
        guard !closed else { return }
        closed = true
        close()
    }
}
