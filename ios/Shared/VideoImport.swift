import AVFoundation
import CoreTransferable
import Foundation
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct ImportedMovie: Transferable {
  let file: URL
  let title: String
  static var transferRepresentation: some TransferRepresentation {
    FileRepresentation(importedContentType: .movie) { item in try await copy(item.file) }
  }
  static let typeError = "Choose a video file from Photos or Files."

  /// NSItemProvider removes its temporary file as soon as its completion block returns.
  /// Take ownership synchronously here, then inspect the private copy asynchronously.
  private static func stage(_ url: URL, title: String? = nil) throws -> ImportedMovie {
    guard url.isFileURL else { throw Problem(typeError) }
    let scoped = url.startAccessingSecurityScopedResource()
    defer { if scoped { url.stopAccessingSecurityScopedResource() } }
    let files = FileManager.default
    let attributes = try files.attributesOfItem(atPath: url.path)
    guard attributes[.type] as? FileAttributeType == .typeRegular else { throw Problem(typeError) }
    guard let size = attributes[.size] as? NSNumber, size.int64Value > 0 else { throw Problem("This video is empty.") }

    let folder = files.temporaryDirectory.appending(path: "ReelDubImports", directoryHint: .isDirectory)
    try files.createDirectory(at: folder, withIntermediateDirectories: true,
                              attributes: [.protectionKey: FileProtectionType.complete])
    let dest = folder.appendingPathComponent(UUID().uuidString).appendingPathExtension(
      url.pathExtension.isEmpty ? "mov" : url.pathExtension)
    do {
      try files.copyItem(at: url, to: dest)
      try files.setAttributes([.protectionKey: FileProtectionType.complete, .creationDate: Date()], ofItemAtPath: dest.path)
      guard let stagedSize = try dest.resourceValues(forKeys: [.fileSizeKey]).fileSize,
            Int64(stagedSize) == size.int64Value else { throw Problem("The video copy was incomplete.") }
    } catch {
      try? files.removeItem(at: dest)
      throw error
    }
    let filename = title?.trimmingCharacters(in: .whitespacesAndNewlines)
    let display = (filename.flatMap { $0.isEmpty ? nil : $0 } ?? url.lastPathComponent) as NSString
    return ImportedMovie(file: dest, title: (display.lastPathComponent as NSString).deletingPathExtension)
  }

  func remove() { try? FileManager.default.removeItem(at: file) }

  private func validate() async throws -> ImportedMovie {
    do {
      try Task.checkCancellation()
      let asset = AVURLAsset(url: file)
      guard let tracks = try? await asset.loadTracks(withMediaType: .video), !tracks.isEmpty else {
        throw Problem(Self.typeError)
      }
      try Task.checkCancellation()
      return self
    } catch {
      remove()
      throw error
    }
  }

  static func copy(_ url: URL) async throws -> ImportedMovie {
    try await stage(url).validate()
  }

  static func shared(from provider: NSItemProvider) async throws -> ImportedMovie {
    let transfer = Progress(totalUnitCount: 1)
    let title = provider.suggestedName
    return try await withTaskCancellationHandler {
      try Task.checkCancellation()
      let movie: ImportedMovie = try await withCheckedThrowingContinuation { continuation in
        let finish: @Sendable (URL?, Error?) -> Void = { url, error in
          do {
            if transfer.isCancelled { throw CancellationError() }
            if let error { throw error }
            guard let url else { throw Problem(typeError) }
            let owned = try stage(url, title: title)
            continuation.resume(returning: owned)
          } catch { continuation.resume(throwing: error) }
        }
        let movieType = provider.registeredTypeIdentifiers.first { UTType($0)?.conforms(to: .movie) == true }
        if let movieType {
          let request = provider.loadFileRepresentation(forTypeIdentifier: movieType, completionHandler: finish)
          transfer.addChild(request, withPendingUnitCount: 1)
          if transfer.isCancelled { request.cancel() }
        } else if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
          provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, error in
            finish(item as? URL, error)
          }
        } else {
          continuation.resume(throwing: Problem(typeError))
        }
      }
      return try await movie.validate()
    } onCancel: { transfer.cancel() }
  }

  /// The system can terminate a share extension before normal cleanup.
  static func removeExpiredImports() {
    let files = FileManager.default
    let folder = files.temporaryDirectory.appending(path: "ReelDubImports", directoryHint: .isDirectory)
    let keys: Set<URLResourceKey> = [.creationDateKey]
    let cutoff = Date().addingTimeInterval(-24 * 3600)
    guard let urls = try? files.contentsOfDirectory(at: folder, includingPropertiesForKeys: Array(keys)) else { return }
    for url in urls {
      guard let created = try? url.resourceValues(forKeys: keys).creationDate, created < cutoff else { continue }
      try? files.removeItem(at: url)
    }
  }
}

struct VideoImportButton: View {
  let receive: (ImportedMovie) -> Void
  let failed: (String) -> Void
  var title = "Import video"
  @Environment(\.dynamicTypeSize) private var typeSize
  @State private var showSources = false
  @State private var showPhotos = false
  @State private var showFiles = false
  @State private var photo: PhotosPickerItem?
  @State private var file: URL?

  var body: some View {
    Button { showSources = true } label: {
      if photo != nil || file != nil {
        ProgressView("Importing video…")
      } else {
        Group {
          if typeSize.isAccessibilitySize { Text(title) }
          else { Label(title, systemImage: "square.and.arrow.down") }
        }.fixedSize(horizontal: false, vertical: true).frame(minHeight: 44).contentShape(Rectangle())
      }
    }
    .disabled(photo != nil || file != nil)
    .confirmationDialog("Import video", isPresented: $showSources, titleVisibility: .visible) {
      Button("Photos") { showPhotos = true }
      Button("Files") { showFiles = true }
    }
    .photosPicker(isPresented: $showPhotos, selection: $photo, matching: .videos)
    .fileImporter(isPresented: $showFiles, allowedContentTypes: [.movie]) { result in
      do { file = try result.get() }
      catch where (error as NSError).code == NSUserCancelledError { }
      catch { failed(error.localizedDescription) }
    }
    .task(id: photo) {
      guard let photo else { return }
      defer { self.photo = nil }
      do {
        guard let movie = try await photo.loadTransferable(type: ImportedMovie.self) else {
          throw Problem("This video could not be imported.")
        }
        complete(movie)
      } catch { if !Task.isCancelled { failed(error.localizedDescription) } }
    }
    .task(id: file) {
      guard let file else { return }
      defer { self.file = nil }
      do {
        let movie = try await Task.detached(priority: .userInitiated) {
          try await ImportedMovie.copy(file)
        }.value
        complete(movie)
      } catch { if !Task.isCancelled { failed(error.localizedDescription) } }
    }
  }

  private func complete(_ movie: ImportedMovie) {
    if Task.isCancelled { movie.remove() }
    else { receive(movie) }
  }
}
