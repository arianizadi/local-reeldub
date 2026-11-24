import SwiftUI
import AVKit

struct JobView: View {
    let studio: Studio
    let initial: Job
    let requestDeletion: (Job) -> Void
    @State private var player: AVPlayer?
    @State private var didStart = false
    @State private var preparingPlayback = false
    @State private var playbackRequest = UUID()
    @State private var temporaryPlayback: URL?
    @State private var soundMuted = true
    @State private var playerObservation: NSKeyValueObservation?
    @AppStorage("ReelDub.EnglishSubtitles") private var wantsSubtitles = true
    @AppStorage("ReelDub.PlaybackMuted") private var wantsMuted = false
    @State private var subtitles: DubSubtitles?
    @State private var pendingCaptionChoice: Bool?
    @State private var reporting = false
    @State private var preparingShare = false
    @State private var sharing: ShareFile?
    @State private var problem: String?
    @State private var project: ProjectInput?
    @State private var importedFile: URL?
    @State private var confirmSkip = false
    private var job: Job { studio.jobs.first { $0.id == initial.id } ?? initial }

    private var originalURL: URL? { Job.isReel(job.url) ? URL(string: job.url) : nil }
    private var expired: Bool { studio.isExpired(job) }

    var body: some View {
        GeometryReader { viewport in
            details(in: viewport.size)
        }
        .background(Palette.canvas).navigationTitle("Video details").navigationBarTitleDisplayMode(.inline)
            .sheet(item: $project, onDismiss: {
                if let importedFile { try? FileManager.default.removeItem(at: importedFile); self.importedFile = nil }
            }) { input in
                if let api=studio.api { QuoteSheet(api:api,input:input,voiceModes:studio.account?.upload_voice_modes ?? [:],target:job.target,trim:job.trimmed ?? false,creatorMinutes:studio.account?.creator_minutes == true,liveOffers:studio.account?.purchase_preflight == true,plans:AnyView(CloudPlanSheet(studio:studio))) { _ in Task { await studio.refresh() } } }
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
                if job.state == "done", !expired, !didStart, !UIAccessibility.isVoiceOverRunning { await play() }
            }
            .onReceive(NotificationCenter.default.publisher(for: AVPlayerItem.mediaSelectionDidChangeNotification)) { change in
                guard let subtitles, let item = change.object as? AVPlayerItem,
                      item === subtitles.item, item === player?.currentItem else { return }
                if let pendingCaptionChoice {
                    guard subtitles.isEnabled == pendingCaptionChoice else { return }
                    self.pendingCaptionChoice = nil
                }
                // Keep an explicit change from the native fullscreen menu, once selection has settled.
                wantsSubtitles = subtitles.isEnabled
            }
            .onReceive(NotificationCenter.default.publisher(for: AVAudioSession.interruptionNotification)) { note in
                guard let player, !soundMuted,
                      let type = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                      type == AVAudioSession.InterruptionType.ended.rawValue,
                      let options = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt,
                      AVAudioSession.InterruptionOptions(rawValue: options).contains(.shouldResume),
                      player.timeControlStatus != .paused else { return }
                applySound(muted: false, on: player)
            }
            .onReceive(NotificationCenter.default.publisher(for: AVAudioSession.mediaServicesWereResetNotification)) { _ in
                guard player != nil else { return }
                stopPlayback()
                problem = "Audio was reset. Tap Play to continue."
            }
            .onChange(of: job.recoveryCue) { _, cue in
                if let cue { AccessibilityNotification.Announcement(cue).post() }
            }
            .onDisappear { stopPlayback() }
    }
    private func details(in viewport: CGSize) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if job.state == "done", !expired {
                    DubVideoFrame(viewport: viewport)
                        .overlay {
                            if let player { VideoPlayer(player: player) }
                            else if preparingPlayback {
                                DubLoadingIndicator("Loading dub…")
                                    .environment(\.colorScheme, .dark)
                            }
                            else {
                                Poster(job: job, api: studio.api)
                                Button { Task { await play() } } label: {
                                    Label("Play \(job.targetName) dub", systemImage: "play.fill")
                                        .font(.headline).padding().foregroundStyle(.white).background(.black.opacity(0.76), in: Capsule())
                                }
                            }
                        }.clipShape(RoundedRectangle(cornerRadius: 20))
                    if let player {
                        HStack(spacing: 16) {
                            Button {
                                chooseSound(muted: !soundMuted, on: player)
                            } label: {
                                Label(soundMuted ? "Unmute video" : "Mute video",
                                      systemImage: soundMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                                    .labelStyle(.iconOnly)
                                    .frame(minWidth: 44, minHeight: 44)
                                    .background(.quaternary, in: Circle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityIdentifier("playback-sound")
                            if let subtitles, subtitles.item === player.currentItem {
                                Toggle(isOn: Binding(get: { wantsSubtitles }, set: { enabled in
                                    wantsSubtitles = enabled
                                    applyCaptions(enabled, to: subtitles)
                                })) {
                                    Label("\(job.targetName) subtitles", systemImage: "captions.bubble")
                                }
                                .tint(Color(red: 0.40, green: 0.59, blue: 0.29))
                                .accessibilityIdentifier("english-subtitles")
                            }
                        }
                        .onReceive(player.publisher(for: \.isMuted).receive(on: RunLoop.main)) { _ in
                            // AVKit can also change sound when using its native playback controls.
                            guard self.player === player, player.isMuted != soundMuted else { return }
                            chooseSound(muted: player.isMuted, on: player)
                        }
                        .onReceive(player.publisher(for: \.timeControlStatus).removeDuplicates().receive(on: RunLoop.main)) { status in
                            // Replay from native controls can follow an interruption that deactivated the session.
                            guard self.player === player, status == .playing, !soundMuted else { return }
                            applySound(muted: false, on: player)
                        }
                    }
                    Button { Task { await share() } } label: {
                        Label(preparingShare ? "Preparing video…" : "Share \(job.targetName) video", systemImage: "square.and.arrow.up")
                            .font(.headline).frame(maxWidth: .infinity, minHeight: 50)
                            .foregroundStyle(Palette.ink).background(Palette.leaf, in: RoundedRectangle(cornerRadius: 13))
                    }.disabled(preparingShare)
                }
                Text(job.displayTitle).font(.custom("InstrumentSerif-Italic", size: 35, relativeTo: .title))
                if let author = job.author, !author.isEmpty { Text(author).font(.subheadline).foregroundStyle(.secondary) }
                if let mode = job.voice_mode {
                    Label(mode == "match" ? "AI-translated audio · Matched speaker" : "AI-translated audio · Narrator",
                          systemImage: "waveform")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                if let recovery = job.recoveryNotice {
                    Label(recovery, systemImage: "arrow.clockwise")
                        .font(.body).padding(14).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.orange.opacity(0.13), in: RoundedRectangle(cornerRadius: 12))
                        .accessibilityIdentifier("render-recovery")
                }
                if job.state == "running" || job.state == "queued" { ProgressRow(job: job) }
                else {
                    Label(job.state == "done" ? (studio.storageLabel(job) ?? "\(job.targetName) dub ready") : "Couldn’t finish this dub", systemImage: job.state == "done" ? "checkmark.circle" : "exclamationmark.circle")
                    if let error = job.error {
                        Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                        if let originalURL { Link("Check original Reel", destination: originalURL) }
                    }
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
                if job.state != "failed", let quality = job.lab_mode ?? job.render_quality {
                    LabeledContent("Render quality", value: quality.capitalized)
                }
                if job.state == "queued", let lane = job.lane {
                    let recovering = (job.recovery_count ?? 0) > 0 || job.stage == "Recovering render"
                    DubValueRow("Queue", value: recovering ? "Recovery priority" : lane == "premium" ? "Priority" : "Standard")
                    if !recovering, lane == "free", (job.ahead ?? 0) > 0, (studio.account?.queue_skips ?? 0) > 0 {
                        Button("Use 1 queue pass") { confirmSkip = true }
                    }
                }
                if job.state == "failed" || expired {
                    if Job.isReel(job.url) || job.source_id == nil {
                        VideoImportButton(receive: { movie in
                            importedFile = movie.file
                            project = .file(movie.file, movie.title)
                        }, failed: { problem = $0 }, title: "Choose video again")
                    } else {
                        Button(expired ? "Translate again" : "Try again", systemImage: "arrow.clockwise") {
                            if let source = job.source_id { project = .source(source) }
                        }.buttonStyle(.bordered)
                    }
                }
                if let problem { Text(problem).font(.footnote).foregroundStyle(.secondary) }
                Divider()
                if job.error == nil, let originalURL { Link(destination: originalURL) { Label("Open original Reel", systemImage: "arrow.up.right.square") } }
                Button { reporting = true } label: { Label(job.reported ? "Update issue report" : "Report an issue", systemImage: "flag") }
                if !job.rdPhase.isActive {
                    Button(role: .destructive) { requestDeletion(job) } label: {
                        Label("Remove from Library", systemImage: "trash")
                    }.accessibilityIdentifier("delete-dub")
                }
            }.padding(DubVideoFrame.inset).frame(maxWidth: DubVideoFrame.contentWidth).frame(maxWidth: .infinity)
        }
    }

    private func play() async {
        guard !preparingPlayback, player == nil else { return }
        let request = UUID()
        playbackRequest = request
        preparingPlayback = true
        defer { if playbackRequest == request { preparingPlayback = false } }
        didStart = true
        problem = nil
        do {
            let url: URL
            if let local = studio.localMovie(job.id) { url = local }
            else {
                guard let api = studio.api else {
                    throw Problem("Restore your self-host unlock in Account to play the server copy.")
                }
                url = try await api.playback(job.id)
                if url.isFileURL {
                    guard playbackRequest == request, !Task.isCancelled else {
                        try? FileManager.default.removeItem(at: url)
                        return
                    }
                    temporaryPlayback = url
                }
            }
            guard playbackRequest == request, !Task.isCancelled else { return }
            let item = AVPlayerItem(url: url)
            let track = try? await DubSubtitles.load(from: item, language: job.target)
            guard playbackRequest == request, !Task.isCancelled else { return }
            let next = AVPlayer()
            if let track {
                next.appliesMediaSelectionCriteriaAutomatically = false
                // Select before attachment so a cue starting at time zero is delivered.
                track.setEnabled(wantsSubtitles)
            }
            next.isMuted = true
            soundMuted = true
            player = next
            playerObservation = item.observe(\.status, options: [.new]) { item, _ in
                Task { @MainActor in
                    guard playbackRequest == request, item === next.currentItem else { return }
                    switch item.status {
                    case .readyToPlay:
                        if let track {
                            if track.isEnabled != wantsSubtitles { applyCaptions(wantsSubtitles, to: track) }
                            subtitles = track
                        }
                        applySound(muted: wantsMuted, on: next)
                        next.play()
                    case .failed:
                        stopPlayback()
                        problem = "Playback stopped. Tap Play to try again."
                    default:
                        break
                    }
                }
            }
            next.replaceCurrentItem(with: item)
        } catch {
            guard playbackRequest == request else { return }
            stopPlayback()
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func applyCaptions(_ enabled: Bool, to track: DubSubtitles) {
        pendingCaptionChoice = track.isEnabled == enabled ? nil : enabled
        track.setEnabled(enabled)
    }

    @discardableResult
    private func applySound(muted: Bool, on player: AVPlayer) -> Bool {
        do {
            if muted { DubPlaybackAudio.mute(player) }
            else { try DubPlaybackAudio.unmute(player) }
            soundMuted = muted
            return true
        } catch {
            DubPlaybackAudio.mute(player)
            soundMuted = true
            problem = "Sound couldn’t start. Tap Unmute to try again."
            return false
        }
    }

    private func chooseSound(muted: Bool, on player: AVPlayer) {
        if applySound(muted: muted, on: player) { wantsMuted = muted }
    }

    private func stopPlayback() {
        playbackRequest = UUID()
        preparingPlayback = false
        playerObservation = nil
        if let player {
            player.pause()
            player.replaceCurrentItem(with: nil)
            DubPlaybackAudio.release(player)
        }
        player = nil
        soundMuted = true
        subtitles = nil
        pendingCaptionChoice = nil
        if let temporaryPlayback {
            try? FileManager.default.removeItem(at: temporaryPlayback)
            self.temporaryPlayback = nil
        }
    }

    private func share() async {
        preparingShare = true
        defer { preparingShare = false }
        do { sharing = ShareFile(url: try await studio.shareMovie(job.id)) }
        catch { problem = error.localizedDescription }
    }
}

/// The session belongs to the app, while mute belongs to each visible player.
@MainActor
enum DubPlaybackAudio {
    private static let audible = NSHashTable<AVPlayer>.weakObjects()

    static func unmute(_ player: AVPlayer) throws {
        let audio = AVAudioSession.sharedInstance()
        if audio.category != .playback || audio.mode != .moviePlayback {
            try audio.setCategory(.playback, mode: .moviePlayback)
        }
        // iOS can deactivate a session without changing its category or the player's mute flag.
        try audio.setActive(true)
        if #available(iOS 26.0, *), audio.isOutputMuted {
            try audio.setOutputMuted(false)
        }
        player.isMuted = false
        audible.add(player)
    }

    static func mute(_ player: AVPlayer) {
        player.isMuted = true
        audible.remove(player)
        if audible.allObjects.isEmpty {
            // Silent autoplay can mix with music from another app.
            try? AVAudioSession.sharedInstance().setCategory(.ambient, mode: .default)
        }
    }

    static func release(_ player: AVPlayer) {
        audible.remove(player)
        guard audible.allObjects.isEmpty else { return }
        let audio = AVAudioSession.sharedInstance()
        try? audio.setActive(false, options: .notifyOthersOnDeactivation)
        try? audio.setCategory(.ambient, mode: .default)
    }
}

/// The inline video shares its viewport with sound, captions and the share button.
struct DubVideoFrame: View {
    static let contentWidth: CGFloat = 650
    static let inset: CGFloat = 20
    let viewport: CGSize

    var body: some View {
        let width = max(0, min(Self.contentWidth, viewport.width) - 2 * Self.inset)
        let controls: CGFloat = Self.inset + 2 * 18 + 44 + 50
        let height = min(width / 0.83, max(0, viewport.height - controls))
        Color.black.frame(height: height)
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
