import SwiftUI

struct Home: View {
    let studio: Studio
    @State private var settings = false
    @State private var url = ""
    @State private var issue: String?
    @State private var detail: Job?
    @Environment(\.scenePhase) private var phase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var type
    @FocusState private var editing: Bool
    private var working: [Job] { studio.jobs.filter { ["running", "queued"].contains($0.state) } }
    private var library: [Job] { studio.jobs.filter { !["running", "queued"].contains($0.state) } }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    HStack {
                        HStack(spacing: 7) {
                            Image("brand-mark").resizable().frame(width: 29, height: 29).clipShape(RoundedRectangle(cornerRadius: 8))
                            Text("ReelDub").font(.headline)
                        }.accessibilityElement(children: .combine)
                        Spacer()
                        Button { settings = true } label: {
                            Image(systemName: "person.crop.circle").font(.title2).frame(width: 44, height: 44)
                        }.accessibilityLabel("Account")
                    }
                    VStack(alignment: .leading, spacing: 13) {
                        Text("Dubs").font(.system(.largeTitle, design: .serif, weight: .semibold))
                            .accessibilityAddTraits(.isHeader)
                        HStack(spacing: 8) {
                            Image(systemName: "link").foregroundStyle(.secondary).accessibilityHidden(true)
                            TextField("Instagram Reel link", text: $url)
                                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                                .focused($editing).onSubmit { Task { await submit() } }
                            PasteButton(payloadType: String.self) { values in
                                if let value = values.first { url = value; issue = nil }
                            }.labelStyle(.iconOnly).tint(.secondary).frame(minWidth: 44, minHeight: 44)
                        }.padding(.horizontal, 12).frame(minHeight: 53)
                            .background(Palette.surface, in: RoundedRectangle(cornerRadius: 14))
                        Button { Task { await submit() } } label: {
                            HStack {
                                Text(studio.sending ? "Queuing Reel…" : "Dub in English").font(.headline)
                                Spacer()
                                if studio.sending { ProgressView() } else { Image(systemName: "arrow.up.right") }
                            }.padding(.horizontal, 17).frame(minHeight: 51)
                                .foregroundStyle(Palette.ink).background(Palette.ember, in: RoundedRectangle(cornerRadius: 14))
                        }.disabled(studio.sending)
                        if let issue { Text(issue).font(.footnote).foregroundStyle(.red).accessibilityLabel("Reel link error. " + issue) }
                    }
                    if let error = studio.failure { Text(error).font(.footnote).foregroundStyle(.red) }
                    if !working.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            sectionTitle("In progress")
                            ForEach(working) { job in
                                Button { detail = job } label: { ProgressRow(job: job) }
                                    .buttonStyle(.plain).accessibilityIdentifier("job-" + job.state)
                            }
                        }
                    }
                    VStack(alignment: .leading, spacing: 15) {
                        sectionTitle("Library")
                        if library.isEmpty {
                            VStack(alignment: .leading, spacing: 9) {
                                Image(systemName: "rectangle.stack").font(.title2)
                                Text("Your dubs will appear here.").font(.headline)
                                Text("In Instagram, tap Share → ReelDub, then confirm. You can also paste a Reel link above.")
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(18)
                                .background(Palette.surface, in: RoundedRectangle(cornerRadius: 16))
                        } else {
                            LazyVGrid(columns: [GridItem(.adaptive(minimum: type.isAccessibilitySize ? 260 : 147), spacing: 15)], alignment: .leading, spacing: 21) {
                                ForEach(library) { job in
                                    Button { detail = job } label: { DubTile(job: job, api: studio.api) }
                                        .buttonStyle(.plain).accessibilityIdentifier("job-" + job.state)
                                }
                            }
                        }
                    }
                }.frame(maxWidth: 760).padding(.horizontal, 21).padding(.vertical, 8)
                    .frame(maxWidth: .infinity)
            }.background(Palette.canvas).scrollDismissesKeyboard(.interactively)
                .refreshable { await studio.refresh() }
                .navigationDestination(item: $detail) { job in JobView(studio: studio, initial: job) }
        }.sheet(isPresented: $settings) { SettingsView(studio: studio) }
            .task(id: "\(phase == .active)-\(studio.connection?.base.absoluteString ?? "")-\(studio.connection?.token.hashValue ?? 0)") {
                guard phase == .active else { return }
                while !Task.isCancelled, studio.api != nil {
                    await studio.refresh()
                    try? await Task.sleep(for: .seconds(working.isEmpty ? 8 : 2))
                }
            }.animation(reduceMotion ? nil : .easeOut(duration: 0.23), value: working.map(\.id))
    }

    private func sectionTitle(_ title: String) -> some View {
        Text(title).font(.title2.weight(.semibold)).accessibilityAddTraits(.isHeader)
    }
    private func submit() async {
        guard !studio.sending else { return }
        let link = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Job.isReel(link) else {
            issue = link.isEmpty ? "Paste an Instagram Reel link to get started." : "That link doesn’t point to an Instagram Reel."
            editing = true
            return
        }
        editing = false
        issue = nil
        do { try await studio.submit(link); url = "" }
        catch { issue = error.localizedDescription }
    }
}

