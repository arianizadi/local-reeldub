import SwiftUI

struct ReportView: View {
    let studio: Studio
    let job: Job
    @Environment(\.dismiss) private var dismiss
    @State private var category = "Translation"
    @State private var note = ""
    @State private var sending = false
    @State private var error: String?
    var body: some View {
        NavigationStack {
            Form {
                Picker("Issue", selection: $category) { ForEach(["Translation","Voice","Timing","Background","Other"], id: \.self) { Text($0) } }
                Section("What went wrong?") { TextField("For example, a word was mistranslated at 0:12.", text: $note, axis: .vertical).lineLimit(3...8) }
                Section { Text(job.url).font(.footnote) } header: { Text("Reel included") } footer: {
                    Text("Your note, Reel link and render details go to the operator of your connected server for debugging.")
                }
                if note.count > 2000 { Text("Shorten your note to 2,000 characters.").foregroundStyle(.red) }
                if sending { ProgressView("Sending report…") }
                if let error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("Report an issue").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Send") { Task {
                    guard !sending, let api = studio.api else { return }
                    sending = true
                    defer { sending = false }
                    do {
                        _ = try await api.data("v1/jobs/\(job.id)/report", method: "POST", body: ["category":category,"note":note])
                        await studio.refresh(); dismiss()
                    } catch { self.error = error.localizedDescription }
                } }.disabled(sending || note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || note.count > 2000) }
            }
        }
    }
}
