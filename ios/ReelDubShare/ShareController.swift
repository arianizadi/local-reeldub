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
