#if DEBUG
import Foundation

extension Studio {
    /// Optional wired development setup. The file is inside the app's private
    /// sandbox and can be placed there by a paired developer Mac via devicectl.
    func consumeDevelopmentPairing() async {
        let fm = FileManager.default
        let file = URL.documentsDirectory.appending(path: ".reeldub-pair.json")
        guard fm.fileExists(atPath: file.path) else { return }
        let receipt = URL.documentsDirectory.appending(path: ".reeldub-pair-result")
        var result = "failed"
        var requestID: UUID?
        do {
            try fm.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: file.path)
            let data = try Data(contentsOf: file)
            try fm.removeItem(at: file)
            struct Pairing: Decodable { let base: URL; let token: String; let requestID: UUID? }
            let input = try JSONDecoder().decode(Pairing.self, from: data)
            requestID = input.requestID
            let server = try Connection.server(input.base.absoluteString)
            guard !input.token.isEmpty else { throw Problem("Missing server key.") }
            try await connect(Connection(base: server, token: input.token))
            result = "connected"
        } catch {
            try? fm.removeItem(at: file)
            failure = "Couldn’t pair with your development server. Check the server address and key."
        }
        let acknowledgement = requestID.map { result + " " + $0.uuidString } ?? result
        try? Data(acknowledgement.utf8).write(to: receipt, options: [.atomic, .completeFileProtection])
    }
}
#endif
