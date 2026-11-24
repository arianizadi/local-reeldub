import SwiftUI

enum ProjectInput: Identifiable {
  case file(URL, String)
  case source(String)
  var id: String {
    switch self {
    case .file(let url, _): url.path
    case .source(let id): id
    }
  }
}

struct SourceReceipt: Decodable {
  let id, title: String
  let seconds: Int
}

struct DubQuote: Decodable {
  struct Line: Decodable, Identifiable {
    let target: String
    let free, monthly, pack, missing: Int
    let reused, unlimited: Bool
    var id: String { target }
    var price: String {
      if reused { return "Already in your library or queue · free" }
      if unlimited { return "Included · unlimited" }
      if free > 0 { return "1 daily dub" }
      var parts: [String] = []
      if monthly > 0 { parts.append(Job.duration(monthly) + " monthly") }
      if pack > 0 { parts.append(Job.duration(pack) + " purchased") }
      if missing > 0 { parts.append(Job.duration(missing) + " more needed") }
      return parts.joined(separator: " + ")
    }
  }
  let id: String
  let seconds, monthly_left, pack_left, free_left: Int
  let lines: [Line]
}

extension API {
  func prepare(_ input: ProjectInput) async throws -> SourceReceipt {
    let result: Data
    switch input {
    case .file(let file, let title):
      var req = try request("v1/sources/upload", method: "POST")
      req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
      req.setValue(
        String(title.prefix(140)).addingPercentEncoding(withAllowedCharacters: .alphanumerics),
        forHTTPHeaderField: "X-Video-Title")
      req.timeoutInterval = 180
      let (data, response) = try await transport.upload(for: req, fromFile: file)
      result = try checked(data, response)
    case .source(let id):
      result = try await data("v1/sources/" + id)
    }
    return try JSONDecoder().decode(SourceReceipt.self, from: result)
  }
  func quote(_ request: UploadQuoteRequest) async throws -> DubQuote {
    try JSONDecoder().decode(DubQuote.self, from: await post("v1/quotes", request))
  }
}

enum DubVoice: String, Encodable {
  case narrator, match
}

enum VoiceConsent: String, Encodable {
  case own = "own_voice"
  case documented = "documented_permission"
}

struct UploadQuoteRequest: Encodable, Hashable {
  let source_id: String
  let targets: [String]
  let trim: Bool
  let voice_mode: DubVoice
  let voice_consent: VoiceConsent?
  let consent_reference: String?

  init?(source: String?, targets: Set<String>, supported: [String], trim: Bool, voice: DubVoice,
        consent: VoiceConsent?, reference: String, agreed: Bool) {
    guard let source, !targets.isEmpty, targets.isSubset(of: Set(supported)) else { return nil }
    let ref = reference.trimmingCharacters(in: .whitespacesAndNewlines)
    if voice == .match {
      guard agreed, let consent else { return nil }
      if consent == .documented && (ref.isEmpty || ref.count > 160) { return nil }
    }
    source_id = source
    self.targets = targets.sorted()
    self.trim = trim
    voice_mode = voice
    voice_consent = voice == .match ? consent : nil
    consent_reference = voice_consent == .documented ? ref : nil
  }
}

struct BalanceOptions: View {
  let live: Bool
  var plans: AnyView?
  var refresh: () -> Void = {}
  @State private var showing = false

  var body: some View {
    if let plans {
      Button(live ? "View payment options" : "View plans") { showing = true }
        .sheet(isPresented: $showing, onDismiss: refresh) { plans }
    } else {
      Text(live
           ? "To add minutes, open ReelDub and choose Account → Plans & minutes."
           : "Purchases are currently unavailable. Open Account in ReelDub to view your plan.")
        .font(.footnote).foregroundStyle(.secondary)
    }
  }
}

struct QuoteSheet: View {
  let api: API
  let input: ProjectInput
  let voiceModes: [String: [String]]
  let done: ([Job]) -> Void
  let creatorMinutes: Bool
  let liveOffers: Bool
  let plans: AnyView?
  @Environment(\.dismiss) private var dismiss
  @State private var selected: Set<String>
  @State private var trim: Bool
  @State private var source: SourceReceipt?
  @State private var quote: DubQuote?
  @State private var quotedRequest: UploadQuoteRequest?
  @State private var voice = DubVoice.narrator
  @State private var consent: VoiceConsent?
  @State private var releaseReference = ""
  @State private var agreed = false
  @State private var committed: [Job] = []
  @State private var submittedNotice: String?
  @State private var revision = 0
  @State private var busy = false
  @State private var message: String?
  @State private var needsNewFile = false
  @State private var needsPreparation = false
  @State private var replacement: ImportedMovie?
  private var currentInput: ProjectInput { replacement.map { .file($0.file, $0.title) } ?? input }
  private var preparationTitle: String {
    if case .file = currentInput { return "Uploading video…" }
    return "Checking video duration…"
  }
  init(
    api: API, input: ProjectInput, voiceModes: [String: [String]], target: String, trim: Bool = false,
    creatorMinutes: Bool = false, liveOffers: Bool = false, plans: AnyView? = nil, done: @escaping ([Job]) -> Void
  ) {
    self.api = api
    self.input = input
    self.voiceModes = voiceModes
    self.done = done
    self.creatorMinutes = creatorMinutes
    self.liveOffers = liveOffers
    self.plans = plans
    let available = voiceModes[DubVoice.narrator.rawValue] ?? []
    _selected = State(initialValue: available.isEmpty ? [] : [OutputLanguage.selected(target, supported: available)])
    _trim = State(initialValue: trim)
  }
  private var languages: [String] { voiceModes[voice.rawValue] ?? [] }
  private var request: UploadQuoteRequest? {
    UploadQuoteRequest(source: source?.id, targets: selected, supported: languages, trim: trim,
                       voice: voice, consent: consent, reference: releaseReference, agreed: agreed)
  }

