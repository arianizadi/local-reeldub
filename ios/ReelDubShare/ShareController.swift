import SwiftUI
import UIKit

@MainActor
final class ShareController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        ImportedMovie.removeExpiredImports()
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        let host = UIHostingController(rootView: SharePanel(items: items) { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        })
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
    }
}
