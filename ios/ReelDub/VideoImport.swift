import CoreTransferable
import Foundation
import UniformTypeIdentifiers

struct ImportedMovie: Transferable {
  let file: URL
  let title: String
  static var transferRepresentation: some TransferRepresentation {
    FileRepresentation(importedContentType: .movie) { item in try copy(item.file) }
  }
  static func copy(_ url: URL) throws -> ImportedMovie {
    let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
    guard size > 0, size <= 80 * 1024 * 1024 else { throw Problem("Choose a video under 80 MiB.") }
    let dest = FileManager.default.temporaryDirectory.appending(
      path: "reeldub-import-" + UUID().uuidString)
    try FileManager.default.copyItem(at: url, to: dest)
    do {
      try FileManager.default.setAttributes(
        [.protectionKey: FileProtectionType.complete], ofItemAtPath: dest.path)
    } catch {
      try? FileManager.default.removeItem(at: dest)
      throw error
    }
    return ImportedMovie(file: dest, title: url.deletingPathExtension().lastPathComponent)
  }
}
