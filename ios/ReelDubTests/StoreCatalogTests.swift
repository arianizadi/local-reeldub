import StoreKit
import StoreKitTest
import XCTest
@testable import ReelDub

final class StoreCatalogTests: XCTestCase {
    @MainActor
    func testPendingSelfHostPurchaseGrantsAccessOnlyAfterAppleApproval() async throws {
        let file = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "ReelDub", withExtension: "storekit"))
        let session = try SKTestSession(contentsOf: file)
        session.disableDialogs = true
        session.askToBuyEnabled = true
        session.clearTransactions()
        SelfHostEntitlement.clear()
        defer { session.askToBuyEnabled = false; session.clearTransactions(); SelfHostEntitlement.clear() }
        let access = SelfHostStore { _ in }
        let listener = Task { await access.observe() }
        defer { listener.cancel() }
        let restoredBeforePurchase = try await access.restore()
        XCTAssertFalse(restoredBeforePurchase)
        try await access.loadOffer()
        let intent = UUID()
        do {
            _ = try await access.purchase { intent }
            XCTFail("A purchase awaiting approval must not unlock the app.")
        } catch { XCTAssertTrue(error.localizedDescription.contains("approval"), error.localizedDescription) }
        XCTAssertFalse(access.owned)
        XCTAssertFalse(SelfHostEntitlement.isCached)
        let pending = try XCTUnwrap(session.allTransactions().first)
        try session.approveAskToBuyTransaction(identifier: pending.identifier)
        for _ in 0..<30 {
            if access.owned { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertTrue(access.owned)
        XCTAssertTrue(SelfHostEntitlement.isCached)
        try session.refundTransaction(identifier: pending.identifier)
        for _ in 0..<30 {
            if !access.owned { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertFalse(access.owned)
        XCTAssertFalse(SelfHostEntitlement.isCached)
    }

    @MainActor
    func testSelfHostUnlockIsRestorableAfterLocalDataIsClearedAndHonorsRefunds() async throws {
        let file = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "ReelDub", withExtension: "storekit"))
        let session = try SKTestSession(contentsOf: file)
        session.disableDialogs = true
        session.clearTransactions()
        SelfHostEntitlement.clear()
        defer { session.clearTransactions(); SelfHostEntitlement.clear() }
        let sku = SelfHostEntitlement.productID
        let intent = UUID()
        var recorded: [UInt64] = []
        var ledgerOnline = false
        let access = SelfHostStore { signed in
            guard ledgerOnline else { throw URLError(.notConnectedToInternet) }
            let transaction = try signed.payloadValue
            XCTAssertEqual(transaction.productID, sku)
            XCTAssertEqual(transaction.appAccountToken, intent)
            recorded.append(transaction.id)
        }
        await access.refresh()
        XCTAssertFalse(access.owned)
        try await access.loadOffer()
        let product = try XCTUnwrap(access.product)
        XCTAssertEqual(product.type, .nonConsumable)
        XCTAssertEqual(product.price, Decimal(string: "4.99"))
        do {
            _ = try await access.purchase { throw URLError(.notConnectedToInternet) }
            XCTFail("An unavailable reservation must stop checkout before Apple creates a transaction.")
        } catch { XCTAssertEqual((error as? URLError)?.code, .notConnectedToInternet) }
        XCTAssertTrue(session.allTransactions().isEmpty)
        let purchased = try await access.purchase { intent }
        XCTAssertTrue(purchased)
        XCTAssertTrue(SelfHostEntitlement.isCached)
        let purchase = try XCTUnwrap(session.allTransactions().first)
        func unfinished() async -> [UInt64] {
            var ids: [UInt64] = []
            for await result in Transaction.unfinished {
                if let tx = try? result.payloadValue, tx.productID == sku { ids.append(tx.id) }
            }
            return ids
        }
        let pendingDelivery = await unfinished()
        XCTAssertEqual(pendingDelivery, [UInt64(purchase.identifier)])
        ledgerOnline = true
        await access.reconcile()
        let afterDelivery = await unfinished()
        XCTAssertTrue(afterDelivery.isEmpty)
        XCTAssertEqual(recorded, [UInt64(purchase.identifier)])

        // A replacement phone starts without the old phone's connection or device-only Keychain item.
        SelfHostEntitlement.clear()
        let restored = SelfHostStore()
        let hasRestored = try await restored.restore()
        XCTAssertTrue(hasRestored)
        XCTAssertTrue(SelfHostEntitlement.isCached)
        let ownedAgain = try await restored.purchase { XCTFail("An existing purchase never opens checkout."); return UUID() }
        XCTAssertTrue(ownedAgain)
        XCTAssertEqual(session.allTransactions().count, 1)

        try session.refundTransaction(identifier: purchase.identifier)
        for _ in 0..<25 {
            await restored.refresh()
            if !restored.owned { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertFalse(restored.owned)
        XCTAssertFalse(SelfHostEntitlement.isCached)
    }

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

extension StoreCatalogTests {
    /// Prepare the app's local App Store for a subsequent UI test on the same disposable simulator.
    @MainActor
    func testPrepareSelfHostUITestStore() async throws {
        guard let mode = ProcessInfo.processInfo.environment["REELDUB_UI_STORE"],
              ["empty", "owned"].contains(mode) else {
            throw XCTSkip("Used by the self-host simulator fixtures.")
        }
        let file = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "ReelDub", withExtension: "storekit"))
        let session = try SKTestSession(contentsOf: file)
        session.resetToDefaultState()
        session.disableDialogs = true
        session.clearTransactions()
        let offer = SelfHostStore { _ in }
        for _ in 0..<20 {
            try? await offer.loadOffer()
            if offer.product != nil { break }
            try await Task.sleep(for: .milliseconds(500))
        }
        XCTAssertEqual(offer.product?.displayPrice, "$4.99")
        if mode == "owned" {
            _ = try await session.buyProduct(identifier: SelfHostEntitlement.productID)
        }
        // The UI must recover from Apple's transactions, without a previous device's local cache.
        SelfHostEntitlement.clear()
    }
}
