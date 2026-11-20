import SwiftUI

private struct Overview: Decodable {
  struct Pair: Decodable {
    let source, target: String
    let count: Int
  }
  struct Speed: Decodable {
    let mode: String
    let count: Int
    let average: Double
    let per_minute: Double?
  }
  let states: [String: Int], cache_hits: Int, reports: Int, avg_wait: Double?
  struct GPU: Decodable {
    let name, sampled: String
    let load, used_mib, total_mib, temperature: Int
    let watts: Double?
  }
  let gpu: GPU?
  let accounts, banned, signed_in_30d: Int?
  let languages: [Pair], speeds: [Speed]
}

private struct Activity: Decodable, Identifiable {
  struct Report: Decodable { let category, note, status: String? }
  let id, account, state, created: String
  let title, url, source_language, target_language, lab_mode, stage, error, started,
    finished: String?
  let asr_model, voice_model, translation_model, media_expires: String?
  let media_seconds, quoted_seconds, progress: Double?
  let last_requested: String?
  let lab_runtime, lab_separation: String?
  let cache_hits, voice_batch, priority, attempt: Int?
  let report: Report?
}
private struct OperatorAccount: Decodable, Identifiable {
  let id: String
  let tier, email, last_sign_in, moderation_reason: String?
  struct Event: Decodable {
    let at, reason: String
    let ban: Bool
  }
  let moderation_history: [Event]?
  let protected: Bool?
  let banned: Bool?
  var label: String { email ?? String(id.suffix(12)) }
}
private struct Rows<T: Decodable>: Decodable {
  let rows: [T]
  let more: Bool
}
private struct Cursor: Encodable {
  var before: String? = nil
  var after_id: String? = nil
  var account: String? = nil
  var reports_only: Bool? = nil
}
private enum AdminTab: String, CaseIterable {
  case overview = "Overview"
  case activity = "Requests"
  case reports = "Reports"
  case accounts = "Accounts"
}

struct AdminConsole: View {
  let studio: Studio
  @Environment(\.scenePhase) private var phase
  @State private var grant: String?
  @State private var expires = Date.distantPast
  @State private var busy = false
  @State private var issue: String?

  var body: some View {
    Group {
      if studio.connection?.isHosted == true, studio.account?.admin_console == true {
        if let grant, Date() < expires, let connection = studio.connection {
          ConsoleContent(
            studio: studio,
            api: API(
              connection: connection, adminGrant: grant,
              adminExpired: {
                if self.grant == grant { self.grant = nil }
              }))
        } else {
          Form {
            Section {
              AppleButton(studio: studio, white: false, busy: $busy, issue: $issue) {
                identity, challenge, _ in
                guard let api = studio.api else { throw SessionError.expired }
                let data = try await api.data(
                  "v1/admin/unlock", method: "POST",
                  body: ["identity_token": identity, "challenge": challenge])
                guard studio.connection?.token == api.connection.token else {
                  throw SessionError.expired
                }
                let key = try JSONDecoder().decode(SessionKey.self, from: data)
                expires = Date().addingTimeInterval(890)
                grant = key.token
              }.frame(height: 46)
              if let issue { Text(issue).foregroundStyle(.red) }
            } header: {
              Text("Verify administrator access")
            } footer: {
              Text("Unlocks account activity, moderation and the render lab for 15 minutes.")
            }
          }
        }
      } else {
        Color.clear
      }
    }.opacity(phase == .active ? 1 : 0).privacySensitive().navigationTitle("Admin console")
      .task(id: grant) {
        guard grant != nil else { return }
        do {
          try await Task.sleep(for: .seconds(max(0, expires.timeIntervalSinceNow)))
          grant = nil
        } catch {}
      }
      .onChange(of: studio.connection?.token) { _, _ in grant = nil }
  }
}

private struct ConsoleContent: View {
  let studio: Studio
  let api: API
  @Environment(\.scenePhase) private var phase
  @State private var tab: AdminTab = .overview
  @State private var summary: Overview?
  @State private var jobs: [Activity] = []
  @State private var accounts: [OperatorAccount] = []
  @State private var more = false
  @State private var busy = false
  @State private var error: String?
  @State private var generation = UUID()

