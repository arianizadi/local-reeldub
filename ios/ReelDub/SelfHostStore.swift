import Foundation
import Observation
import StoreKit

@MainActor @Observable
final class SelfHostStore {
    private(set) var checked = false
    private(set) var owned = false
    private(set) var product: Product?
    @ObservationIgnored private var revision = 0
    @ObservationIgnored private let deliver: @MainActor (VerificationResult<Transaction>) async throws -> Void

    init(deliver: @escaping @MainActor (VerificationResult<Transaction>) async throws -> Void = SelfHostStore.recordSale) {
        self.deliver = deliver
    }

    func loadOffer() async throws {
        let offers = try await Product.products(for: [SelfHostEntitlement.productID])
        product = offers.first { $0.id == SelfHostEntitlement.productID && $0.type == .nonConsumable }
        guard product != nil else { throw Problem("The App Store couldn’t load the self-host unlock. Try again later.") }
    }

    /// StoreKit supplies device-verified entitlements at launch, including on a replacement device.
    func refresh() async {
        revision += 1
        let request = revision
        let result = await SelfHostEntitlement.currentVerification()
        guard request == revision, !Task.isCancelled else { return }
        try? setAccess(result)
    }

    func purchase(prepare: () async throws -> UUID) async throws -> Bool {
        await refresh()
        if owned { return true }
        if product == nil { try await loadOffer() }
        guard let product else { throw Problem("The App Store couldn’t load the self-host unlock.") }
        let intent = try await prepare()
        try Task.checkCancellation()
        switch try await product.purchase(options: [.appAccountToken(intent)]) {
        case .success(let result):
            let tx = try result.payloadValue
            guard SelfHostEntitlement.accepts(tx) else { throw Problem("Apple couldn’t verify the self-host unlock.") }
            try await accept(result, transaction: tx)
            return owned
        case .userCancelled: return false
        case .pending: throw Problem("Awaiting Apple approval. The unlock will appear when Apple confirms the purchase.")
        @unknown default: throw Problem("The App Store hasn’t completed the purchase. Try Restore Purchases later.")
        }
    }

    func restore() async throws -> Bool {
        await refresh()
        if owned { return true }
        try await AppStore.sync()
        await refresh()
        return owned
    }

    /// Run alongside the launch listener and again when the app returns to the foreground.
    func reconcile() async {
        await refresh()
        for await result in Transaction.unfinished {
            guard !Task.isCancelled else { return }
            if case .verified(let tx) = result, SelfHostEntitlement.accepts(tx) {
                // Unfinished can contain an older purchase. Current entitlements owns the access decision.
                await recordAndFinish(result, transaction: tx)
            }
        }
    }

    func observe() async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { @MainActor in
                for await result in Transaction.updates {
                    guard !Task.isCancelled else { return }
                    if case .verified(let tx) = result, SelfHostEntitlement.accepts(tx) {
                        try? await self.accept(result, transaction: tx)
                    }
                }
            }
            await reconcile()
            await group.waitForAll()
        }
    }

    private func accept(_ result: VerificationResult<Transaction>, transaction: Transaction) async throws {
        revision += 1
        try setAccess(result)
        // A verified purchase belongs to the Apple customer even during a temporary ledger outage.
        // Keep it unfinished if delivery fails, so the next launch or foreground can deliver it again.
        await recordAndFinish(result, transaction: transaction)
    }

    private func recordAndFinish(_ result: VerificationResult<Transaction>, transaction: Transaction) async {
        do {
            try await deliver(result)
            await transaction.finish()
        } catch { /* StoreKit retains the unfinished transaction for retry. */ }
    }

    private func setAccess(_ result: VerificationResult<Transaction>?) throws {
        checked = true
        guard let result else {
            owned = false
            SelfHostEntitlement.clear()
            return
        }
        do { owned = try SelfHostEntitlement.record(result) }
        catch { owned = false; throw error }
    }

    nonisolated static func recordSale(_ result: VerificationResult<Transaction>) async throws {
        let tx = try result.payloadValue
        guard tx.environment != .xcode else { throw Problem("An Xcode purchase has no live ledger value.") }
        let cloud = API(connection: Connection(base: Connection.hosted, token: ""))
        _ = try await cloud.data("v1/store/self-host/transactions", method: "POST",
                                 body: ["signed_transaction": result.jwsRepresentation])
    }
}
