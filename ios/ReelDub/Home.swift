import SwiftUI

enum Ink {
    static let plum = Color(red: 0.23, green: 0.14, blue: 0.25)
    static let berry = Color(red: 0.53, green: 0.18, blue: 0.35)
    static let linen = Color(uiColor: UIColor { $0.userInterfaceStyle == .dark ? .systemBackground : UIColor(red: 0.98, green: 0.97, blue: 0.95, alpha: 1) })
    static let card = Color(uiColor: .secondarySystemGroupedBackground)
}
struct Home: View {
    @Bindable var studio: Studio
    @State private var settings = false
    @State private var url = ""
    @State private var message: String?
    @State private var selection = 0
    @State private var detail: Job?
    @Environment(\.scenePhase) private var phase
    @Environment(\.accessibilityReduceMotion) private var reduced

    var body: some View {
        TabView(selection: $selection) {
            screen(library: false).tabItem { Label("Studio", image: "intake") }.tag(0)
            screen(library: true).tabItem { Label("Library", image: "library") }.tag(1)
        }.tint(Ink.berry)
        .sheet(isPresented: $settings) { SettingsView(studio: studio) }
        .sheet(item: $detail) { job in JobView(studio: studio, initial: job) }
        .alert("ReelDub", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("OK") { message = nil }
        } message: { Text(message ?? "") }
        .task(id: "\(phase == .active)-\(studio.connection?.base.absoluteString ?? "")") {
            guard phase == .active, studio.api != nil else { return }
            while !Task.isCancelled {
                await studio.refresh()
                try? await Task.sleep(for: .seconds(studio.jobs.contains { ["queued", "running"].contains($0.state) } ? 2 : 8))
            }
        }
    }
    private func screen(library: Bool) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    HStack {
                        Image("brand-mark").resizable().frame(width: 35, height: 35).clipShape(RoundedRectangle(cornerRadius: 10))
                        Text("ReelDub").font(.system(.title2, design: .serif, weight: .bold))
                        Spacer()
                        Button { settings = true } label: { Image(systemName: "slider.horizontal.3").frame(width: 44, height: 44).background(Ink.card, in: Circle()) }
                            .accessibilityLabel("Server and account")
                    }
                    if !library { intake }
                    HStack(alignment: .lastTextBaseline) {
                        Text(library ? "Your library" : "Recent dubs").font(.system(.title, design: .serif, weight: .semibold))
                        Spacer()
                        if let a = studio.account { Text(a.limit.map { "\(a.used)/\($0) today" } ?? "Admin").font(.caption.bold()).foregroundStyle(.secondary) }
                    }
                    if let failure = studio.failure { Text(failure).font(.footnote).foregroundStyle(.red) }
                    if studio.jobs.isEmpty {
                        HStack(spacing: 14) {
                            Image("library").foregroundStyle(Ink.berry).frame(width: 40, height: 44)
                            VStack(alignment: .leading, spacing: 4) {
                                Text("No dubs yet").font(.headline)
                                Text("Share a Reel from Instagram or paste its link here.").font(.subheadline).foregroundStyle(.secondary)
                            }
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading).background(Ink.card, in: RoundedRectangle(cornerRadius: 18))
                    } else {
                        LazyVStack(spacing: 9) {
                            ForEach(Array(library ? studio.jobs[...] : studio.jobs.prefix(5))) { job in
                                Button { detail = job } label: { JobRow(job: job) }.buttonStyle(.plain)
                                    .accessibilityIdentifier("job-" + job.state)
                            }
                        }.animation(reduced ? nil : .smooth(duration: 0.28), value: studio.jobs.map(\.id))
                    }
                }.padding(20)
            }.background(Ink.linen).refreshable { await studio.refresh() }
        }
    }
    private var intake: some View {
        VStack(alignment: .leading, spacing: 15) {
            ZStack(alignment: .bottomLeading) {
                Color.clear.overlay { Image("sound-ribbon").resizable().scaledToFill().accessibilityHidden(true) }.clipped()
                LinearGradient(colors: [.black.opacity(0.46), .clear], startPoint: .leading, endPoint: .trailing)
                Text("Dub a Reel").font(.system(.largeTitle, design: .serif, weight: .bold)).foregroundStyle(.white).padding(18)
            }.frame(height: 148).clipped().accessibilityElement(children: .combine)
            VStack(spacing: 12) {
                HStack {
                    Image(systemName: "link").foregroundStyle(Ink.berry)
                    TextField("Instagram Reel link", text: $url).textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    PasteButton(payloadType: String.self) { values in if let value = values.first { url = value } }.labelStyle(.iconOnly).scaleEffect(0.82)
                }.padding(.horizontal, 12).frame(minHeight: 46).background(Ink.linen, in: RoundedRectangle(cornerRadius: 12))
                Button {
                    if studio.api == nil { settings = true; return }
                    Task { do { try await studio.submit(url); url = "" } catch { message = error.localizedDescription } }
                } label: {
                    HStack { if studio.sending { ProgressView().tint(.white) }
                        Text(studio.api == nil ? "Connect a server" : "Translate to English").font(.headline)
                        Spacer(); Image(systemName: "arrow.right")
                    }.padding(.horizontal, 16).frame(minHeight: 48).foregroundStyle(.white).background(Ink.plum, in: RoundedRectangle(cornerRadius: 13))
                }.disabled(studio.sending || (studio.api != nil && url.isEmpty))
            }.padding([.horizontal, .bottom], 15)
        }.background(Ink.card, in: RoundedRectangle(cornerRadius: 23)).clipShape(RoundedRectangle(cornerRadius: 23))
    }
}
struct JobRow: View {
    let job: Job
    var body: some View {
        HStack(spacing: 13) {
            Image(systemName: job.state == "done" ? "play.fill" : job.state == "failed" ? "exclamationmark" : "waveform")
                .font(.headline).foregroundStyle(Ink.berry).frame(width: 44, height: 52).background(Ink.berry.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 5) {
                Text("Reel · " + job.code).font(.subheadline.bold()).lineLimit(1)
                TimelineView(.periodic(from: .now, by: 1)) { tick in
                    Text(job.timing(at: tick.date)).font(.caption).monospacedDigit().foregroundStyle(.secondary)
                }
            }; Spacer()
            Text(job.state == "done" ? "Ready" : job.state.capitalized).font(.caption2.bold()).foregroundStyle(job.state == "failed" ? .red : .secondary)
        }.padding(12).background(Ink.card, in: RoundedRectangle(cornerRadius: 16))
    }
}
