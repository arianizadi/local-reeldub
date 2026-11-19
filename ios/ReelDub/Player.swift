import SwiftUI
import AVKit

struct JobView: View {
    let studio: Studio
    let initial: Job
    @State private var player: AVPlayer?
    @State private var didStart = false
    @State private var playerObservation: NSKeyValueObservation?
    @State private var reporting = false
    @State private var preparingShare = false
    @State private var sharing: ShareFile?
    @State private var problem: String?
    @State private var retrying = false
    private var job: Job { studio.jobs.first { $0.id == initial.id } ?? initial }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if job.state == "done" {
                    Color.black.aspectRatio(0.83, contentMode: .fit)
                        .overlay {
                            if let player { VideoPlayer(player: player) }
                            else {
                                Poster(job: job, api: studio.api)
                                Button { Task { await play() } } label: {
                                    Label("Play English dub", systemImage: "play.fill")
                                        .font(.headline).padding().foregroundStyle(.white).background(.black.opacity(0.76), in: Capsule())
                                }
                            }
                        }.clipShape(RoundedRectangle(cornerRadius: 20))
                    Button { Task { await share() } } label: {
                        Label(preparingShare ? "Preparing video…" : "Share English video", systemImage: "square.and.arrow.up")
                            .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                            .foregroundStyle(Palette.ink).background(Palette.ember, in: RoundedRectangle(cornerRadius: 13))
                    }.disabled(preparingShare)
                }
                Text(job.displayTitle).font(.system(.title, design: .serif, weight: .semibold))
                if let author = job.author, !author.isEmpty { Text(author).font(.subheadline).foregroundStyle(.secondary) }
                if job.state == "running" || job.state == "queued" { ProgressRow(job: job) }
                else {
                    Label(job.state == "done" ? "English dub ready" : "Couldn’t finish this dub", systemImage: job.state == "done" ? "checkmark.circle" : "exclamationmark.circle")
                    Text(job.timing(at: Date())).font(.subheadline).foregroundStyle(.secondary)
                    if let seconds = job.media_seconds { LabeledContent("Video length", value: Job.duration(Int(seconds.rounded()))) }
                }
                if let error = job.error { Text(error).foregroundStyle(.red) }
                if job.state == "failed" {
                    Button("Try this Reel again") { Task {
                        retrying = true
                        defer { retrying = false }
                        do { try await studio.submit(job.url); problem = "A new attempt is in your Dubs." }
                        catch { problem = error.localizedDescription }
                    } }.disabled(retrying)
                }
                if let problem { Text(problem).font(.footnote).foregroundStyle(.secondary) }
                Divider()
                if let url = URL(string: job.url) { Link(destination: url) { Label("Open original Reel", systemImage: "arrow.up.right.square") } }
                Button { reporting = true } label: { Label(job.reported ? "Update issue report" : "Report an issue", systemImage: "flag") }
            }.padding(20).frame(maxWidth: 650).frame(maxWidth: .infinity)
        }.background(Palette.canvas).navigationTitle("Reel details").navigationBarTitleDisplayMode(.inline)
            .sheet(isPresented: $reporting) { ReportView(studio: studio, job: job) }
            .sheet(item: $sharing) { file in ExportSheet(url: file.url) }
            .task(id: job.state) {
                if job.state == "done", !didStart { await play() }
            }
            .onDisappear { player?.pause() }
    }
    private func play() async {
        guard let api = studio.api else { return }
        didStart = true
        problem = nil
        do {
            let url = try await api.playback(job.id)
            guard !Task.isCancelled else { return }
            let item = AVPlayerItem(url: url)
            playerObservation = item.observe(\.status, options: [.new]) { item, _ in
                guard item.status == .failed else { return }
                Task { @MainActor in player = nil; problem = "Playback stopped. Tap Play English dub to try again." }
            }
            let next = AVPlayer(playerItem: item)
            player = next
            next.play()
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }
    private func share() async {
        guard let api = studio.api else { return }
        preparingShare = true
        defer { preparingShare = false }
        do { sharing = ShareFile(url: try await api.movie(job.id)) }
        catch { problem = error.localizedDescription }
    }
}
private struct ShareFile: Identifiable { let url: URL; var id: URL { url } }
private struct ExportSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: [url], applicationActivities: nil)
        controller.completionWithItemsHandler = { _, _, _, _ in try? FileManager.default.removeItem(at: url) }
        return controller
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
