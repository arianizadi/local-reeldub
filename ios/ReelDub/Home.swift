import PhotosUI
import UniformTypeIdentifiers
import CryptoKit
import SwiftUI
import UIKit

@MainActor
struct Home: View {
    let studio: Studio
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.scenePhase) private var scenePhase
    @State private var path: [Job] = []
    @State private var sheet: LibrarySheet?
    @State private var hasLoaded = false
    @State private var searching = false
    @State private var query = ""
    @State private var filter: RDShelfFilter = .all
    @State private var selecting = false
    @State private var selected: Set<String> = []
    @State private var deletion: [Job] = []
    @State private var rangeSelection = LibraryDragSelection()
    private var eligible: [String] { sections.flatMap { $0.jobs }.filter { !$0.rdPhase.isActive }.map(\.id) }
    @State private var deletionError: String?

    private enum LibrarySheet: String, Identifiable {
        case composer, account, selfHost
        var id: Self { self }
    }

    private let canvas = Color(red: 0.064, green: 0.074, blue: 0.070)
    private let leaf = Palette.leaf
    private var visible: [Job] {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return studio.jobs.filter { job in
            filter.admits(job) && (filter != .ready || !studio.isExpired(job)) && (text.isEmpty || [job.displayTitle, job.author ?? "", job.code]
                .contains { $0.localizedStandardContains(text) })
        }
    }

    var body: some View {
        NavigationStack(path: $path) {
            VStack(alignment: .leading, spacing: 0) {
                if usesWindowToolbar {
                    if !selecting && typeSize > .xLarge {
                        addReel.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 18)
                    }
                } else { header }
                if searching {
                    HStack {
                        RDSearchField(text: $query)
                        Button("Cancel") { searching = false; query = "" }
                    }.padding(.horizontal, 18).padding(.bottom, 12)
                }
                if typeSize.isAccessibilitySize {
                    filterPicker.pickerStyle(.menu).padding(.horizontal, 18)
                } else {
                    filterPicker.pickerStyle(.segmented).padding(.horizontal, 18).padding(.bottom, 12)
                }
                Divider()
                transfers
                if selecting {
                    Divider()
                    HStack {
                        Button(selected.count == eligible.count && !eligible.isEmpty ? "Deselect All" : "Select All") {
                            selected = selected.count == eligible.count ? [] : Set(eligible)
                        }.disabled(eligible.isEmpty)
                        Spacer()
                        Button(role: .destructive) {
                            deletion = visible.filter { selected.contains($0.id) }
                        } label: {
                            Label(selected.isEmpty ? "Remove" : "Remove (\(selected.count))", systemImage: "trash")
                        }.disabled(selected.isEmpty).accessibilityIdentifier("delete-selected")
                    }.font(.headline).padding(.horizontal, 20).frame(minHeight: 56)
                }
            }
            .background(canvas.ignoresSafeArea())
            .navigationTitle(usesWindowToolbar ? "Dubs" : "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(usesWindowToolbar ? .visible : .hidden, for: .navigationBar)
            .toolbar {
                if usesWindowToolbar {
                    // iPadOS positions the system window controls within the navigation bar.
                    ToolbarItemGroup(placement: .topBarTrailing) { headerActions }
                }
            }
            .navigationDestination(for: Job.self) { job in
                JobView(studio: studio, initial: job, requestDeletion: { deletion = [$0] })
            }
            .sheet(item: $sheet) { item in
                switch item {
                case .account: SettingsView(studio: studio).presentationDragIndicator(.visible)
                case .selfHost: SelfHost(studio: studio).presentationDragIndicator(.visible)
                case .composer:
                    VStack(alignment: .leading, spacing: 0) {
                        HStack(alignment: .center, spacing: 12) {
                            Text("New dub").font(.title2.bold())
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .accessibilityAddTraits(.isHeader)
                            Button { sheet = nil } label: {
                                Image(systemName: "xmark").font(.system(size: 18, weight: .semibold))
                                    .frame(width: 48, height: 48).background(Color(.secondarySystemFill), in: Circle())
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain).foregroundStyle(.primary).accessibilityLabel("Close")
                        }.padding(.horizontal, 18).padding(.top, 18).padding(.bottom, 6)
                        ScrollView {
                            RDComposer(studio: studio) { query = ""; filter = .all; sheet = nil }
                        }
                    }
                    .presentationDetents(typeSize.isAccessibilitySize ? [.large] : [.medium, .large])
                    .presentationDragIndicator(.visible)
                }
            }
        }
        .preferredColorScheme(.dark).tint(leaf)
        .onChange(of: selecting) { _, active in if !active { rangeSelection.detach() } }
        .onDisappear { rangeSelection.detach() }
        .onChange(of: eligible) { _, ids in selected.formIntersection(ids) }
        .confirmationDialog(deletion.count > 1 ? "Remove \(deletion.count) dubs?" : "Remove this dub?",
                            isPresented: Binding(get: { !deletion.isEmpty }, set: { if !$0 { deletion = [] } }),
                            titleVisibility: .visible, presenting: deletion.isEmpty ? nil : deletion) { items in
            Button("Remove from Library", role: .destructive) { remove(items) }
                .accessibilityIdentifier("confirm-library-removal")
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("Removes the library entry and its download from this device. Copies you saved in Photos or Files stay there.")
        }
        .alert("Couldn’t finish removing dubs", isPresented: Binding(get: { deletionError != nil },
               set: { if !$0 { deletionError = nil } })) { Button("OK") {} } message: { Text(deletionError ?? "") }
        .task(id: scenePhase == .active && studio.hasPendingRemovals) {
            guard scenePhase == .active, studio.hasPendingRemovals else { return }
            var delay = 15
            while !Task.isCancelled, studio.hasPendingRemovals {
                await studio.syncLibraryRemovals()
                guard studio.hasPendingRemovals else { return }
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                delay = min(delay * 2, 300)
            }
        }
        .task(id: scenePhase == .active && (studio.connection?.isHosted == true || studio.selfHostAccess.owned)) {
            guard scenePhase == .active else { return }
            if studio.connection?.isHosted == true || studio.selfHostAccess.owned {
                await studio.refresh()
            }
            hasLoaded = true
        }
        .task(id: scenePhase == .active && studio.jobs.contains { $0.rdPhase.isActive }) {
            guard scenePhase == .active else { return }
            var failures = 0
            while let delay = studio.progressDelay(failures: failures), !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(delay)) } catch { return }
                await studio.refresh(progressOnly: true)
                failures = studio.failure == nil ? 0 : min(failures + 1, 3)
            }
        }
        .task(id: scenePhase == .active ? studio.nextTransferRetry : nil) {
            guard scenePhase == .active, let retry = studio.nextTransferRetry else { return }
            do { try await Task.sleep(for: .seconds(max(0, retry.timeIntervalSinceNow))) } catch { return }
            studio.retryTransfers()
        }
    }

    private var filterPicker: some View {
        Picker("Show dubs", selection: $filter) {
            ForEach(RDShelfFilter.allCases) { Text($0.title).tag($0) }
        }
    }

    private var usesWindowToolbar: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Dubs").font(.largeTitle.bold()).tracking(-1.5).accessibilityAddTraits(.isHeader)
                Spacer()
                headerActions
            }
            if !selecting && typeSize > .xLarge { addReel }
        }.padding(.horizontal, 18).padding(.bottom, 10).padding(.top, 4)
    }

    @ViewBuilder private var headerActions: some View {
        if selecting {
            Button("Done") { selecting = false; selected = [] }.frame(minHeight: 44)
        } else {
            Menu {
                if !eligible.isEmpty {
                    Button("Select dubs", systemImage: "checkmark.circle") { selecting = true }
                        .accessibilityIdentifier("select-dubs")
                }
                Button("Search library", systemImage: "magnifyingglass") { searching = true }
                Button("Account", systemImage: "person.crop.circle") { sheet = .account }.accessibilityIdentifier("account")
            } label: {
                Image(systemName: "ellipsis.circle").font(.title3).frame(width: 44, height: 44)
            }.accessibilityLabel("Library options").accessibilityIdentifier("library-options")
            if typeSize <= .xLarge { addReel }
        }
    }

    private var addReel: some View {
        Button { sheet = studio.needsSelfHostUnlock ? .selfHost : .composer } label: {
            Label("Add video", systemImage: "plus").font(.headline)
                .padding(.horizontal, 14).padding(.vertical, 7)
                .foregroundStyle(.black).background(leaf, in: Capsule())
                .frame(minHeight: 44)
        }.accessibilityIdentifier("add-reel")
    }

    private var sections: [(title: String, jobs: [Job])] {
        let items = visible
        return [
            ("Processing", items.filter { $0.rdPhase == .running }),
            ("Queued", items.filter { $0.rdPhase == .queued }.sorted { ($0.ahead ?? Int.max) < ($1.ahead ?? Int.max) }),
            ("Ready", items.filter { $0.rdPhase == .done && !studio.isExpired($0) }),
            ("Expired", items.filter { studio.isExpired($0) }),
            ("Failed", items.filter { $0.rdPhase == .failed })
        ]
    }

    private func beginRange(at id: String) -> LibraryDragSelection.Update? {
        let order = eligible
        guard let start = order.firstIndex(of: id) else { return nil }
        let original = selected
        let adding = !original.contains(id)
        return { last in
            guard let end = order.firstIndex(of: last) else { return }
            let range = order[min(start, end)...max(start, end)]
            selected = (adding ? original.union(range) : original.subtracting(range)).intersection(eligible)
        }
    }

    private var transfers: some View {
        let items = visible
        return List {
            if studio.needsSelfHostUnlock {
                Button("Unlock or restore self-hosting") { sheet = .selfHost }
                    .accessibilityIdentifier("self-host-library-unlock")
            }
            if let failure = studio.failure {
                RDNotice(message: failure) { Task { await studio.refresh() } }
                    .listRowBackground(Color.clear)
            }
            if items.isEmpty && studio.failure == nil {
                Group {
                    if !hasLoaded { RDLoading() }
                    else if studio.jobs.isEmpty {
                        ContentUnavailableView {
                            Label("Your first dub", systemImage: "film.stack")
                        } description: { Text("Choose a video or share one from Photos or Files.") }
                        actions: { Button("Add video") { sheet = studio.needsSelfHostUnlock ? .selfHost : .composer } }
                    } else {
                        ContentUnavailableView(query.isEmpty ? filter.emptyTitle : "No matching dubs",
                                               systemImage: query.isEmpty ? "tray" : "magnifyingglass")
                    }
                }.listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            ForEach(sections, id: \.title) { group in section(group.title, group.jobs) }
        }
        .listStyle(.plain).scrollContentBackground(.hidden)
        .scrollDismissesKeyboard(.interactively)
        .refreshable { await studio.refresh(); hasLoaded = true }
    }

    @ViewBuilder private func section(_ title: String, _ jobs: [Job]) -> some View {
        if !jobs.isEmpty {
            Section {
                ForEach(jobs) { job in
                    HStack(spacing: 6) {
                        Button {
                            if !selecting { path.append(job) }
                            else if selected.contains(job.id) { selected.remove(job.id) }
                            else { selected.insert(job.id) }
                        } label: {
                            HStack(spacing: 12) {
                                if selecting {
                                    Image(systemName: selected.contains(job.id) ? "checkmark.circle.fill" : "circle")
                                        .frame(width: 24).foregroundStyle(selected.contains(job.id) ? leaf : .secondary)
                                }
                                TransferRow(job: job, api: studio.api, storage: studio.storageLabel(job))
                            }
                            .contentShape(Rectangle())
                        }
                        .disabled(selecting && job.rdPhase.isActive)
                        .accessibilityAddTraits(selected.contains(job.id) ? .isSelected : [])
                        .buttonStyle(.plain).accessibilityIdentifier("job-" + job.state)
                        if !selecting && job.rdPhase.isActive {
                            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary).accessibilityHidden(true)
                                .allowsHitTesting(false)
                        } else if !selecting {
                            Menu { actions(job) } label: {
                                Image(systemName: "ellipsis").frame(width: 44, height: 44)
                                    .contentShape(Rectangle())
                            }.accessibilityLabel("Actions for " + job.displayTitle)
                        }
                    }
                    .background {
                        if selecting && !job.rdPhase.isActive {
                            LibrarySelectionRow(id: job.id, selection: rangeSelection, begin: beginRange)
                        }
                    }
                    .contextMenu { if !selecting { actions(job) } }
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        if !selecting && !job.rdPhase.isActive {
                            Button("Remove from Library", systemImage: "trash", role: .destructive) { deletion = [job] }
                        }
                    }
                    .listRowInsets(EdgeInsets(top: 9, leading: 18, bottom: 9, trailing: 10))
                    .listRowBackground(canvas).listRowSeparatorTint(.white.opacity(0.12))
                }
            } header: {
                Text("\(title) (\(jobs.count))").font(.headline).textCase(nil)
                    .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
            }
        }
    }

    @ViewBuilder private func actions(_ job: Job) -> some View {
        RDJobMenu(job: job, expired: studio.isExpired(job)) { path.append(job) }
        if !job.rdPhase.isActive {
            Divider()
            Button("Remove from Library", systemImage: "trash", role: .destructive) { deletion = [job] }
        }
    }

    private func remove(_ items: [Job]) {
        for job in items {
            do {
                try studio.removeFromLibrary(job)
                selected.remove(job.id)
                path.removeAll { $0.id == job.id }
            } catch {
                deletionError = error.localizedDescription
                return
            }
        }
        selecting = false; selected = []
        AccessibilityNotification.Announcement(items.count == 1 ? "Dub removed" : "\(items.count) dubs removed").post()
    }
}

