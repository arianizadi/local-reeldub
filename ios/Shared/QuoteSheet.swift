import SwiftUI

enum ProjectInput: Identifiable {
  case reel(String)
  case file(URL, String)
  case source(String)
  var id: String {
    switch self {
    case .reel(let link): link
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
    case .reel(let value):
      var req = try request("v1/sources/reel", method: "POST", body: ["url": value])
      // The intake service can wait 168 seconds for another request to finish preparing the same Reel.
      req.timeoutInterval = 195
      let (data, response) = try await transport.data(for: req)
      result = try checked(data, response)
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
  func quote(_ source: String, targets: [String], trim: Bool) async throws -> DubQuote {
    struct Input: Encodable {
      let source_id: String
      let targets: [String]
      let trim: Bool
    }
    return try JSONDecoder().decode(
      DubQuote.self,
      from: await post("v1/quotes", Input(source_id: source, targets: targets, trim: trim)))
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
  let languages: [String]
  let done: ([Job]) -> Void
  let liveOffers: Bool
  let plans: AnyView?
  @Environment(\.dismiss) private var dismiss
  @State private var selected: Set<String>
  @State private var trim: Bool
  @State private var source: SourceReceipt?
  @State private var quote: DubQuote?
  @State private var quotedKey: String?
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
    api: API, input: ProjectInput, languages: [String], target: String, trim: Bool = false,
    liveOffers: Bool = false, plans: AnyView? = nil, done: @escaping ([Job]) -> Void
  ) {
    self.api = api
    self.input = input
    self.languages = languages
    self.done = done
    self.liveOffers = liveOffers
    self.plans = plans
    _selected = State(initialValue: [target])
    _trim = State(initialValue: trim)
  }
  private var key: String {
    (source?.id ?? "") + selected.sorted().joined() + String(trim) + String(revision)
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
          if let quote, quotedKey == key, !needsNewFile {
            Section("Cost to confirm") {
              ForEach(quote.lines) { line in
                DubValueRow(OutputLanguage.name(line.target), value: line.price)
              }
            }
            if quote.lines.contains(where: { $0.missing > 0 }) {
              Section("More time needed") {
                let missing = quote.lines.reduce(0) { $0 + $1.missing }
                Text("You need " + Job.duration(missing) + " more for this selection.")
                if quote.free_left == 0 && !quote.lines.contains(where: { $0.free > 0 }) {
                  Text(Account.dailyResetNotice).foregroundStyle(.secondary)
                }
                if api.connection.isHosted {
                  BalanceOptions(live: liveOffers, plans: plans) { revision += 1 }
                }
                Button("Check balance again") { revision += 1 }
              }
            } else {
              if !quote.lines.allSatisfy(\.unlimited) {
                Section("Available after confirmation") {
                  DubValueRow("Daily dubs", value: String(quote.free_left))
                  DubValueRow("Monthly time", value: Account.minuteBalance(quote.monthly_left))
                  DubValueRow("Purchased time", value: Account.minuteBalance(quote.pack_left))
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
          } else if message == nil {
            DubLoadingIndicator("Calculating cost…", compact: true)
          }
        } else if message == nil {
          DubLoadingIndicator(preparationTitle, compact: true)
        }
        if let message {
          Text(message).foregroundStyle(.secondary)
          if needsNewFile {
            VideoImportButton(receive: { movie in
              if let replacement { try? FileManager.default.removeItem(at: replacement.file) }
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
      guard source == nil else { return }
      message = nil
      do {
        let item = try await api.prepare(currentInput)
        try Task.checkCancellation()
        source = item
      } catch { if !Task.isCancelled { handle(error) } }
    }
    .task(id: key) {
      guard let source else { return }
      let version = key
      quote = nil
      message = nil
      do {
        let value = try await api.quote(source.id, targets: selected.sorted(), trim: trim)
        try Task.checkCancellation()
        if key == version {
          quote = value
          quotedKey = version
        }
      } catch { if !Task.isCancelled { handle(error) } }
    }
    .onDisappear {
      if let replacement { try? FileManager.default.removeItem(at: replacement.file) }
    }
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
    guard !busy, quotedKey == key, !value.lines.contains(where: { $0.missing > 0 }) else { return }
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
