import AVFoundation
import CoreTransferable
import Foundation
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

enum DubGuidance {
  static let durationLimit = CMTime(value: 120, timescale: 1)
  static let lengthError = "This video is longer than 2 minutes. Choose a shorter video."
  static let accuracy = "AI translations can miss words or change their meaning."
  static func checkDuration(_ duration: CMTime) throws {
    if duration.isNumeric && CMTimeCompare(duration, durationLimit) > 0 { throw Problem(lengthError) }
  }
}

struct ImportedMovie: Transferable {
  let file: URL
  let title: String
  static var transferRepresentation: some TransferRepresentation {
    FileRepresentation(importedContentType: .movie) { item in try await copy(item.file) }
  }
  static func copy(_ url: URL) async throws -> ImportedMovie {
    let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
    guard size > 0 else { throw Problem("This video is empty.") }
    if let duration = try? await AVURLAsset(url: url).load(.duration) {
      try DubGuidance.checkDuration(duration)
    }
    try Task.checkCancellation()
    guard size <= 80 * 1024 * 1024 else { throw Problem("Choose a smaller video. The upload limit is about 84 MB.") }
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
    .fileImporter(isPresented: $showFiles, allowedContentTypes: [.mpeg4Movie, .quickTimeMovie]) { result in
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
          let access = file.startAccessingSecurityScopedResource()
          defer { if access { file.stopAccessingSecurityScopedResource() } }
          return try await ImportedMovie.copy(file)
        }.value
        complete(movie)
      } catch { if !Task.isCancelled { failed(error.localizedDescription) } }
    }
  }

  private func complete(_ movie: ImportedMovie) {
    if Task.isCancelled { try? FileManager.default.removeItem(at: movie.file) }
    else { receive(movie) }
  }
}