private struct TransferRow: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    let job: Job
    let api: API?
    let storage: String?
    private let leaf = Palette.leaf
    var body: some View {
        HStack(spacing: 13) {
            if !typeSize.isAccessibilitySize {
                ZStack {
                    if job.has_poster == true {
                        Poster(job: job, api: api)
                    } else {
                        Rectangle().fill(Color(white: 0.14))
                        if job.rdPhase == .failed {
                            Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                        } else {
                            Image(systemName: job.rdPhase.isActive ? "film" : "play.rectangle")
                                .foregroundStyle(.secondary)
                        }
                    }
                }.frame(width: 50, height: 64).clipShape(RoundedRectangle(cornerRadius: 8))
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(job.displayTitle).font(.subheadline.weight(.semibold)).lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                if let mode = job.lab_mode {
                    Text("Lab · " + mode.capitalized + " · " + (job.lab_runtime ?? "standard")).font(.caption2).foregroundStyle(.secondary)
                }
                switch job.rdPhase {
                case .running, .queued:
                    if let cue = job.recoveryCue {
                        Label(cue, systemImage: "arrow.clockwise").font(.caption.weight(.semibold)).foregroundStyle(.orange)
                    }
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(job.timing(at: context.date)).font(.caption.monospacedDigit()).foregroundStyle(leaf)
                    }
                    if job.rdPhase == .running {
                        if let start = Job.date(job.started), let created = Job.date(job.created) {
                            Text(Job.duration(max(0, Int(start.timeIntervalSince(created)))) + " queued")
                                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        RDActivityBar(moving: true, tint: leaf, progress: job.progress)
                        TimelineView(.periodic(from: .now, by: 1)) { context in
                            Text(job.remaining(at: context.date)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }
                case .done:
                    Text([job.targetName, job.dubVoiceLabel, job.rdByline, job.rdRuntime].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    if let storage { Text(storage).font(.caption2).foregroundStyle(leaf) }
                    Text(job.timing(at: .now)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                case .failed:
                    Text(job.error ?? "Open for details or retry.").font(.caption).foregroundStyle(.orange)
                        .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        .multilineTextAlignment(.leading).contentShape(Rectangle()).accessibilityElement(children: .combine)
    }
}

// MARK: - Composer

@MainActor
struct RDComposer: View {
    let studio: Studio
    let onQueued: () -> Void

    @State private var project: ProjectInput?
    @State private var tempImport: URL?
    @State private var problem: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let account = studio.account {
                if account.video_uploads == true, let modes = account.upload_voice_modes, !(modes["narrator"] ?? []).isEmpty {
                    Text("Choose a video you have permission to translate. It will upload to your connected server for review.")
                        .foregroundStyle(.secondary)
                    VideoImportButton(receive: { movie in
                        tempImport = movie.file
                        project = .file(movie.file, movie.title)
                        problem = nil
                    }, failed: { problem = $0 }, title: "Choose video")
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("choose-video")
                    Text("Up to 2 minutes · 80 MiB")
                        .font(.footnote).foregroundStyle(.secondary)
                } else {
                    Label("This server needs an update to accept video uploads.", systemImage: "arrow.triangle.2.circlepath")
                }
            } else {
                Text("Connect to your server to upload a video.").foregroundStyle(.secondary)
                Button("Check connection") { Task { await studio.refresh() } }
            }
            if let problem { Text(problem).font(.footnote).foregroundStyle(.orange) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(18)
        .sheet(item: $project, onDismiss: {
            if let tempImport { try? FileManager.default.removeItem(at: tempImport); self.tempImport = nil }
        }) { input in
            if let api = studio.api, let account = studio.account {
                QuoteSheet(api: api, input: input, voiceModes: account.upload_voice_modes ?? [:],
                           kittenLanguages: account.kitten_narrator_languages ?? [],
                           kittenMatchLanguages: account.kitten_match_languages ?? [],
                           target: OutputLanguage.read(), creatorMinutes: account.creator_minutes == true,
                           liveOffers: account.purchase_preflight == true,
                           plans: AnyView(CloudPlanSheet(studio: studio))) { _ in
                    if let tempImport { try? FileManager.default.removeItem(at: tempImport); self.tempImport = nil }
                    Task { await studio.refresh() }
                    onQueued()
                }
            }
        }
        .onDisappear {
            if let tempImport, project == nil { try? FileManager.default.removeItem(at: tempImport) }
        }
    }
}

struct ProgressRow: View {
    let job: Job

    var body: some View {
        let phase = job.rdPhase
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                RDStateBadge(phase: phase)
                VStack(alignment: .leading, spacing: 1) {
                    Text(headline(phase))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(phase == .failed ? Color.red : Color.primary)
                        .lineLimit(2)
                    clock(phase)
                }
            }
            if phase.isActive {
                RDActivityBar(moving: phase == .running, progress: phase == .running ? job.progress : nil)
            }
            if phase == .failed, let error = job.error?.trimmingCharacters(in: .whitespacesAndNewlines), !error.isEmpty {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func clock(_ phase: RDPhase) -> some View {
        if phase.isActive {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                if let created = Job.date(job.created) {
                    let start = Job.date(job.started) ?? created
                    if phase == .running {
                        let elapsed = max(0, Int(context.date.timeIntervalSince(start)))
                        let wait = max(0, Int(start.timeIntervalSince(created)))
                        Text("\(Job.duration(elapsed)) elapsed · \(Job.duration(wait)) queued")
                        Text(job.remaining(at: context.date))
                    } else {
                        let wait = max(0, Int(context.date.timeIntervalSince(created)))
                        Text("\(Job.duration(wait)) waiting")
                    }
                }
            }
            .font(.footnote.monospacedDigit())
            .foregroundStyle(.secondary)
        } else {
            Text(job.timing(at: .now))
                .font(.footnote.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }

    private func headline(_ phase: RDPhase) -> String {
        if let cue = job.recoveryCue { return cue }
        switch phase {
        case .queued:
            guard let ahead = job.ahead else { return "Queued" }
            switch ahead {
            case ...0: return "Queued · next up"
            case 1: return "Queued · 1 ahead"
            default: return "Queued · \(ahead) ahead"
            }
        case .running:
            return job.rdStageName
        case .done:
            return job.rdRuntime.map { "Ready · " + $0 } ?? "Ready to watch"
        case .failed:
            return "Dub failed"
        }
    }
}

struct RDStateBadge: View {
    let phase: RDPhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .subheadline) private var size: CGFloat = 26

    var body: some View {
        Image(systemName: phase.symbol)
            .font(.system(size: size * 0.48, weight: .bold))
            .foregroundStyle(phase.tint)
            .symbolEffect(.variableColor.iterative, isActive: phase == .running && !reduceMotion)
            .frame(width: size, height: size)
            .background(phase.tint.opacity(0.15), in: Circle())
            .accessibilityHidden(true)
    }
}

/// Older workers send stage updates without a numeric fraction.
struct RDActivityBar: View {
    let moving: Bool
    var tint: Color = RD.accent
    var progress: Double? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        if let progress, progress.isFinite {
            let fraction = min(1, max(0, progress))
            ProgressView(value: fraction)
                .tint(tint)
                .accessibilityLabel("Render progress")
                .accessibilityValue(fraction.formatted(.percent.precision(.fractionLength(0))))
        } else {
            GeometryReader { geometry in
                let width = geometry.size.width
                ZStack {
                    Capsule().fill(Color.primary.opacity(0.08))
                    if moving {
                        if reduceMotion || scenePhase != .active {
                            HStack(spacing: 3) {
                                ForEach(0..<3) { _ in Circle().fill(tint).frame(width: 3, height: 3) }
                            }
                        } else {
                            let segment = min(width, max(width * 0.3, 16))
                            Capsule().fill(tint.opacity(0.7)).frame(width: segment)
                                .phaseAnimator([false, true]) { view, forward in
                                    view.offset(x: (forward ? 1 : -1) * (width - segment) / 2)
                                } animation: { _ in .easeInOut(duration: 1.6) }
                        }
                    }
                }
                .clipShape(Capsule())
            }
            .frame(height: 4)
            .accessibilityHidden(true)
        }
    }
}

// MARK: - Posters

/// Real JPEG poster when the server has one, otherwise a deterministic generated cover.
/// Fills whatever frame the caller gives it.
@MainActor
struct Poster: View {
    let job: Job
    let api: API?

    @State private var loaded: RDLoadedPoster?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var wants: Bool { job.has_poster == true && api != nil }

    var body: some View {
        let key = RDPosterStore.key(for: job.id, api: api)
        let image = loaded?.key == key ? loaded?.image : (wants ? RDPosterStore.shared.cached(key) : nil)

        Color.clear
            .overlay {
                if let image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFill()
                        .transition(.opacity)
                } else {
                    RDFallbackArt(seed: job.id)
                }
            }
            .clipped()
            .accessibilityHidden(true)
            .task(id: key + (wants ? "|1" : "|0")) { await load(key) }
    }

    private func load(_ key: String) async {
        guard wants, let api, loaded?.key != key else { return }
        if let hit = RDPosterStore.shared.cached(key) {
            loaded = RDLoadedPoster(key: key, image: hit)
            return
        }
        guard let image = await RDPosterStore.shared.image(for: job.id, key: key, api: api), !Task.isCancelled else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.3)) {
            loaded = RDLoadedPoster(key: key, image: image)
        }
    }
}

struct RDLoadedPoster {
    let key: String
    let image: UIImage
}

/// Shared poster cache. One download per poster (in-flight requests are joined),
/// decoded and downsampled off the main thread, scoped per server + account so
/// switching accounts never shows another account's art.
@MainActor
final class RDPosterStore {
    static let shared = RDPosterStore()

    private let cache = NSCache<NSString, UIImage>()
    private var inflight: [String: Task<UIImage?, Never>] = [:]
    private let misses = NSCache<NSString, NSDate>()

    init() {
        misses.countLimit = 240
        cache.countLimit = 240
        cache.totalCostLimit = 128 * 1024 * 1024
    }

    static func key(for id: String, api: API?) -> String {
        guard let connection = api?.connection else { return "offline/" + id }
        let digest = SHA256.hash(data: Data(connection.token.utf8))
        return connection.base.absoluteString + "|" + Data(digest).base64EncodedString() + "|" + id
    }

    func remove(_ id: String, api: API) {
        let key = Self.key(for: id, api: api)
        inflight[key]?.cancel()
        cache.removeObject(forKey: key as NSString)
        misses.removeObject(forKey: key as NSString)
    }

    func cached(_ key: String) -> UIImage? {
        cache.object(forKey: key as NSString)
    }

    func image(for id: String, key k: String, api: API) async -> UIImage? {
        if let hit = cache.object(forKey: k as NSString) { return hit }
        if let running = inflight[k] { return await running.value }
        if let miss = misses.object(forKey: k as NSString), miss.timeIntervalSinceNow > -30 { return nil }

        // A saved movie's directory is private to this account and is removed with the movie.
        // Never create a directory here. A late download must not resurrect deleted local data.
        let poster = DeviceLibrary(connection: api.connection)?.poster(id)
        let task = Task.detached(priority: .utility) { () -> UIImage? in
            if let poster, let data = try? Data(contentsOf: poster), let image = RDPosterStore.decode(data) {
                return image
            }
            guard let data = try? await api.data("v1/jobs/\(id)/poster"), !Task.isCancelled else { return nil }
            guard let image = RDPosterStore.decode(data) else { return nil }
            if let poster, data.count <= 512_000 {
                try? data.write(to: poster, options: [.atomic, .completeFileProtection])
            }
            return image
        }
        inflight[k] = task
        let image = await task.value

        inflight[k] = nil
        guard !task.isCancelled else { return nil }
        if let image {
            let pixels = image.size.width * image.scale * image.size.height * image.scale
            cache.setObject(image, forKey: k as NSString, cost: Int(pixels * 4))
        } else {
            misses.setObject(NSDate(), forKey: k as NSString)
        }
        return image
    }

    nonisolated static func decode(_ data: Data) -> UIImage? {
        guard let image = UIImage(data: data) else { return nil }
        let pixelW = image.size.width * image.scale
        let pixelH = image.size.height * image.scale
        let longest = max(pixelW, pixelH)
        if longest > 1280 {
            let factor = 1280 / longest
            return image.preparingThumbnail(of: CGSize(width: pixelW * factor, height: pixelH * factor)) ?? image
        }
        return image.preparingForDisplay() ?? image
    }
}

/// Generated cover: a duotone field with a waveform unique to the job.
struct RDFallbackArt: View {
    let seed: String
    var colors: [Color]? = nil

    var body: some View {
        let hash = RDHash.stable(seed)
        let palette = colors ?? RD.palettes[Int(hash % UInt64(RD.palettes.count))]
        let glowX = 0.15 + Double((hash >> 8) & 0xFF) / 255 * 0.7

        ZStack {
            LinearGradient(colors: palette, startPoint: .topLeading, endPoint: .bottomTrailing)
            RadialGradient(colors: [.white.opacity(0.24), .clear],
                           center: UnitPoint(x: glowX, y: 0.18),
                           startRadius: 0, endRadius: 260)
            Canvas { context, size in
                var rng = RDRandom(state: hash)
                let count = 13
                let span = size.width * 0.7
                let gap = span * 0.035
                let bar = (span - gap * CGFloat(count - 1)) / CGFloat(count)
                let x0 = (size.width - span) / 2
                let midY = size.height * 0.64
                let maxH = min(size.height * 0.24, size.width * 0.5)
                for i in 0..<count {
                    let envelope = 0.35 + 0.65 * sin(Double(i) / Double(count - 1) * .pi)
                    let h = max(bar, maxH * CGFloat(envelope * (0.3 + 0.7 * rng.unit())))
                    let rect = CGRect(x: x0 + CGFloat(i) * (bar + gap), y: midY - h / 2, width: bar, height: h)
                    context.fill(Path(roundedRect: rect, cornerRadius: bar / 2), with: .color(.white.opacity(0.6)))
                }
            }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - Empty, loading & notices

struct RDLoading: View {
    var body: some View {
        DubLoadingIndicator("Loading your library")
            .frame(maxWidth: .infinity)
            .padding(.vertical, 72)
    }
}

struct RDNotice: View {
    let message: String
    let retry: () -> Void
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12)) : AnyLayout(HStackLayout(alignment: .top, spacing: 12))
        layout {
            VStack(alignment: .leading, spacing: 4) {
                Label("Couldn’t refresh", systemImage: "exclamationmark.icloud")
                    .font(.subheadline.weight(.semibold))
                Text(message).font(.footnote).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            Button("Retry", action: retry).buttonStyle(.bordered)
                .font(.subheadline.weight(.semibold))
        }
        .padding(14)
        .background(RDCard())
    }
}

// MARK: - Small components

struct RDSearchField: View {
    @Binding var text: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            TextField("Search titles and creators", text: $text)
                .submitLabel(.search)
                .autocorrectionDisabled()
            if !text.isEmpty {
                Button {
                    text = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.tertiary)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear search")
            }
        }
        .padding(.leading, 12)
        .frame(minHeight: 44)
        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

struct RDJobMenu: View {
    let job: Job
    let expired: Bool
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            switch job.rdPhase {
            case .done: Label(expired ? "View details" : "Watch", systemImage: expired ? "info.circle" : "play.fill")
            case .failed: Label("Details & Report Issue", systemImage: "exclamationmark.bubble")
            default: Label("Show Progress", systemImage: "waveform")
            }
        }
        if Job.isReel(job.url), let url = URL(string: job.url) {
            Link(destination: url) {
                Label("Open in Instagram", systemImage: "arrow.up.forward.app")
            }
            ShareLink(item: url) {
                Label("Share Reel Link", systemImage: "square.and.arrow.up")
            }
            Button { UIPasteboard.general.string = job.url } label: { Label("Copy Reel Link", systemImage: "link") }
        }
    }
}

struct RDCard: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 22, style: .continuous)
            .fill(Color(.secondarySystemGroupedBackground))
    }
}

