import UniformTypeIdentifiers
import UIKit

/// Video apps and browsers can supply a URL, a share caption or both. Read those inline without
/// opening the link or asking the host app to switch to ReelDub.
enum SharedVideo {
    static func extract(from items: [NSExtensionItem]) async throws -> DubInput {
        let providers = items.flatMap { $0.attachments ?? [] }
        for type in [UTType.url, .plainText, .text] {
            for provider in providers where provider.hasItemConformingToTypeIdentifier(type.identifier) {
                if let link = try? await link(in: provider, type: type) { return .link(link) }
            }
        }
        for item in items {
            if let caption = item.attributedContentText?.string, let link = RDLink.video(in: caption) { return .link(link) }
        }
        let movies = providers.filter {
            $0.hasItemConformingToTypeIdentifier(UTType.movie.identifier) || $0.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier)
        }
        if movies.count == 1 { return .file(try await ImportedMovie.shared(from: movies[0])) }
        throw Problem("Share a public video link, or choose a video from Photos or Files.")
    }

    private static func link(in provider: NSItemProvider, type: UTType) async throws -> String? {
        if type == .url, provider.canLoadObject(ofClass: NSURL.self) {
            let url: URL? = await withCheckedContinuation { continuation in
                _ = provider.loadObject(ofClass: NSURL.self) { object, _ in continuation.resume(returning: object as? URL) }
            }
            if let url, let link = RDLink.video(in: url.absoluteString) { return link }
        }
        if type.conforms(to: .text), provider.canLoadObject(ofClass: NSString.self) {
            let caption: String? = await withCheckedContinuation { continuation in
                _ = provider.loadObject(ofClass: NSString.self) { object, _ in continuation.resume(returning: object as? String) }
            }
            if let caption, let link = RDLink.video(in: caption) { return link }
        }
        return try await withCheckedThrowingContinuation { continuation in
            provider.loadItem(forTypeIdentifier: type.identifier, options: nil) { item, error in
                if let error { continuation.resume(throwing: error); return }
                let text: String?
                switch item {
                case let url as URL: text = url.absoluteString
                case let caption as String: text = caption
                case let caption as NSAttributedString: text = caption.string
                case let bytes as Data: text = String(data: bytes, encoding: .utf8)
                default: text = nil
                }
                continuation.resume(returning: text.flatMap(RDLink.video))
            }
        }
    }
}