  private var canUpload: Bool { !(voiceModes["narrator"] ?? []).isEmpty }
  private var validReference: Bool {
    let value = releaseReference.trimmingCharacters(in: .whitespacesAndNewlines)
    return !value.isEmpty && value.count <= 160
  }

  @ViewBuilder private var voiceSection: some View {
    Section {
      if !(voiceModes["match"] ?? []).isEmpty {
        Picker("Voice", selection: $voice) {
          Text("Narrator").tag(DubVoice.narrator)
          Text("Match speaker").tag(DubVoice.match)
        }
        .pickerStyle(.segmented)
        .accessibilityIdentifier("dub-voice")
      } else {
        Label("Narrator", systemImage: "waveform")
      }
    } header: { Text("Dub voice") } footer: {
      Text(voice == .narrator
           ? "A standard generated voice reads the translation."
           : "ReelDub uses speech from this video to generate translated audio that sounds like the speaker.")
    }
    if voice == .match {
      Section("Speaker permission") {
        Picker("Permission", selection: $consent) {
          Text("Choose…").tag(nil as VoiceConsent?)
          Text("Only my voice").tag(Optional(VoiceConsent.own))
          Text("I have written permission").tag(Optional(VoiceConsent.documented))
        }
        if consent == .documented {
          TextField("Release reference", text: $releaseReference)
            .autocorrectionDisabled()
            .textInputAutocapitalization(.never)
            .accessibilityLabel("Reference for the written permission")
          Text("Use a reference you can find later, such as a release number. Keep personal details out of this field.")
            .font(.footnote).foregroundStyle(.secondary)
        }
        if let consent {
          Toggle(isOn: $agreed) {
            Text(consent == .own
                 ? "I am the only speaker and agree to my voice being matched for this dub."
                 : "I have written permission from every speaker to match their voice for this translated video and export it.")
          }.disabled(consent == .documented && !validReference)
           .accessibilityIdentifier("voice-consent")
        }
      }
    }
  }