struct RDPressStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.97 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(reduceMotion ? nil : .spring(response: 0.25, dampingFraction: 0.8), value: configuration.isPressed)
    }
}

// MARK: - Model helpers

enum RDPhase: String {
    case queued, running, done, failed

    var isActive: Bool { self == .queued || self == .running }

    var symbol: String {
        switch self {
        case .queued: "hourglass"
        case .running: "waveform"
        case .done: "checkmark"
        case .failed: "exclamationmark"
        }
    }

    var tint: Color {
        switch self {
        case .queued: .secondary
        case .running: RD.accent
        case .done: .green
        case .failed: .red
        }
    }
}

enum RDShelfFilter: String, CaseIterable, Identifiable {
    case all = "All", active = "Active", ready = "Ready", failed = "Failed"
    var id: Self { self }
    var title: String { rawValue }
    var emptyTitle: String {
        switch self {
        case .all: "No dubs"
        case .active: "No active translations"
        case .ready: "No ready dubs"
        case .failed: "No failed dubs"
        }
    }
    func admits(_ job: Job) -> Bool {
        switch self {
        case .all: true
        case .active: job.rdPhase.isActive
        case .ready: job.rdPhase == .done
        case .failed: job.rdPhase == .failed
        }
    }
}

extension Job {
    var rdPhase: RDPhase { RDPhase(rawValue: state) ?? .queued }