  var body: some View {
    Group {
      if phase == .active, studio.account?.admin_console == true {
        List {
          Section {
            NavigationLink {
              RenderLab(studio: studio, api: api)
            } label: {
              Label("Render lab", systemImage: "slider.horizontal.3")
            }
          }
          Picker("View", selection: $tab) {
            ForEach(AdminTab.allCases, id: \.self) { Text($0.rawValue).tag($0) }
          }.pickerStyle(.segmented)
          if let error { Text(error).foregroundStyle(.red) }
          switch tab {
          case .overview:
            if let summary {
              if let total = summary.accounts {
                Section("Accounts") {
                  LabeledContent("Total accounts", value: String(total))
                  if let recent = summary.signed_in_30d {
                    LabeledContent("Signed in within 30 days", value: String(recent))
                  }
                  if let banned = summary.banned {
                    LabeledContent("Suspended", value: String(banned))
                  }
                }
              }
              Section("Retained requests") {
                ForEach(summary.states.keys.sorted(), id: \.self) { key in
                  LabeledContent(key.capitalized, value: String(summary.states[key] ?? 0))
                }
                LabeledContent("Measured cache hits", value: String(summary.cache_hits))
                LabeledContent("Issue reports", value: String(summary.reports))
                if let wait = summary.avg_wait {
                  LabeledContent("Average queue wait", value: Job.duration(Int(wait.rounded())))
                }
              }
              Section("Completed renders") {
                ForEach(summary.speeds, id: \.mode) { speed in
                  VStack(alignment: .leading, spacing: 4) {
                    Text(
                      speed.mode.capitalized + " · " + Job.duration(Int(speed.average.rounded()))
                        + " average")
                    Text(
                      "\(speed.count) renders"
                        + (speed.per_minute.map { " · \(Int($0.rounded()))s per minute of video" }
                          ?? "")
                    ).font(.caption).foregroundStyle(.secondary)
                  }
                }
              }
              if let gpu = summary.gpu {
                Section(gpu.name) {
                  LabeledContent("GPU utilization", value: "\(gpu.load)%")
                  LabeledContent(
                    "VRAM",
                    value: String(
                      format: "%.1f / %.1f GiB", Double(gpu.used_mib) / 1024,
                      Double(gpu.total_mib) / 1024))
                  LabeledContent("Temperature", value: "\(gpu.temperature) °C")
                  if let watts = gpu.watts {
                    LabeledContent("GPU power", value: "\(Int(watts.rounded())) W")
                  }
                  if let sampled = Job.date(gpu.sampled) {
                    LabeledContent(
                      "Sampled", value: sampled.formatted(date: .omitted, time: .standard))
                  }
                }
              }
              Section("Language pairs") {
                ForEach(summary.languages, id: \.selfKey) { pair in
                  LabeledContent(pair.label, value: String(pair.count))
                }
              }
            }
          case .activity, .reports:
            ForEach(jobs) { item in
              NavigationLink {
                AdminRequestDetails(job: item)
              } label: {
                VStack(alignment: .leading, spacing: 4) {
                  Text(item.title ?? item.url ?? "Video upload").lineLimit(2)
                  Text(
                    "\(item.state.capitalized) · \(item.lab_mode ?? "standard") · \(OutputLanguage.name(item.target_language ?? "en"))"
                  )
                  .font(.caption).foregroundStyle(.secondary)
                }
              }
            }
          case .accounts:
            ForEach(accounts) { account in
              NavigationLink {
                AdminAccountView(api: api, account: account) { await load() }
              } label: {
                VStack(alignment: .leading) {
                  Text(account.label)
                  Text(
                    account.banned == true
                      ? "Banned" : account.protected == true ? "Console owner" : "Active"
                  )
                  .font(.caption).foregroundStyle(.secondary)
                }
              }
            }
          }
          if more, tab != .overview {
            Button("Load older entries") { Task { await load(append: true) } }.disabled(busy)
          }
          if busy { ProgressView() }
        }.refreshable { await load() }
      } else {
        Color.clear
      }
    }.privacySensitive().navigationTitle("Admin console")
      .task(id: tab) { await load() }
  }

  @MainActor private func load(append: Bool = false) async {
    guard studio.account?.admin_console == true else { return }
    let ticket = UUID()
    generation = ticket
    busy = true
    error = nil
    if !append {
      more = false
      jobs = []
      accounts = []
    }
    func current() -> Bool {
      generation == ticket && studio.connection?.token == api.connection.token && !Task.isCancelled
    }
    defer { if generation == ticket { busy = false } }
    do {
      switch tab {
      case .overview:
        let value = try JSONDecoder().decode(
          Overview.self, from: await api.data("v1/admin/overview"))
        if current() { summary = value }
      case .activity, .reports:
        let last = append ? jobs.last : nil
        let data = try await api.post(
          "v1/admin/activity",
          Cursor(before: last?.created, after_id: last?.id, reports_only: tab == .reports))
        let page = try JSONDecoder().decode(Rows<Activity>.self, from: data)
        if current() {
          jobs = (append ? jobs : []) + page.rows
          more = page.more
        }
      case .accounts:
        let data = try await api.post(
          "v1/admin/accounts", Cursor(after_id: append ? accounts.last?.id : nil))
        let page = try JSONDecoder().decode(Rows<OperatorAccount>.self, from: data)
        if current() {
          accounts = (append ? accounts : []) + page.rows
          more = page.more
        }
      }
    } catch { if current() { self.error = error.localizedDescription } }
  }
}
extension Overview.Pair {
  fileprivate var selfKey: String { source + "/" + target }
  fileprivate var label: String {
    (source == "unknown" ? "Undetected" : OutputLanguage.name(source)) + " → "
      + OutputLanguage.name(target)
  }
}

