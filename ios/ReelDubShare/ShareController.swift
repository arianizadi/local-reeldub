import SwiftUI
import UniformTypeIdentifiers
import UIKit

final class ShareController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        Task { @MainActor in
            let value = await incomingURL()
            let host = UIHostingController(rootView: SharePanel(link: value) { [weak self] in
                self?.extensionContext?.completeRequest(returningItems: nil)
            })
            addChild(host)
            host.view.frame = view.bounds
            host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(host.view); host.didMove(toParent: self)
        }
    }
    private func incomingURL() async -> String? {
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        for provider in items.flatMap({ $0.attachments ?? [] }) {
            for type in [UTType.url, .plainText] where provider.hasItemConformingToTypeIdentifier(type.identifier) {
                let item = try? await provider.loadItem(forTypeIdentifier: type.identifier)
                if let url = item as? URL { return url.absoluteString }
                if let text = item as? String, let match = text.range(of: #"https://(?:www\.)?instagram\.com/reels?/[^\s]+"#, options: .regularExpression) {
                    return String(text[match])
                }
            }
        }
        return nil
    }
}
struct SharePanel: View {
    let link: String?
    let close: () -> Void
    @State private var requestID = UUID()
    @State private var busy = false
    @State private var success = false
    @State private var message: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Capsule().fill(.secondary.opacity(0.4)).frame(width: 35, height: 5).frame(maxWidth: .infinity)
            Text(success ? "Reel queued" : "Dub with ReelDub").font(.system(.largeTitle, design: .serif, weight: .bold))
            Text(success ? "Open ReelDub to follow its progress and play the English video." : link ?? "Share an Instagram Reel link.").foregroundStyle(.secondary)
            if let message { Text(message).font(.footnote).foregroundStyle(.red) }
            Button {
                guard !success else { close(); return }
                guard let connection = Connection.read(), let link else { message = "Open ReelDub and connect your server first."; return }
                Task { busy = true; defer { busy = false }
                    do { _ = try await API(connection: connection).submit(link, id: requestID); success = true }
                    catch { message = error.localizedDescription }
                }
            } label: {
                Text(success ? "Done" : busy ? "Sending…" : "Translate to English").frame(maxWidth: .infinity).padding(.vertical, 6)
            }.buttonStyle(.borderedProminent).tint(Color(red: 0.34, green: 0.18, blue: 0.31)).disabled(busy || link == nil)
            if !success { Button("Cancel", action: close) }
            Spacer()
        }.padding(24)
    }
}

