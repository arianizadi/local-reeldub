import SwiftUI
import AVKit

struct JobView: View {
    let studio: Studio
    let initial: Job
    let requestDeletion: (Job) -> Void
    @State private var player: AVPlayer?
    @State private var didStart = false
    @State private var playerObservation: NSKeyValueObservation?
    @AppStorage("ReelDub.EnglishSubtitles") private var wantsSubtitles = UIAccessibility.isClosedCaptioningEnabled
    @State private var subtitles: DubSubtitles?
    @State private var reporting = false
    @State private var preparingShare = false
    @State private var sharing: ShareFile?
    @State private var problem: String?
    @State private var project: ProjectInput?
    @State private var showBalance = false
    @State private var retrying = false
    @State private var confirmSkip = false
    private var job: Job { studio.jobs.first { $0.id == initial.id } ?? initial }

    private var expired: Bool { studio.isExpired(job) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if job.state == "done", !expired {
                    Color.black.aspectRatio(0.83, contentMode: .fit)
                        .overlay {
                            if let player { VideoPlayer(player: player) }
                            else {
                                Poster(job: job, api: studio.api)
                                Button { Task { await play() } } label: {
                                    Label("Play \(job.targetName) dub", systemImage: "play.fill")
                                        .font(.headline).padding().foregroundStyle(.white).background(.black.opacity(0.76), in: Capsule())
                                }
                            }
                        }.clipShape(RoundedRectangle(cornerRadius: 20))
                    if let subtitles, subtitles.item === player?.currentItem {
                        Toggle(isOn: Binding(get: { wantsSubtitles }, set: { enabled in
                            wantsSubtitles = enabled
                            subtitles.setEnabled(enabled)
                        })) {
                            Label("\(job.targetName) subtitles", systemImage: "captions.bubble")
                        }
                        .tint(Color(red: 0.40, green: 0.59, blue: 0.29))
                        .accessibilityIdentifier("english-subtitles")
                    }
                    Button { Task { await share() } } label: {
                        Label(preparingShare ? "Preparing video…" : "Share \(job.targetName) video", systemImage: "square.and.arrow.up")
                            .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                            .foregroundStyle(Palette.ink).background(Palette.ember, in: RoundedRectangle(cornerRadius: 13))
                    }.disabled(preparingShare)
                }
                Text(job.displayTitle).font(.custom("InstrumentSerif-Italic", size: 35, relativeTo: .title))
                if let author = job.author, !author.isEmpty { Text(author).font(.subheadline).foregroundStyle(.secondary) }
                if job.state == "running" || job.state == "queued" { ProgressRow(job: job) }
                else {
                    Label(job.state == "done" ? (studio.storageLabel(job) ?? "\(job.targetName) dub ready") : "Couldn’t finish this dub", systemImage: job.state == "done" ? "checkmark.circle" : "exclamationmark.circle")
                    Text(job.timing(at: Date())).font(.subheadline).foregroundStyle(.secondary)
                    if studio.canDownloadAgain(job.id), job.available_on_server != false {
                        Button("Download to this device", systemImage: "arrow.down.circle") {
                            do { try studio.downloadAgain(job.id) }
                            catch { problem = error.localizedDescription }
                        }
                    }
                    if job.state == "done", let deadline = Job.date(job.cache_expires), deadline > .now {
                        LabeledContent("Server copy expires", value: deadline.formatted(date: .abbreviated, time: .shortened))
                    }
                    if let seconds = job.media_seconds { LabeledContent("Video length", value: Job.duration(Int(seconds.rounded()))) }
                }
                if job.state == "queued", let lane = job.lane {
                    LabeledContent("Queue", value: lane == "premium" ? "Priority" : "Standard")
                    if lane == "free", (job.ahead ?? 0) > 0, (studio.account?.queue_skips ?? 0) > 0 {
                        Button("Use 1 queue pass") { confirmSkip = true }
                    }
                }
                if let error = job.error { Text(error).foregroundStyle(.red) }
                if job.state == "failed" || expired {
                    Button(expired ? "Translate again" : "Try this Reel again") { Task {
                        if studio.account?.creator_minutes == true {
                            if Job.isReel(job.url) { project = .reel(job.url) }
                            else if let source=job.source_id { project = .source(source) }
                            else { problem="Import this video again from Photos or Files." }
                            return
                        }
                        retrying = true
                        defer { retrying = false }
                        do { try await studio.submit(job.url, target: job.target); problem = "A new attempt is in your Dubs." }
                        catch let error as ServiceError where error.status == 428 {
                            if Job.isReel(job.url) { project = .reel(job.url) }
                            else if let id = job.source_id { project = .source(id) }
                            else { problem = "Import this video again from Photos or Files." }
                        } catch {
                            showBalance = (error as? ServiceError)?.needsBalance == true
                            problem = error.localizedDescription
                        }
                    } }.disabled(retrying)
                }
                if showBalance, studio.connection?.isHosted == true {
                    BalanceOptions(live: studio.account?.purchase_preflight == true, plans: AnyView(CloudPlanSheet(studio: studio)))
                }
                if let problem { Text(problem).font(.footnote).foregroundStyle(.secondary) }
                Divider()
                if Job.isReel(job.url), let url = URL(string: job.url) { Link(destination: url) { Label("Open original Reel", systemImage: "arrow.up.right.square") } }
                Button { reporting = true } label: { Label(job.reported ? "Update issue report" : "Report an issue", systemImage: "flag") }
                if !job.rdPhase.isActive {
                    Button(role: .destructive) { requestDeletion(job) } label: {
                        Label("Delete dub", systemImage: "trash")
                    }.accessibilityIdentifier("delete-dub")
                }
            }.padding(20).frame(maxWidth: 650).frame(maxWidth: .infinity)
        }.background(Palette.canvas).navigationTitle("Reel details").navigationBarTitleDisplayMode(.inline)
            .sheet(item:$project) { input in
                if let api=studio.api { QuoteSheet(api:api,input:input,languages:studio.account?.output_languages ?? ["en"],target:job.target,trim:job.trimmed ?? false,liveOffers:studio.account?.purchase_preflight == true,plans:AnyView(CloudPlanSheet(studio:studio))) { _ in Task { await studio.refresh() } } }
            }
            .confirmationDialog("Join the end of the priority line?", isPresented: $confirmSkip, titleVisibility: .visible) {
                Button("Use 1 queue pass") {
                    Task {
                        guard let api = studio.api else { return }
                        do { _ = try await api.data("v1/jobs/" + job.id + "/skip", method: "POST"); await studio.refresh() }
                        catch { problem = error.localizedDescription }
                    }
                }
            } message: { Text("Earlier priority dubs keep their places. A job already processing will finish its turn.") }
            .sheet(isPresented: $reporting) { ReportView(studio: studio, job: job) }
            .sheet(item: $sharing) { file in ExportSheet(url: file.url) }
            .task(id: job.state) {
                if job.state == "done", !expired, !didStart { await play() }
            }
            .task(id: player) {
                subtitles = nil
                guard let item = player?.currentItem,
                      let track = try? await DubSubtitles.load(from: item, language: job.target),
                      !Task.isCancelled, item === player?.currentItem else { return }
                track.setEnabled(wantsSubtitles)
                subtitles = track
            }
            .onReceive(NotificationCenter.default.publisher(for: AVPlayerItem.mediaSelectionDidChangeNotification)) { change in
                guard let subtitles, let item = change.object as? AVPlayerItem,
                      item === subtitles.item, item === player?.currentItem else { return }
                // The native fullscreen player also offers a subtitle menu.
                wantsSubtitles = subtitles.isEnabled
            }
            .onDisappear { player?.pause() }
    }
    private func play() async {
        guard let api = studio.api else { return }
        didStart = true
        problem = nil
        do {
            let url: URL
            if let local = studio.localMovie(job.id) { url = local }
            else { url = try await api.playback(job.id) }
            guard !Task.isCancelled else { return }
            let item = AVPlayerItem(url: url)
            playerObservation = item.observe(\.status, options: [.new]) { item, _ in
                guard item.status == .failed else { return }
                Task { @MainActor in player = nil; problem = "Playback stopped. Tap Play to try again." }
            }
            let next = AVPlayer(playerItem: item)
            player = next
            next.play()
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }
    private func share() async {
        preparingShare = true
        defer { preparingShare = false }
        do { sharing = ShareFile(url: try await studio.shareMovie(job.id)) }
        catch { problem = error.localizedDescription }
    }
}

struct DubSubtitles {
    let item: AVPlayerItem
    let group: AVMediaSelectionGroup
    let option: AVMediaSelectionOption

    static func load(from item: AVPlayerItem, language: String) async throws -> Self? {
        // AVKit can also offer live transcription for files with no subtitle track.
        let tracks = try await item.asset.load(.tracks)
        guard tracks.contains(where: { [.subtitle, .closedCaption, .text].contains($0.mediaType) }) else { return nil }
        guard let group = try await item.asset.loadMediaSelectionGroup(for: .legible), group.allowsEmptySelection else { return nil }
        let options = AVMediaSelectionGroup.mediaSelectionOptions(from: group.options, with: Locale(identifier: language))
        guard let option = options.first(where: { !$0.hasMediaCharacteristic(.containsOnlyForcedSubtitles) }) else { return nil }
        return Self(item: item, group: group, option: option)
    }

    var isEnabled: Bool { item.currentMediaSelection.selectedMediaOption(in: group) == option }

    func setEnabled(_ enabled: Bool) {
        item.select(enabled ? option : nil, in: group)
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