    var rdByline: String {
        let name = (author ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return Job.isReel(url) ? "Instagram Reel" : "Uploaded video" }
        return name.hasPrefix("@") || name.contains(" ") ? name : "@" + name
    }

    var rdRuntime: String? {
        guard let seconds = media_seconds, seconds.isFinite, seconds > 0 else { return nil }
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }

    var rdStageName: String {
        let raw = stage
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty, !["queued", "running", "done", "failed"].contains(raw.lowercased()) else {
            return "Dubbing"
        }
        return raw.prefix(1).uppercased() + raw.dropFirst()
    }


}

// MARK: - Theme & utilities

enum RD {
    static let accent = Palette.action

    static let palettes: [[Color]] = [
        [Color(red: 0.17, green: 0.11, blue: 0.33), Color(red: 0.89, green: 0.33, blue: 0.48)],
        [Color(red: 0.06, green: 0.23, blue: 0.30), Color(red: 0.25, green: 0.72, blue: 0.69)],
        [Color(red: 0.23, green: 0.11, blue: 0.05), Color(red: 0.94, green: 0.54, blue: 0.24)],
        [Color(red: 0.11, green: 0.17, blue: 0.39), Color(red: 0.42, green: 0.55, blue: 1.00)],
        [Color(red: 0.12, green: 0.24, blue: 0.18), Color(red: 0.61, green: 0.80, blue: 0.42)],
        [Color(red: 0.29, green: 0.07, blue: 0.19), Color(red: 1.00, green: 0.48, blue: 0.35)],
        [Color(red: 0.13, green: 0.13, blue: 0.23), Color(red: 0.60, green: 0.55, blue: 0.62)],
    ]
}

enum RDHash {
    /// FNV-1a — stable across launches (unlike `hashValue`).
    static func stable(_ string: String) -> UInt64 {
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in string.utf8 {
            hash ^= UInt64(byte)
            hash &*= 0x0000_0100_0000_01b3
        }
        return hash
    }
}

struct RDRandom {
    var state: UInt64

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }

    mutating func unit() -> Double {
        Double(next() >> 11) / Double(UInt64(1) << 53)
    }
}