private struct AdminRequestDetails: View {
  let job: Activity
  var body: some View {
    List {
      Section("Request") {
        Text(job.title ?? "Dub request")
        if let raw = job.url, Job.isReel(raw), let url = URL(string: raw) {
          Link("Open original Reel", destination: url)
        }
        LabeledContent("Account", value: String(job.account.suffix(12)))
        LabeledContent("Submitted", value: Job.date(job.created)?.formatted() ?? job.created)
        LabeledContent("Status", value: job.stage ?? job.state)
        LabeledContent("Queue", value: job.priority == 1 ? "Priority" : "Free")
        LabeledContent(
          "Languages", value: (job.source_language ?? "?") + " → " + (job.target_language ?? "en"))
        if let seconds = job.media_seconds ?? job.quoted_seconds {
          LabeledContent("Video length", value: Job.duration(Int(seconds.rounded())))
        }
        if let progress = job.progress, job.state == "running" {
          ProgressView(value: max(0, min(1, progress)))
        }
        if let attempt = job.attempt, attempt > 0 {
          LabeledContent("Render attempts", value: String(attempt))
        }
        if let requested = Job.date(job.last_requested) {
          LabeledContent("Last requested", value: requested.formatted())
        }
        if let count = job.cache_hits { LabeledContent("Cache hits", value: String(count)) }
        if let expires = Job.date(job.media_expires) {
          LabeledContent("Cache expiry", value: expires.formatted())
        }
        if let start = Job.date(job.started), let created = Job.date(job.created) {
          LabeledContent(
            "Queued", value: Job.duration(max(0, Int(start.timeIntervalSince(created)))))
          if let end = Job.date(job.finished) {
            LabeledContent(
              "Processing", value: Job.duration(max(0, Int(end.timeIntervalSince(start)))))
          }
        }
        if let error = job.error { Text(error).foregroundStyle(.red) }
      }
      Section("Render settings") {
        LabeledContent("Mode", value: (job.lab_mode ?? "standard").capitalized)
        if let runtime = job.lab_runtime { LabeledContent("Runtime", value: runtime) }
        if let size = job.voice_batch {
          LabeledContent("Requested voice batch", value: String(size))
        }
        if let precision = job.lab_separation {
          LabeledContent("Dialogue separation", value: precision.uppercased())
        }
        if let model = job.asr_model { LabeledContent("Speech recognition", value: model) }
        if let model = job.translation_model { LabeledContent("Translation", value: model) }
        if let model = job.voice_model { LabeledContent("Voice", value: model) }
      }
      if let report = job.report {
        Section(report.category ?? "Issue report") {
          Text(report.note ?? "")
          Text(report.status ?? "").foregroundStyle(.secondary)
        }
      }
    }.navigationTitle("Request details").privacySensitive()
  }
}

