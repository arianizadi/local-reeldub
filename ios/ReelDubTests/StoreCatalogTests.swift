import StoreKit
import StoreKitTest
import XCTest
@testable import ReelDub

final class StoreCatalogTests: XCTestCase {
    func testAccountBoundLocalPurchases() async throws {
        let file = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "ReelDub", withExtension: "storekit"))
        let store = try SKTestSession(contentsOf: file)
        store.disableDialogs = true
        store.timeRate = .oneRenewalEveryTwoSeconds
        store.clearTransactions()
        defer { store.clearTransactions() }
        let updates = Task {
            for await _ in Transaction.updates {
                if Task.isCancelled { break }
            }
        }
        defer { updates.cancel() }
        let account = UUID()
        let prefix = "com.arianizadi.reeldub."
        for product in ["minutes15", "creator.monthly"] {
            let id = prefix + product
            let products = try await Product.products(for: [id])
            let storefrontProduct = try XCTUnwrap(products.first)
            let offer = try XCTUnwrap(CloudProduct(rawValue: product))
            XCTAssertTrue(offer.accepts(storefrontProduct))
            XCTAssertFalse(storefrontProduct.displayPrice.isEmpty)
            let transaction: Transaction
            do {
                guard case .success(let result) = try await storefrontProduct.purchase(options: [.appAccountToken(account)]) else {
                    XCTFail("Local purchase did not complete for \(product)"); return
                }
                transaction = try result.payloadValue
            }
            catch { XCTFail("Local purchase of \(product) failed: \(error)"); return }
            XCTAssertEqual(transaction.productID, id)
            XCTAssertEqual(transaction.appAccountToken, account)
            XCTAssertEqual(transaction.environment, .xcode)
            XCTAssertEqual(transaction.productType, product == "creator.monthly" ? .autoRenewable : .consumable)
            await transaction.finish()
            if product == "creator.monthly" {
                var restored: Transaction?
                for _ in 0..<20 {
                    for await result in Transaction.currentEntitlements {
                        if let entry = try? result.payloadValue, entry.productID == id { restored = entry }
                    }
                    if restored != nil { break }
                    try await Task.sleep(for: .milliseconds(200))
                }
                XCTAssertEqual(restored?.appAccountToken, account)
                do { try store.expireSubscription(productIdentifier: id) }
                catch { XCTFail("Local subscription expiry failed: \(error)"); return }
                let subscription = try XCTUnwrap(storefrontProduct.subscription)
                var observedExpiry = false
                for _ in 0..<30 {
                    for status in try await subscription.status where status.state == .expired {
                        let expired = try status.transaction.payloadValue
                        if expired.productID == id && expired.appAccountToken == account { observedExpiry = true }
                    }
                    if observedExpiry { break }
                    try await Task.sleep(for: .milliseconds(250))
                }
                XCTAssertTrue(observedExpiry, "StoreKit must report the verified account's subscription expired.")
            }
        }
    }
}
