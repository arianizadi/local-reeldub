import AVKit
import SwiftUI

struct JobView: View {
    let studio: Studio
    let initial: Job
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?
    @State private var movie: URL?
    @State private var reporting = false
    @State private var loading = false
    @State private var problem: String?
    var job: Job { studio.jobs.first { $0.id == initial.id } ?? initial }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let player { VideoPlayer(player: player).frame(height: 330).clipShape(RoundedRectangle(cornerRadius: 20)) }
                    else if job.state == "done" {
                        Button { Task { await play() } } label: {
                            ZStack {
                                Color.clear.overlay { Image("sound-ribbon").resizable().scaledToFill().accessibilityHidden(true) }.clipped()
                                if loading { ProgressView().tint(.white) }
                                else { Label("Play English dub", systemImage: "play.fill").font(.headline).padding().foregroundStyle(.white).background(.black.opacity(0.65), in: Capsule()) }
                            }.frame(height: 170).clipShape(RoundedRectangle(cornerRadius: 20))
                        }.disabled(loading)
                    }
                    Text(job.stage).font(.system(.largeTitle, design: .serif, weight: .bold))
                    TimelineView(.periodic(from: .now, by: 1)) { tick in Text(job.timing(at: tick.date)).monospacedDigit() }
                    if let sec = job.media_seconds { LabeledContent("Video length", value: Job.duration(Int(sec.rounded()))) }
                    if let error = job.error { Text(error).foregroundStyle(.red) }
                    if let url = URL(string: job.url) { Link("Open original Reel", destination: url) }
                    if let movie { ShareLink(item: movie) { Label("Share English video", systemImage: "square.and.arrow.up") } }
                    Divider()
                    Button { reporting = true } label: { Label(job.reported ? "Update issue report" : "Report an issue", image: "report") }
                    if let problem { Text(problem).font(.footnote).foregroundStyle(.red) }
                }.padding(20)
            }.background(Ink.linen).navigationTitle("Reel details").navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
            .sheet(isPresented: $reporting) { ReportView(studio: studio, job: job) }
            .onDisappear { player?.pause() }
        }
    }
    private func play() async {
        guard let api = studio.api else { return }
        loading = true
        defer { loading = false }
        do {
            let url = try await api.movie(job.id)
            movie = url; player = AVPlayer(url: url); player?.play()
        } catch { problem = error.localizedDescription }
    }
}
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
                if let error { Text(error).foregroundStyle(.red) }
            }.navigationTitle("Report an issue").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Send") { Task {
                    guard let api = studio.api else { return }
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
