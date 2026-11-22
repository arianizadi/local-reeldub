import Foundation
import Security
import StoreKit

/// The share extension can use a recent StoreKit decision made by the containing app.
/// Keep this item separate from server credentials so switching servers preserves the purchase.
enum SelfHostEntitlement {
    static let productID = "com.arianizadi.reeldub.selfhost.lifetime"

    private struct Check: Codable {
        let verifiedAt: Date
        let installation: String
    }

    private static var key: [String: Any] {
        var value = Connection.vault
        value[kSecAttrService as String] = "ReelDub.StoreKit"
        value[kSecAttrAccount as String] = productID
        return value
    }

    private static var containingApp: Bundle {
        let bundle = Bundle.main
        return bundle.bundleURL.pathExtension == "appex"
            ? Bundle(url: bundle.bundleURL.deletingLastPathComponent().deletingLastPathComponent()) ?? bundle
            : bundle
    }

    private static var installation: String {
        let app = containingApp
        // The container's receipt distinguishes TestFlight and App Store installs of the same build.
        let receipt = app.appStoreReceiptURL?.lastPathComponent ?? ""
        let version = app.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
        #if DEBUG
        let channel = "debug"
        #else
        let channel = "release"
        #endif
        return [app.bundleIdentifier ?? "", version, receipt, channel].joined(separator: "|")
    }

    static var isCached: Bool {
        var query = key
        query[kSecReturnData as String] = true
        var entry: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &entry) == errSecSuccess,
              let data = entry as? Data, let check = try? JSONDecoder().decode(Check.self, from: data),
              check.installation == installation else { return false }
        let age = Date().timeIntervalSince(check.verifiedAt)
        return age >= 0 && age < 24 * 60 * 60
    }

    static func clear() {
        SecItemDelete(key as CFDictionary)
    }

    static func accepts(_ transaction: Transaction) -> Bool {
        guard transaction.productID == productID, transaction.productType == .nonConsumable,
              transaction.appBundleID == containingApp.bundleIdentifier else { return false }
        #if !DEBUG
        if transaction.environment == .xcode { return false }
        #endif
        return true
    }

    static func currentVerification() async -> VerificationResult<Transaction>? {
        for await result in Transaction.currentEntitlements {
            if case .verified(let tx) = result, accepts(tx), tx.revocationDate == nil { return result }
        }
        return nil
    }

    @discardableResult static func record(_ result: VerificationResult<Transaction>) throws -> Bool {
        let tx = try result.payloadValue
        guard accepts(tx) else { throw Problem("Apple couldn’t verify the self-host unlock.") }
        guard tx.revocationDate == nil else { clear(); return false }
        let data = try JSONEncoder().encode(Check(verifiedAt: Date(), installation: installation))
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var status = SecItemUpdate(key as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(key.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Problem("Unlock your iPhone and try Restore Purchases again.") }
        return true
    }

    static func current() async -> Bool {
        if let result = await currentVerification() { return (try? record(result)) == true }
        // Some share-extension installations expose no purchase history for the containing app.
        // A visible history without an entitlement (including a refund) supersedes the shared cache.
        if Bundle.main.bundleURL.pathExtension == "appex",
           await Transaction.latest(for: productID) == nil { return isCached }
        clear()
        return false
    }
}