struct ProgressRow: View {
    let job: Job
    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: job.state == "queued" ? "clock" : "waveform")
                .font(.title3.weight(.medium)).frame(width: 44, height: 46)
                .foregroundStyle(Palette.ember).background(Palette.ink, in: RoundedRectangle(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 4) {
                Text(job.displayTitle).font(.subheadline.weight(.semibold)).lineLimit(1)
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text(job.timing(at: context.date)).font(.footnote).monospacedDigit().foregroundStyle(.secondary)
                }
            }
            Spacer()
            Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
        }.padding(13).background(Palette.surface, in: RoundedRectangle(cornerRadius: 15))
            .accessibilityElement(children: .ignore).accessibilityLabel(job.displayTitle + ", " + job.timing(at: Date()))
    }
}

struct Poster: View {
    let job: Job
    let api: API?
    @State private var image: UIImage?
    var body: some View {
        ZStack {
            Color(red: 0.16, green: 0.14, blue: 0.13)
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: "film.stack").font(.largeTitle.weight(.light)).foregroundStyle(.white.opacity(0.64)) }
        }.clipped().accessibilityHidden(true)
            .task(id: "\(job.id)-\(job.has_poster == true)") {
                image = nil
                guard job.has_poster == true, let api else { return }
                if let bytes = try? await api.data("v1/jobs/\(job.id)/poster"), !Task.isCancelled {
                    image = UIImage(data: bytes)
                }
            }
    }
}

struct DubTile: View {
    let job: Job
    let api: API?
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Color.clear.aspectRatio(0.78, contentMode: .fit)
                .overlay { Poster(job: job, api: api) }
                .overlay(alignment: .bottomLeading) {
                    HStack(spacing: 5) {
                        Image(systemName: job.state == "failed" ? "exclamationmark.circle" : "play.fill")
                        if let duration = job.media_seconds { Text(Job.duration(Int(duration.rounded()))) }
                    }.font(.caption.weight(.bold)).monospacedDigit().foregroundStyle(.white)
                        .padding(.horizontal, 9).padding(.vertical, 6).background(.black.opacity(0.75), in: Capsule()).padding(9)
                }.clipShape(RoundedRectangle(cornerRadius: 14))
            Text(job.displayTitle).font(.subheadline.weight(.semibold)).lineLimit(2)
            Text(job.state == "failed" ? "Couldn’t dub" : "English dub").font(.footnote).foregroundStyle(.secondary)
        }.accessibilityElement(children: .ignore).accessibilityLabel(job.displayTitle + (job.state == "failed" ? ", failed" : ", play English dub"))
    }
}