private struct AdminAccountView: View {
  let api: API
  struct Allowance: Decodable {
    let tier: String
    let used: Int
    let limit, monthly_seconds, pack_seconds: Int?
  }
  @State private var allowance: Allowance?
  let account: OperatorAccount
  let changed: () async -> Void
  @State private var reason = ""
  @State private var confirm = false
  @State private var banned: Bool?
  @State private var busy = false
  @State private var error: String?
  @State private var rows: [Activity] = []
  @State private var more = false
  var isBanned: Bool { banned ?? account.banned ?? false }
  var body: some View {
    List {
      Section("Account") {
        Text(account.label)
        Text(account.id).font(.caption).textSelection(.enabled)
        LabeledContent("Status", value: isBanned ? "Banned" : "Active")
        if let date = Job.date(account.last_sign_in) {
          LabeledContent("Last sign-in", value: date.formatted())
        }
        if let note = account.moderation_reason, !note.isEmpty {
          Text(note).foregroundStyle(.secondary)
        }
      }
      if let allowance {
        Section("Allowance") {
          LabeledContent("Plan", value: allowance.tier.capitalized)
          LabeledContent(
            "Daily usage",
            value: allowance.limit.map { "\(allowance.used) of \($0)" } ?? "Unlimited")
          if let monthly = allowance.monthly_seconds {
            LabeledContent("Subscription time left", value: Job.duration(monthly))
          }
          if let extra = allowance.pack_seconds {
            LabeledContent("Extra time left", value: Job.duration(extra))
          }
        }
      }
      if account.protected != true {
        Section {
          TextField("Reason for moderation", text: $reason, axis: .vertical)
          Button(isBanned ? "Lift ban" : "Ban account", role: .destructive) { confirm = true }
            .disabled(
              busy || (!isBanned && reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
        }
      }
      if let history = account.moderation_history, !history.isEmpty {
        Section("Moderation history") {
          ForEach(Array(history.reversed().enumerated()), id: \.offset) { _, event in
            VStack(alignment: .leading) {
              Text(
                (event.ban ? "Banned" : "Restored") + " · "
                  + (Job.date(event.at)?.formatted() ?? event.at))
              if !event.reason.isEmpty { Text(event.reason).font(.caption) }
            }
          }
        }
      }
      if let error { Text(error).foregroundStyle(.red) }
      Section("Requests") {
        ForEach(rows) { row in
          NavigationLink(row.title ?? row.url ?? "Video upload") { AdminRequestDetails(job: row) }
        }
        if more { Button("Load older requests") { Task { await load() } }.disabled(busy) }
      }
    }.privacySensitive().navigationTitle("Account activity").task {
      if rows.isEmpty { await load() }
      guard !Task.isCancelled else { return }
      do {
        let data = try await api.post("v1/admin/allowance", ["account": account.id])
        allowance = try JSONDecoder().decode(Allowance.self, from: data)
      } catch { self.error = error.localizedDescription }
    }
    .confirmationDialog(
      isBanned
        ? "Restore hosted access for \(account.label)?"
        : "Ban \(account.label) from hosted access?", isPresented: $confirm
    ) {
      Button(isBanned ? "Lift ban" : "Ban account", role: .destructive) {
        Task { await moderate() }
      }
    }
  }
  @MainActor private func load() async {
    guard !busy else { return }
    busy = true
    defer { busy = false }
    do {
      let data = try await api.post(
        "v1/admin/activity",
        Cursor(before: rows.last?.created, after_id: rows.last?.id, account: account.id))
      let page = try JSONDecoder().decode(Rows<Activity>.self, from: data)
      rows += page.rows
      more = page.more
    } catch { self.error = error.localizedDescription }
  }
  @MainActor private func moderate() async {
    struct Action: Encodable {
      let account: String
      let ban: Bool
      let reason: String
    }
    guard !busy else { return }
    busy = true
    defer { busy = false }
    do {
      let next = !isBanned
      _ = try await api.post(
        "v1/admin/moderation", Action(account: account.id, ban: next, reason: reason))
      banned = next
      reason = ""
      error = nil
      await changed()
    } catch { self.error = error.localizedDescription }
  }
}

private struct RenderLab: View {
  let studio: Studio
  let api: API
  @State private var url = ""
  @State private var target = "en"
  @State private var mode = "best"
  @State private var voice = "default"
  @State private var runtime = "standard"
  @State private var paired = true
  @State private var halfPrecision = false
  @State private var duration = 40.0
  @State private var pendingID: UUID?
  @State private var busy = false
  @State private var message: String?
  @State private var queued = false
  private let preset = ["en", "fr", "hi", "it", "pt", "es"]
  private var formKey: String {
    [url, target, mode, voice, runtime, paired ? "2" : "1", halfPrecision ? "fp16" : "fp32"].joined(
      separator: "|")
  }
  private var languages: [String] { studio.account?.output_languages ?? ["en"] }
  private var usesPreset: Bool {
    voice == "preset"
      || (voice == "default" && preset.contains(target) && (mode != "best" || target == "hi"))
  }
  private var explanation: String {
    if mode == "best" {
      return
        "Context translation and the 1.7B matched voice where supported. Retains the source-voice cleanup."
    }
    if mode == "medium" {
      return
        "Context translation with a Kokoro preset where available. Other languages use the 1.7B matched voice."
    }
    return
      "Faster speech decoding and preset voices where available. English uses Whisper’s direct translation. Other outputs use context translation."
  }
  private var estimate: String {
    let baseline =
      mode == "best"
      ? 19 + 1.73 * duration : mode == "medium" ? 12 + 1.38 * duration : 13 + 1.19 * duration
    return "About " + Job.duration(Int(baseline.rounded())) + " processing"
  }
  var body: some View {
    Form {
      Section("Test Reel") {
        TextField("Instagram Reel link", text: $url).keyboardType(.URL).textInputAutocapitalization(
          .never
        ).autocorrectionDisabled()
        Picker("Dub language", selection: $target) {
          ForEach(languages, id: \.self) { Text(OutputLanguage.name($0)).tag($0) }
        }
      }
      Section {
        Picker("Render mode", selection: $mode) {
          ForEach(["best", "medium", "fast"], id: \.self) { Text($0.capitalized).tag($0) }
        }.pickerStyle(.segmented)
        Text(explanation).font(.subheadline)
        LabeledContent("Sample Reel length", value: Job.duration(Int(duration)))
        Slider(value: $duration, in: 10...120, step: 5).accessibilityLabel("Length for estimate")
        Text(
          voice == "default" && (usesPreset || paired) && !halfPrecision && target == "en"
            ? estimate : "Custom configuration · estimate after processing starts"
        )
        .font(.headline)
      } header: {
        Text("Render mode")
      } footer: {
        Text(
          "Estimates use English dubs on the 4070 Ti Super. Queue wait and download time are additional."
        )
      }
      Section("Experiment controls") {
        Picker("Voice engine", selection: $voice) {
          Text("Mode default").tag("default")
          if target != "hi" {
            Text("Qwen 1.7B · matched").tag("matched")
            Text("Qwen 0.6B · experimental").tag("small")
          }
          if preset.contains(target) { Text("Kokoro · preset").tag("preset") }
        }
        if target != "hi", let runtimes = studio.account?.lab_runtimes, runtimes.count > 1 {
          Picker("Matched voice runtime", selection: $runtime) {
            ForEach(runtimes, id: \.self) { engine in
              Text(
                engine == "graph"
                  ? "CUDA Graphs" : engine == "omni" ? "vLLM-Omni · experimental" : "Standard"
              ).tag(engine)
            }
          }
          if runtime != "standard" {
            Text(
              runtime == "omni"
                ? "Includes engine startup on each test."
                : "Replays GPU operations for one line at a time."
            ).font(.caption)
          }
        }
        Toggle("FP16 dialogue separation", isOn: $halfPrecision)
        Toggle("Generate lines in pairs", isOn: $paired).disabled(usesPreset || runtime == "graph")
        if voice == "small" {
          Text("This model can take longer on multi-speaker clips.").font(.caption)
        }
      }
      Section {
        Button {
          Task { await submit() }
        } label: {
          if busy { ProgressView() } else { Label("Queue fresh test", systemImage: "play.fill") }
        }.disabled(
          busy || queued || !Job.isReel(url.trimmingCharacters(in: .whitespacesAndNewlines)))
        if let message {
          Text(message).font(.subheadline).foregroundStyle(queued ? Color.secondary : Color.red)
        }
        if queued {
          Button("Prepare another run") {
            queued = false
            pendingID = nil
            message = nil
          }
        }
      } footer: {
        Text(
          "Each test renders a fresh copy and appears in Dubs. It joins the priority queue in submission order."
        )
      }
    }.navigationTitle("Render lab").privacySensitive()
      .onChange(of: target) { _, _ in
        voice = "default"
        runtime = "standard"
      }
      .onChange(of: runtime) { _, engine in
        if engine != "standard" { voice = "matched" }
        if engine == "graph" { paired = false }
      }
      .onChange(of: voice) { _, new in if new != "matched" { runtime = "standard" } }
      .onChange(of: formKey) { _, _ in
        pendingID = nil
        queued = false
        message = nil
      }
      .disabled(busy)
  }
  @MainActor private func submit() async {
    struct Experiment: Encodable {
      let url, target, mode, voice, runtime, separation: String
      let batch: Int
      let request_id: UUID
    }
    guard !busy, !queued else { return }
    let id = pendingID ?? UUID()
    pendingID = id
    busy = true
    message = nil
    defer { busy = false }
    do {
      let body = Experiment(
        url: url.trimmingCharacters(in: .whitespacesAndNewlines), target: target, mode: mode,
        voice: voice, runtime: runtime, separation: halfPrecision ? "fp16" : "fp32",
        batch: paired ? 2 : 1, request_id: id)
      let result = try JSONDecoder().decode(
        Job.self, from: await api.post("v1/admin/experiments", body))
      queued = true
      message =
        result.state == "queued" ? "Queued. Follow this test in Dubs." : "This test is in Dubs."
      await studio.refresh()
    } catch { message = error.localizedDescription }
  }
}
