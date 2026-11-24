import SwiftUI
import UniformTypeIdentifiers
import UIKit

@MainActor
final class ShareController: UIViewController {
    private var attachment: SharedMovieRequest?

    override func viewDidLoad() {
        super.viewDidLoad()
        ImportedMovie.removeExpiredImports()
        let items = extensionContext?.inputItems as? [NSExtensionItem] ?? []
        let attachment = SharedMovieRequest(providers: items.flatMap { $0.attachments ?? [] })
        self.attachment = attachment
        let host = UIHostingController(rootView: SharePanel(attachment: attachment) { [weak self] in
            self?.attachment?.discard()
            self?.extensionContext?.completeRequest(returningItems: nil)
        })
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
    }
}

/// A provider's file can disappear while the confirmation sheet is open.
@MainActor
final class SharedMovieRequest: ObservableObject {
    let provider: NSItemProvider?
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    @Published var project: ProjectInput?
    private var movie: ImportedMovie?
    private var task: Task<Void, Never>?

    init(providers: [NSItemProvider]) {
        if providers.count == 1, let candidate = providers.first,
           candidate.hasItemConformingToTypeIdentifier(UTType.movie.identifier)
            || candidate.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            provider = candidate
        } else {
            provider = nil
        }
    }

    func prepare() {
        guard let provider, !loading, project == nil else { return }
        error = nil
        loading = true
        task = Task { [weak self] in
            do {
                let loaded = try await ImportedMovie.shared(from: provider)
                guard !Task.isCancelled, let self else {
                    loaded.remove()
                    return
                }
                movie = loaded
                project = .file(loaded.file, loaded.title)
            } catch {
                guard !Task.isCancelled else { return }
                self?.error = error.localizedDescription
            }
            self?.loading = false
            self?.task = nil
        }
    }

    func discard() {
        task?.cancel()
        task = nil
        loading = false
        project = nil
        movie?.remove()
        movie = nil
    }

    deinit {
        task?.cancel()
        movie?.remove()
    }
}
