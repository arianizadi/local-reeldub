import SwiftUI

enum DubQuality: String, CaseIterable {
    case best, fast
    var title: String { rawValue.capitalized }
    var engine: String { self == .fast ? "KittenTTS 2" : "Qwen3-TTS" }
}

enum DubSource {
    case reel(String), uploaded(String)
    var field: String {
        if case .reel = self { return "url" }
        return "source_id"
    }
    var value: String {
        switch self { case .reel(let value), .uploaded(let value): value }
    }
}

enum DubInput {
    case reel(String), file(ImportedMovie)
    var identity: String {
        switch self { case .reel(let link): link; case .file(let movie): movie.file.absoluteString }
    }
}

/// Shared by the main app and the system share sheet. A failed network request
/// keeps its source receipt and request ID so retrying cannot create a second dub.
struct DubForm: View {
    let api: API
    let input: DubInput?
    let languages: [String: [String]]
    let onQueued: (Job) -> Void
    @State private var quality = DubQuality(rawValue: OutputLanguage.readQuality()) ?? .best
    @State private var preferred = OutputLanguage.read()
    @State private var uploadedID: String?
    @State private var requestID = UUID()
    @State private var working = false
    @State private var error: String?
    private var available: [String] { languages["best"] ?? [] }
    private var target: String { OutputLanguage.selected(preferred, supported: available) }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Picker("Voice matching", selection: $quality) {
                ForEach(DubQuality.allCases, id: \.self) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented).accessibilityIdentifier("dub-quality")
            if quality == .fast && !(languages["fast"] ?? []).contains(target) {
                Text("Fast uses Best for this language on your server.").font(.footnote).foregroundStyle(.secondary)
            } else {
                Text(quality == .best ? "Qwen matches the original speaker."
                     : "Kitten matches the original speaker. Best takes over if Kitten is unavailable.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if !available.isEmpty {
                LabeledContent("Translate to") { OutputLanguagePicker(supported: available, preference: $preferred) }
            } else {
                Text("Reconnect to a server with voice matching enabled.").font(.footnote).foregroundStyle(.secondary)
            }
            if let error { Text(error).font(.callout).foregroundStyle(.red) }
            Button(action: { Task { await submit() } }) {
                HStack {
                    if working, case .file = input {
                        ProgressView().tint(Palette.onAction)
                        Text("Uploading video…").font(.headline)
                    } else { Text("Dub video").font(.headline) }
                }.frame(maxWidth: .infinity, minHeight: 40)
            }
            .buttonStyle(.borderedProminent).foregroundStyle(Palette.onAction)
            .accessibilityIdentifier("submit-dub")
            .disabled(input == nil || available.isEmpty)
        }
        .disabled(working)
        .onChange(of: quality) { _, value in OutputLanguage.saveQuality(value.rawValue); requestID = UUID(); error = nil }
        .onChange(of: preferred) { _, _ in requestID = UUID(); error = nil }
    }

    private func submit() async {
        guard !working, let input else { return }
        working = true
        error = nil
        defer { working = false }
        do {
            let source: DubSource
            switch input {
            case .reel(let link): source = .reel(link)
            case .file(let movie):
                if let uploadedID { source = .uploaded(uploadedID) }
                else {
                    let id = try await api.upload(movie)
                    uploadedID = id
                    source = .uploaded(id)
                }
            }
            try Task.checkCancellation()
            let job = try await api.submit(source, id: requestID, target: target, quality: quality)
            onQueued(job)
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}