  var body: some View {
    NavigationStack {
      List {
        if let submittedNotice { Text(submittedNotice).foregroundStyle(.secondary) }
        if let source {
          Section {
            Text(source.title).font(.headline)
            DubValueRow("Source length", value: Job.duration(source.seconds))
            if source.seconds > 60 { Toggle("Use the first 60 seconds", isOn: $trim) }
          } footer: { Text(DubGuidance.accuracy) }
          voiceSection
          Section("Dub into") {
            Menu {
              ForEach(languages, id: \.self) { code in
                Button {
                  if selected.contains(code) {
                    if selected.count > 1 { selected.remove(code) }
                  } else {
                    selected.insert(code)
                  }
                } label: {
                  if selected.contains(code) {
                    Label(OutputLanguage.name(code), systemImage: "checkmark")
                  } else {
                    Text(OutputLanguage.name(code))
                  }
                }
              }
            } label: {
              HStack {
                Label(selected.sorted().map(OutputLanguage.name).joined(separator: ", "), systemImage: "globe")
                Spacer()
                Image(systemName: "chevron.up.chevron.down").font(.footnote).accessibilityHidden(true)
              }
              .accessibilityLabel("Dub languages")
              .accessibilityValue(selected.sorted().map(OutputLanguage.name).joined(separator: ", "))
            }
          }
          if let quote, let request, quotedRequest == request, !needsNewFile {
            Section("Cost to confirm") {
              ForEach(quote.lines) { line in
                DubValueRow(OutputLanguage.name(line.target), value: line.missing > 0 && !creatorMinutes ? "Daily allowance reached" : line.price)
              }
            }
            if quote.lines.contains(where: { $0.missing > 0 }) {
              Section(creatorMinutes ? "More time needed" : "Daily allowance") {
                if creatorMinutes {
                  let missing = quote.lines.reduce(0) { $0 + $1.missing }
                  Text("You need " + Job.duration(missing) + " more for this selection.")
                }
                if quote.free_left == 0 {
                  Text(Account.dailyResetNotice).foregroundStyle(.secondary)
                }
                if creatorMinutes && api.connection.isHosted {
                  BalanceOptions(live: liveOffers, plans: plans) { revision += 1 }
                }
                Button("Check balance again") { revision += 1 }
              }
            } else {
              if !quote.lines.allSatisfy(\.unlimited) {
                Section("Available after confirmation") {
                  DubValueRow("Daily dubs", value: String(quote.free_left))
                  if creatorMinutes {
                    DubValueRow("Monthly time", value: Account.minuteBalance(quote.monthly_left))
                    DubValueRow("Purchased time", value: Account.minuteBalance(quote.pack_left))
                  }
                }
              }
              Button { Task { await confirm(quote) } } label: {
                Text(busy ? "Queuing…" : "Confirm dub")
                  .font(.headline).foregroundStyle(Palette.onAction).frame(maxWidth: .infinity, minHeight: 44)
              }
              .buttonStyle(.borderedProminent)
              .listRowBackground(Color.clear)
              .disabled(busy)
            }
          } else if message == nil && request != nil {
            DubLoadingIndicator("Calculating cost…", compact: true)
          }
        } else if !canUpload {
          Text("This server needs an update before it can dub uploaded videos.")
        } else if message == nil {
          DubLoadingIndicator(preparationTitle, compact: true)
        }
        if let message {
          Text(message).foregroundStyle(.secondary)
          if needsNewFile {
            VideoImportButton(receive: { movie in
              if let replacement { try? FileManager.default.removeItem(at: replacement.file) }
              resetConsent()
              replacement = movie
              source = nil
              quote = nil
              needsNewFile = false
              revision += 1
            }, failed: { self.message = $0 }, title: "Choose video again")
          } else {
            Button("Review again") {
              if needsPreparation {
                source = nil
                quote = nil
                needsPreparation = false
              }
              revision += 1
            }
          }
        }
      }
      .disabled(busy)
      .navigationTitle("Review dub")
      .toolbar {
        ToolbarItem(placement: .cancellationAction) {
          Button(committed.isEmpty ? "Close" : "Done") {
            if !committed.isEmpty { done(committed) }
            dismiss()
          }.disabled(busy)
        }
      }
    }
    .interactiveDismissDisabled(busy || !committed.isEmpty)
    .task(id: source == nil ? revision : -1) {
      guard source == nil, canUpload else { return }
      message = nil
      do {
        let item = try await api.prepare(currentInput)
        try Task.checkCancellation()
        source = item
      } catch { if !Task.isCancelled { handle(error) } }
    }
    .onChange(of: voice) { _, _ in
      resetConsent()
      selected.formIntersection(languages)
      if selected.isEmpty, let first = languages.first { selected = [first] }
    }
    .onChange(of: consent) { _, _ in agreed = false; releaseReference = "" }
    .onChange(of: releaseReference) { _, _ in agreed = false }
    .onChange(of: selected) { _, _ in agreed = false }
    .onChange(of: trim) { _, _ in agreed = false }
    .task(id: QuoteRefresh(request: request, revision: revision)) {
      quote = nil
      quotedRequest = nil
      guard let current = request else { return }
      message = nil
      do {
        let value = try await api.quote(current)
        try Task.checkCancellation()
        if request == current {
          quote = value
          quotedRequest = current
        }
      } catch { if !Task.isCancelled { handle(error) } }
    }
    .onDisappear {
      if let replacement { try? FileManager.default.removeItem(at: replacement.file) }
    }
  }

  private struct QuoteRefresh: Equatable {
    let request: UploadQuoteRequest?
    let revision: Int
  }

  private func resetConsent() {
    consent = nil
    releaseReference = ""
    agreed = false
  }

  private func handle(_ error: Error) {
    message = error.localizedDescription
    if (error as? ServiceError)?.status == 404 {
      quote = nil
      if case .source = currentInput { needsNewFile = true }
      else { needsPreparation = true }
    }
  }

  private func confirm(_ value: DubQuote) async {
    guard !busy, let request, quotedRequest == request, !value.lines.contains(where: { $0.missing > 0 }) else { return }
    busy = true
    defer { busy = false }
    do {
      struct Confirmation: Decodable {
        let jobs: [Job]
        let remaining: [String]
        let detail: String?
      }
      let response = try JSONDecoder().decode(
        Confirmation.self,
        from: await api.data("v1/quotes/" + value.id + "/confirm", method: "POST"))
      committed += response.jobs.filter { job in !committed.contains { $0.id == job.id } }
      if response.remaining.isEmpty {
        done(committed)
        dismiss()
      } else {
        let names = response.jobs.map(\.targetName).joined(separator: ", ")
        submittedNotice =
          "Confirmed " + names + ". " + (response.detail ?? "Review the remaining languages.")
        selected = Set(response.remaining)
        revision += 1
      }
    } catch {
      handle(error)
      if let error = error as? ServiceError, [402, 409, 410].contains(error.status) { quote = nil }
    }
  }
}
