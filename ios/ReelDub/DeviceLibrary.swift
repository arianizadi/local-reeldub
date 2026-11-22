import AVFoundation
import CryptoKit
import Foundation

/// A directory rename commits the movie and its metadata together, before the server receives a receipt.
struct DeviceLibrary {
    let root: URL
    private let files = FileManager.default

    init?(connection: Connection) {
        guard let owner = connection.libraryID,
              let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
        let namespace = connection.base.absoluteString + "|" + owner
        root = support.appending(path: "ReelDub/Downloads/" + Self.digest(namespace), directoryHint: .isDirectory)
    }

    private func protect(_ url: URL) throws {
        #if os(iOS) && !targetEnvironment(macCatalyst)
        let attrs = try files.attributesOfItem(atPath: url.path)
        if attrs[.protectionKey] as? FileProtectionType != .complete {
            try files.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: url.path)
        }
        #endif
    }

    private static func digest(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    private func directory(_ id: String) -> URL { root.appending(path: Self.digest(id), directoryHint: .isDirectory) }
    func movie(_ id: String) -> URL { directory(id).appending(path: "dub.mp4") }
    func poster(_ id: String) -> URL { directory(id).appending(path: "poster.jpg") }
    private func receipt(_ id: String) -> URL { directory(id).appending(path: "received") }
    func needsReceipt(_ id: String) -> Bool { !files.fileExists(atPath: receipt(id).path) }
    func markReceived(_ id: String) throws { try Data().write(to: receipt(id), options: .atomic) }

    func inventory() -> [String: Job] {
        let entries = (try? files.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        var jobs: [String: Job] = [:]
        for entry in entries {
            if entry.lastPathComponent.hasPrefix(".partial-") { try? files.removeItem(at: entry); continue }
            guard let data = try? Data(contentsOf: entry.appending(path: "job.json")),
                  let job = try? JSONDecoder().decode(Job.self, from: data),
                  entry.lastPathComponent == Self.digest(job.id), files.fileExists(atPath: movie(job.id).path) else { continue }
            try? protect(movie(job.id))
            try? protect(entry.appending(path: "job.json"))
            jobs[job.id] = job
        }
        return jobs
    }

    func save(_ temp: URL, job: Job) throws {
        var folder = root
        try files.createDirectory(at: folder, withIntermediateDirectories: true)
        try protect(folder)
        try protect(temp)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try folder.setResourceValues(values)
        let staging = folder.appending(path: ".partial-" + UUID().uuidString)
        try files.createDirectory(at: staging, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: staging) }
        try JSONEncoder().encode(job).write(to: staging.appending(path: "job.json"))
        try protect(staging.appending(path: "job.json"))
        try files.moveItem(at: temp, to: staging.appending(path: "dub.mp4"))
        let destination = directory(job.id)
        if files.fileExists(atPath: destination.path) {
            _ = try files.replaceItemAt(destination, withItemAt: staging)
        } else {
            try files.moveItem(at: staging, to: destination)
        }
        try? files.removeItem(at: reference(job.id))
    }

    private func exclusion(_ id: String) -> URL {
        root.appending(path: ".manual-downloads").appending(path: Self.digest(id))
    }

    func isManualDownload(_ id: String) -> Bool {
        files.fileExists(atPath: exclusion(id).path)
    }

    private var history: URL { root.appending(path: ".library-history", directoryHint: .isDirectory) }
    private func reference(_ id: String) -> URL { history.appending(path: Self.digest(id)) }

    func retainedJobs() -> [String: Job] {
        let entries = (try? files.contentsOfDirectory(at: history, includingPropertiesForKeys: nil)) ?? []
        var jobs: [String: Job] = [:]
        for entry in entries {
            guard let data = try? Data(contentsOf: entry),
                  let job = try? JSONDecoder().decode(Job.self, from: data),
                  entry.lastPathComponent == Self.digest(job.id) else { continue }
            jobs[job.id] = job
        }
        return jobs
    }

    func unrecordedDownloadMarkers() -> Set<String> {
        let markers = (try? files.contentsOfDirectory(atPath: root.appending(path: ".manual-downloads").path)) ?? []
        let known = (try? files.contentsOfDirectory(atPath: history.path)) ?? []
        return Set(markers).subtracting(known)
    }

    func recoverReferences(from remote: [Job], pending: inout Set<String>) -> [Job] {
        var recovered: [Job] = []
        for job in remote {
            let name = Self.digest(job.id)
            guard pending.contains(name), (try? retain(job)) != nil else { continue }
            pending.remove(name)
            recovered.append(job)
        }
        return recovered
    }

    func retain(_ job: Job) throws {
        try files.createDirectory(at: history, withIntermediateDirectories: true)
        try JSONEncoder().encode(job).write(to: reference(job.id), options: [.atomic, .completeFileProtection])
    }

    func removeDownload(_ job: Job) throws {
        let id = job.id
        try retain(job)
        let marker = exclusion(id)
        try files.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
        // Persist the preference before unlinking the movie so a refresh or relaunch cannot restore it.
        try Data().write(to: marker, options: .atomic)
        if files.fileExists(atPath: directory(id).path) { try files.removeItem(at: directory(id)) }
    }

    func allowDownload(_ id: String) throws {
        let marker = exclusion(id)
        if files.fileExists(atPath: marker.path) { try files.removeItem(at: marker) }
    }

    func erase() throws {
        if files.fileExists(atPath: root.path) { try files.removeItem(at: root) }
    }

    func remove(_ id: String) throws {
        for url in [reference(id), exclusion(id), directory(id)] where files.fileExists(atPath: url.path) {
            try files.removeItem(at: url)
        }
    }

    static func validateMovie(_ url: URL) async throws {
        let asset = AVURLAsset(url: url)
        guard try await asset.load(.isPlayable),
              !(try await asset.loadTracks(withMediaType: .video)).isEmpty,
              !(try await asset.loadTracks(withMediaType: .audio)).isEmpty else {
            throw Problem("The downloaded dub could not be played.")
        }
    }

    func export(_ id: String) throws -> URL {
        let temp = files.temporaryDirectory.appending(path: UUID().uuidString + ".mp4")
        do { try files.linkItem(at: movie(id), to: temp) }
        catch { try files.copyItem(at: movie(id), to: temp) }
        return temp
    }
}
